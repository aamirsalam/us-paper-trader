"""Paper-trading modes. Every active mode has to beat `baseline` after costs to justify itself."""

from __future__ import annotations

from .base import Strategy, StrategyResult, sma, total_return

CASH_PROXY = "SGOV"  # 0-3 month US Treasury bill ETF: "cash" that still earns T-bill yield


class Baseline(Strategy):
    """Buy and hold the S&P 500 (VOO). The bar every other mode must clear."""

    name = "baseline"
    universe = ("VOO",)
    lookback_days = 5
    drift_threshold = 0.0
    min_trade = 5.0

    def decide(self, closes, today, prev_state):
        return StrategyResult({"VOO": 1.0}, note="buy and hold")


class Conservative(Strategy):
    """Trend filter on the S&P 500, with hysteresis so it doesn't whipsaw around the line.

    Risk-on : 60% VOO / 40% SGOV when VOO is above its 200-day average (+2% band to switch in).
    Risk-off: 100% SGOV when VOO is below its 200-day average (-2% band to switch out).
    """

    name = "conservative"
    universe = ("VOO", CASH_PROXY)
    lookback_days = 260
    band = 0.02

    def decide(self, closes, today, prev_state):
        voo = closes.get("VOO", [])
        avg = sma(voo, 200)
        regime = prev_state.get("regime", "on")
        if avg is None:
            return StrategyResult(
                {CASH_PROXY: 1.0}, {"regime": "off"}, "not enough history, parked in T-bills"
            )
        last = voo[-1]
        if regime == "off" and last > avg * (1 + self.band):
            regime = "on"
        elif regime == "on" and last < avg * (1 - self.band):
            regime = "off"
        targets = {"VOO": 0.6, CASH_PROXY: 0.4} if regime == "on" else {CASH_PROXY: 1.0}
        return StrategyResult(targets, {"regime": regime}, f"VOO {last:.2f} vs 200d {avg:.2f}: risk-{regime}")


class Balanced(Strategy):
    """Monthly momentum rotation across broad and sector ETFs.

    On the first run of each month: rank by 6-month return, keep the top 3 that are
    positive and above their 200-day average, equal weight; unused slots go to SGOV.
    Between rebalances it holds the previous targets.
    """

    name = "balanced"
    candidates = ("VOO", "QQQ", "XLK", "XLV", "XLF", "XLE", "XLI", "XLY", "XLP", "XLU", "XLC", "XLB")
    universe = (*candidates, CASH_PROXY)
    lookback_days = 260
    top_n = 3

    def decide(self, closes, today, prev_state):
        month = today.strftime("%Y-%m")
        if prev_state.get("month") == month and prev_state.get("targets"):
            return StrategyResult(dict(prev_state["targets"]), dict(prev_state), "holding this month's picks")
        scored = []
        for sym in self.candidates:
            series = closes.get(sym, [])
            ret, avg = total_return(series, 126), sma(series, 200)
            if ret is not None and avg is not None and ret > 0 and series[-1] > avg:
                scored.append((ret, sym))
        picks = [sym for _, sym in sorted(scored, reverse=True)[: self.top_n]]
        slot = 1 / self.top_n
        targets = {sym: slot for sym in picks}
        if len(picks) < self.top_n:
            targets[CASH_PROXY] = round(1 - slot * len(picks), 10)
        note = "picks: " + (", ".join(picks) if picks else "none, all T-bills")
        return StrategyResult(targets, {"month": month, "targets": targets}, note)


REGISTRY: dict[str, type[Strategy]] = {s.name: s for s in (Baseline, Conservative, Balanced)}


def get(name: str) -> Strategy:
    try:
        return REGISTRY[name]()
    except KeyError as exc:
        raise ValueError(f"unknown mode {name!r}; known: {sorted(REGISTRY)}") from exc


__all__ = ["Strategy", "StrategyResult", "get", "REGISTRY", "CASH_PROXY"]
