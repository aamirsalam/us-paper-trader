"""Order guard that keeps the paper run inside what an Indian resident can do under LRS.

Rules enforced here (independent of whatever the paper broker would allow):
  * only listed US stocks and ETFs: no options, crypto, futures
  * no short selling: sells are capped at the quantity actually held
  * no margin and no unsettled funds: buys are funded from settled cash only
  * no leveraged or inverse ETFs (their daily-reset leverage is the kind of
    product LRS is meant to keep you out of, and they wreck small accounts)
"""

from __future__ import annotations

import math

from .ledger import EPS, Position
from .models import Decision, OrderIntent

MIN_NOTIONAL_USD = 1.0  # Alpaca's minimum for fractional notional orders

LEVERAGED_OR_INVERSE = frozenset(
    """
    TQQQ SQQQ QLD QID SSO SDS UPRO SPXU SPXL SPXS SH PSQ DOG DXD SDOW UDOW UMDD SMDD
    TNA TZA URTY SRTY UWM TWM SOXL SOXS USD SSG TECL TECS FAS FAZ LABU LABD CURE
    NUGT DUST JNUG JDST GUSH DRIP ERX ERY BOIL KOLD UCO SCO UVXY SVXY UVIX SVIX VIXY
    TMF TMV TBT UBT TYD TYO YANG YINN FNGU FNGD BULZ BERZ WEBL WEBS DPST HIBL HIBS
    TSLL TSLQ TSLZ NVDL NVDU NVDD NVDQ AAPU AAPD AMZU AMZD MSFU MSFD GGLL GGLS METU CONL MSTU MSTX
    """.split()
)


class ComplianceGuard:
    def __init__(self, blocked: frozenset[str] = LEVERAGED_OR_INVERSE) -> None:
        self.blocked = blocked

    def check(
        self,
        intent: OrderIntent,
        *,
        positions: dict[str, Position],
        available_cash: float,
    ) -> Decision:
        if intent.asset_class != "us_equity":
            return Decision(None, f"LRS: {intent.asset_class} not allowed, stocks/ETFs only", original=intent)
        if intent.symbol.upper() in self.blocked:
            return Decision(None, "leveraged/inverse ETF blocked", original=intent)

        if intent.side == "sell":
            held = positions.get(intent.symbol, Position()).qty
            if held <= EPS:
                return Decision(None, "LRS: would be a short sale (nothing held)", original=intent)
            if intent.qty is None:
                return Decision(None, "sells must be sized in shares", original=intent)
            if intent.qty > held + EPS:
                trimmed = OrderIntent(intent.symbol, "sell", qty=held, reason=intent.reason)
                return Decision(trimmed, "trimmed to quantity held (no shorting)", True, intent)
            return Decision(intent)

        # buy
        if intent.notional is None:
            return Decision(None, "buys must be sized in USD notional", original=intent)
        usable = math.floor(max(0.0, available_cash) * 100) / 100
        if usable < MIN_NOTIONAL_USD:
            return Decision(None, "no settled cash (would need margin or unsettled funds)", original=intent)
        if intent.notional > usable + 1e-6:
            trimmed = OrderIntent(intent.symbol, "buy", notional=usable, reason=intent.reason)
            return Decision(trimmed, f"trimmed to settled cash ${usable:.2f}", True, intent)
        if intent.notional < MIN_NOTIONAL_USD:
            return Decision(None, f"below ${MIN_NOTIONAL_USD:.0f} minimum order", original=intent)
        return Decision(intent)
