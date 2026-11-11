"""Deterministic in-memory broker for tests and `--dry-run` smoke runs."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from ..models import Dividend, Fill, OrderIntent
from .base import Broker


class FakeBroker(Broker):
    name = "fake"

    def __init__(
        self,
        cash: float = 1000.0,
        today: date | None = None,
        trends: dict[str, float] | None = None,
        holidays: set[date] | None = None,
    ) -> None:
        self._cash = cash
        self._offset = 0
        self._today = today or date.today()
        self._trends = trends or {}
        self._holidays = holidays or set()
        self._series: dict[str, list[float]] = {}
        self._positions: dict[str, float] = {}
        self._orders: dict[str, tuple[str, Fill | None]] = {}
        self.submitted: list[OrderIntent] = []
        self.dividends: list[Dividend] = []

    def lock_down_account(self) -> list[str]:
        return []

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self._holidays

    def next_trading_day(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    SERIES_LEN = 800
    START = 400  # index of "today" before any advance()

    def _series_for(self, symbol: str) -> list[float]:
        s = self._series.get(symbol)
        if s is None:
            drift = self._trends.get(symbol, 0.0003)
            rng = random.Random(symbol)
            price = 50 + rng.random() * 400
            s = []
            for _ in range(self.SERIES_LEN):
                price *= math.exp(drift + rng.gauss(0, 0.01))
                s.append(round(price, 2))
            self._series[symbol] = s
        return s

    def advance(self, days: int = 1) -> None:
        """Move the fake market forward (used by `usptrader simulate`)."""
        self._offset = min(self._offset + days, self.SERIES_LEN - self.START - 1)

    def daily_closes(self, symbols: list[str], days: int) -> dict[str, list[float]]:
        i = self.START + self._offset
        return {sym: self._series_for(sym)[:i][-days:] for sym in symbols}

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        i = self.START + self._offset
        return {sym: self._series_for(sym)[i] for sym in symbols}

    def cash_available(self) -> float:
        return self._cash

    def positions(self) -> dict[str, float]:
        return {s: q for s, q in self._positions.items() if q > 1e-9}

    def submit(self, intent: OrderIntent, client_order_id: str) -> str:
        self.submitted.append(intent)
        price = self.latest_prices([intent.symbol])[intent.symbol]
        qty = intent.qty if intent.qty is not None else round(intent.notional / price, 9)
        if intent.side == "buy":
            self._cash -= qty * price
            self._positions[intent.symbol] = self._positions.get(intent.symbol, 0) + qty
        else:
            self._cash += qty * price
            self._positions[intent.symbol] = self._positions.get(intent.symbol, 0) - qty
        oid = f"fake-{len(self._orders) + 1}"
        self._orders[oid] = ("filled", Fill(oid, intent.symbol, intent.side, qty, price, self._today))
        return oid

    def order_fill(self, order_id: str) -> tuple[str, Fill | None]:
        return self._orders.get(order_id, ("dead", None))

    def cash_dividends(self, symbols: list[str], start: date, end: date) -> list[Dividend]:
        return [d for d in self.dividends if d.symbol in symbols and start <= d.payable_date <= end]
