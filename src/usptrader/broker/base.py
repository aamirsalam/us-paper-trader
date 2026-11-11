from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import Dividend, Fill, OrderIntent


class Broker(ABC):
    """What the trader needs from a broker. Alpaca paper today; IBKR or a GIFT City broker later."""

    name: str = "broker"

    @abstractmethod
    def lock_down_account(self) -> list[str]:
        """Apply broker-side cash-account settings (no shorting, no margin...). Returns what changed."""

    @abstractmethod
    def is_trading_day(self, day: date) -> bool: ...

    @abstractmethod
    def next_trading_day(self, day: date) -> date: ...

    @abstractmethod
    def daily_closes(self, symbols: list[str], days: int) -> dict[str, list[float]]:
        """Split/dividend-adjusted daily closes, oldest first, excluding today."""

    @abstractmethod
    def latest_prices(self, symbols: list[str]) -> dict[str, float]: ...

    @abstractmethod
    def cash_available(self) -> float:
        """Cash the broker itself would let us spend without margin."""

    @abstractmethod
    def positions(self) -> dict[str, float]:
        """Broker-side quantities, for reconciliation."""

    @abstractmethod
    def submit(self, intent: OrderIntent, client_order_id: str) -> str:
        """Submit a day market order and return the broker order id."""

    @abstractmethod
    def order_fill(self, order_id: str) -> tuple[str, Fill | None]:
        """(status, fill-or-None). Status is 'filled', 'open' or 'dead'."""

    @abstractmethod
    def cash_dividends(self, symbols: list[str], start: date, end: date) -> list[Dividend]: ...
