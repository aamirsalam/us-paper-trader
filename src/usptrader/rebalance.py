"""Turn target weights into orders. Sells come first; buys wait for settled cash."""

from __future__ import annotations

import math

from .ledger import EPS, Position
from .models import OrderIntent


def plan_orders(
    targets: dict[str, float],
    positions: dict[str, Position],
    prices: dict[str, float],
    equity: float,
    *,
    drift_threshold: float = 0.05,
    min_trade: float = 1.0,
    cash_buffer: float = 0.01,
) -> list[OrderIntent]:
    """Orders that move `positions` towards `targets`.

    A position is only resized when it is off target by more than `drift_threshold`
    of total equity, which stops small daily wiggles from generating trades (and fees).
    Opening or fully closing a position always goes through.
    """
    if equity <= 0:
        return []
    weight_sum = sum(w for w in targets.values() if w > 0)
    if weight_sum > 1 + 1e-9:
        raise ValueError(f"target weights sum to {weight_sum:.4f} > 1")

    investable = equity * (1 - cash_buffer)
    sells: list[OrderIntent] = []
    buys: list[OrderIntent] = []

    for symbol in sorted(set(targets) | set(positions)):
        weight = max(0.0, targets.get(symbol, 0.0))
        held = positions.get(symbol, Position()).qty
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        current = held * price
        target = weight * investable
        diff = target - current

        if weight == 0 and held > EPS:
            sells.append(OrderIntent(symbol, "sell", qty=held, reason="exit"))
            continue
        opening = held <= EPS and target >= min_trade
        if not opening and abs(diff) < max(min_trade, drift_threshold * equity):
            continue
        if diff < 0:
            qty = min(held, round(-diff / price, 9))
            if qty > EPS:
                sells.append(OrderIntent(symbol, "sell", qty=qty, reason="trim to target"))
        else:
            notional = math.floor(diff * 100) / 100
            if notional >= min_trade:
                buys.append(
                    OrderIntent(symbol, "buy", notional=notional, reason="open" if opening else "add")
                )
    # Largest buys first so the core position gets funded if cash runs short.
    buys.sort(key=lambda o: o.notional or 0, reverse=True)
    return sells + buys
