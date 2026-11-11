from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class OrderIntent:
    """An order the strategy wants. Exactly one of notional (USD) or qty is set."""

    symbol: str
    side: str  # "buy" | "sell"
    notional: float | None = None
    qty: float | None = None
    reason: str = ""
    asset_class: str = "us_equity"

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"bad side {self.side!r}")
        if (self.notional is None) == (self.qty is None):
            raise ValueError("set exactly one of notional or qty")


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    filled_on: date


@dataclass(frozen=True)
class Dividend:
    action_id: str
    symbol: str
    rate: float  # USD per share
    ex_date: date
    payable_date: date


@dataclass
class Decision:
    """Result of running an intent through the compliance guard."""

    intent: OrderIntent | None
    reason: str = ""
    adjusted: bool = False
    original: OrderIntent | None = field(default=None, repr=False)

    @property
    def approved(self) -> bool:
        return self.intent is not None
