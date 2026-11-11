from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class StrategyResult:
    targets: dict[str, float]  # symbol -> weight of investable equity, sum <= 1
    state: dict[str, Any] = field(default_factory=dict)  # carried to the next run via the ledger
    note: str = ""


class Strategy(ABC):
    name: str
    universe: tuple[str, ...]
    lookback_days: int = 260
    drift_threshold: float = 0.05
    min_trade: float = 1.0

    @abstractmethod
    def decide(
        self, closes: dict[str, list[float]], today: date, prev_state: dict[str, Any]
    ) -> StrategyResult: ...


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def total_return(values: list[float], n: int) -> float | None:
    if len(values) <= n or values[-n - 1] <= 0:
        return None
    return values[-1] / values[-n - 1] - 1
