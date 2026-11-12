"""Paper-trading modes. Every active mode has to beat `baseline` after costs to justify itself."""

from __future__ import annotations

from ..risk import RiskLimits
from .base import Strategy, StrategyContext, StrategyResult, sma, total_return

CASH_PROXY = "SGOV"  # 0-3 month US Treasury bill ETF: "cash" that still earns T-bill yield


class Baseline(Strategy):
    """Buy and hold the S&P 500 (VOO). The bar every other mode must clear."""

    name = "baseline"
    universe = ("VOO",)
    lookback_days = 5
    drift_threshold = 0.0
    min_trade = 5.0

    def decide(self, closes, today, prev_state, context=None):
        return StrategyResult({"VOO": 1.0}, note="buy and hold")


class Conservative(Strategy):
    """Trend filter on the S&P 500, with hysteresis so it doesn't whipsaw around the line.

    Risk-on : 60% VOO / 40% SGOV when VOO is above its 200-day average (+2% band to switch in).
    Risk-off: 100% SGOV when VOO is below its 200-day average (-2% band to switch out).
    """

    name = "conservative"
    universe = ("VOO", CASH_PROXY)
    limits = RiskLimits(weekly_loss=0.03, max_drawdown=0.12)
    lookback_days = 260
    band = 0.02

    def decide(self, closes, today, prev_state, context=None):
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
    limits = RiskLimits(weekly_loss=0.04, max_drawdown=0.15)

    def decide(self, closes, today, prev_state, context=None):
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


class Aggressive(Strategy):
    """A notch above balanced: faster, more concentrated momentum that can hold mega-cap stocks.

    Twice a month (1st and 16th): score ETFs and mega-caps on a 50/50 blend of 3- and
    6-month return, keep those above their 100-day average, hold the top 3 equal weight.
    Market brake: when VOO is below its 200-day average, hold at most 1 pick.
    Protective exit: between rebalances, a pick that closes below its 50-day average is
    sold to SGOV and stays out until the next rebalance.
    Earnings: single stocks reporting within the blackout window aren't picked or added
    to, and a held stock that reports today or tomorrow is sold first (re-eligible at the
    next rebalance). ETFs are unaffected.
    No leverage, no shorts: still inside what LRS allows.
    """

    name = "aggressive"
    etfs = ("QQQ", "SMH", "XLK", "XLC", "XLY", "XLF", "XLE", "XLI", "XLV", "IWM")
    single_stocks = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "LLY")
    candidates = (*etfs, *single_stocks)
    universe = (*candidates, "VOO", CASH_PROXY)
    lookback_days = 260
    top_n = 3
    drift_threshold = 0.04
    limits = RiskLimits(weekly_loss=0.06, max_drawdown=0.20, max_single_stock=0.40)

    @staticmethod
    def period(day) -> str:
        return f"{day:%Y-%m}-{'a' if day.day <= 15 else 'b'}"

    def _score(self, series: list[float]) -> float | None:
        r3, r6, avg = total_return(series, 63), total_return(series, 126), sma(series, 100)
        if r3 is None or r6 is None or avg is None or series[-1] <= avg:
            return None
        score = 0.5 * r3 + 0.5 * r6
        return score if score > 0 else None

    def _targets(self, picks: list[str]) -> dict[str, float]:
        slot = 1 / self.top_n
        targets = {sym: slot for sym in picks}
        if len(picks) < self.top_n:
            targets[CASH_PROXY] = round(1 - slot * len(picks), 10)
        return targets

    def decide(self, closes, today, prev_state, context=None):
        ctx = context or StrategyContext()
        period = self.period(today)
        if prev_state.get("period") == period and prev_state.get("picks") is not None:
            picks = list(prev_state["picks"])
            stopped, earnings = [], []
            for sym in picks:
                series = closes.get(sym, [])
                avg50 = sma(series, 50)
                if sym in ctx.exit_now:
                    earnings.append(sym)
                elif avg50 is not None and series[-1] < avg50:
                    stopped.append(sym)
            if not stopped and not earnings:
                return StrategyResult(self._targets(picks), dict(prev_state), "holding this period's picks")
            kept = [s for s in picks if s not in stopped and s not in earnings]
            state = {**prev_state, "picks": kept}
            notes = []
            if stopped:
                notes.append("protective exit (below 50d avg): " + ", ".join(stopped))
            if earnings:
                notes.append("out before earnings: " + ", ".join(earnings))
            return StrategyResult(self._targets(kept), state, "; ".join(notes))

        voo = closes.get("VOO", [])
        voo_avg = sma(voo, 200)
        risk_on = voo_avg is None or voo[-1] > voo_avg
        scored = []
        skipped = []
        for sym in self.candidates:
            s = self._score(closes.get(sym, []))
            if s is None:
                continue
            if sym in ctx.blackout:
                skipped.append(sym)
                continue
            scored.append((s, sym))
        limit = self.top_n if risk_on else 1
        picks = [sym for _, sym in sorted(scored, reverse=True)[:limit]]
        note = (
            ("" if risk_on else "market brake on (VOO < 200d), ")
            + "picks: "
            + (", ".join(picks) if picks else "none, all T-bills")
        )
        if skipped:
            note += "; skipped for earnings: " + ", ".join(skipped)
        return StrategyResult(self._targets(picks), {"period": period, "picks": picks}, note)


REGISTRY: dict[str, type[Strategy]] = {s.name: s for s in (Baseline, Conservative, Balanced, Aggressive)}


def get(name: str) -> Strategy:
    try:
        return REGISTRY[name]()
    except KeyError as exc:
        raise ValueError(f"unknown mode {name!r}; known: {sorted(REGISTRY)}") from exc


__all__ = ["Strategy", "StrategyContext", "StrategyResult", "get", "REGISTRY", "CASH_PROXY"]
