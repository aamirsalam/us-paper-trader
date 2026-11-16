"""A mode that trades only on our own ledger, with no broker account behind it.

Alpaca allows three paper accounts per login. The baseline mode is a single buy of VOO
held forever, so it doesn't need one: this broker takes market data (prices, calendar,
dividends) from another mode's Alpaca connection and fills orders itself at the latest
trade price plus a little slippage. All the usual rules still apply through the ledger.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..models import Dividend, Fill, OrderIntent
from .base import Broker

ET = ZoneInfo("America/New_York")


class VirtualBroker(Broker):
    name = "virtual"
    reconciles = False  # there is no broker-side position to compare the ledger with

    def __init__(
        self,
        data: Broker,
        slippage_bps: float = 2.0,
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        self.data = data
        self.slippage = slippage_bps / 10_000
        self._today = today_fn or (lambda: datetime.now(ET).date())
        self._fills: dict[str, Fill] = {}

    def lock_down_account(self) -> list[str]:
        return []

    def is_trading_day(self, day: date) -> bool:
        return self.data.is_trading_day(day)

    def next_trading_day(self, day: date) -> date:
        return self.data.next_trading_day(day)

    def daily_closes(self, symbols: list[str], days: int) -> dict[str, list[float]]:
        return self.data.daily_closes(symbols, days)

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        return self.data.latest_prices(symbols)

    def cash_available(self) -> float:
        return float("inf")  # the ledger's settled cash is the limit

    def positions(self) -> dict[str, float]:
        return {}

    def submit(self, intent: OrderIntent, client_order_id: str) -> str:
        price = self.data.latest_prices([intent.symbol]).get(intent.symbol)
        if not price or price <= 0:
            raise RuntimeError(f"no market price for {intent.symbol}")
        price *= 1 + self.slippage if intent.side == "buy" else 1 - self.slippage
        qty = intent.qty if intent.qty is not None else round(intent.notional / price, 9)
        self._fills[client_order_id] = Fill(
            client_order_id, intent.symbol, intent.side, qty, round(price, 6), self._today()
        )
        return client_order_id

    def order_fill(self, order_id: str) -> tuple[str, Fill | None]:
        fill = self._fills.get(order_id)
        return ("filled", fill) if fill else ("dead", None)

    def cash_dividends(self, symbols: list[str], start: date, end: date) -> list[Dividend]:
        return self.data.cash_dividends(symbols, start, end)
