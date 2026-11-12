"""Broker that replays daily history for backtests.

Clock per trading day:
  premarket  sees adjusted closes up to the previous day (no peeking at today)
  trade      fills market orders at today's open, plus slippage
  postclose  marks positions at today's close
"""

from __future__ import annotations

from datetime import date, timedelta

from ..marketdata import MarketData
from ..models import Dividend, Fill, OrderIntent
from .base import Broker

PAYABLE_LAG_DAYS = 14  # cash dividends typically arrive two to four weeks after the ex-date


class HistoricalBroker(Broker):
    name = "historical"

    def __init__(self, data: MarketData, slippage_bps: float = 5.0) -> None:
        self.data = data
        self.slippage = slippage_bps / 10_000
        self.today: date = data.calendar[0]
        self.phase = "open"
        self._days = set(data.calendar) | {d for s in data.series.values() for d in s.dates}
        self._positions: dict[str, float] = {}
        self._orders: dict[str, Fill] = {}

    def set_clock(self, day: date, phase: str) -> None:
        self.today, self.phase = day, phase

    def lock_down_account(self) -> list[str]:
        return []

    def is_trading_day(self, day: date) -> bool:
        return day in self._days

    def next_trading_day(self, day: date) -> date:
        d, last = day + timedelta(days=1), self.data.calendar[-1]
        while not (d in self._days or (d > last and d.weekday() < 5)):
            d += timedelta(days=1)
        return d

    def daily_closes(self, symbols: list[str], days: int) -> dict[str, list[float]]:
        out = {}
        for sym in symbols:
            s = self.data.series.get(sym)
            if not s:
                out[sym] = []
                continue
            i = s.idx(self.today)
            if i is None:  # symbol didn't trade today: use everything before today
                i = sum(1 for d in s.dates if d < self.today)
            out[sym] = s.adjclose[max(0, i - days) : i]
        return out

    def _price(self, sym: str) -> float | None:
        s = self.data.series.get(sym)
        i = s.idx(self.today) if s else None
        if i is None:
            return None
        return s.open[i] if self.phase == "open" else s.close[i]

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        out = {}
        for sym in symbols:
            p = self._price(sym)
            if p is not None:
                out[sym] = p
        return out

    def cash_available(self) -> float:
        return float("inf")  # the ledger's settled cash is the real constraint

    def positions(self) -> dict[str, float]:
        return {s: q for s, q in self._positions.items() if q > 1e-9}

    def submit(self, intent: OrderIntent, client_order_id: str) -> str:
        price = self._price(intent.symbol)
        if price is None:
            raise RuntimeError(f"no price for {intent.symbol} on {self.today}")
        price *= 1 + self.slippage if intent.side == "buy" else 1 - self.slippage
        qty = intent.qty if intent.qty is not None else round(intent.notional / price, 9)
        sign = 1 if intent.side == "buy" else -1
        self._positions[intent.symbol] = self._positions.get(intent.symbol, 0.0) + sign * qty
        fill = Fill(client_order_id, intent.symbol, intent.side, qty, round(price, 6), self.today)
        self._orders[client_order_id] = fill
        return client_order_id

    def order_fill(self, order_id: str) -> tuple[str, Fill | None]:
        fill = self._orders.get(order_id)
        return ("filled", fill) if fill else ("dead", None)

    def cash_dividends(self, symbols: list[str], start: date, end: date) -> list[Dividend]:
        out = []
        for sym in symbols:
            s = self.data.series.get(sym)
            if not s:
                continue
            for ex, amount in s.dividends.items():
                pay = ex + timedelta(days=PAYABLE_LAG_DAYS)
                if start <= pay <= end:
                    out.append(Dividend(f"{sym}-{ex}", sym, amount, ex, pay))
        return out
