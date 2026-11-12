"""Loss limits per mode, borrowed in spirit from day-trader-skill's trader-risk.

Three brakes, checked at the start of every trade phase with live prices:

  weekly loss    equity down more than X% since last week's final close
                 -> PAUSE: no new risk buys for the rest of the week (sells and T-bill
                    parking still allowed). Clears itself on Monday.
  max drawdown   equity down more than Y% from its peak
                 -> HALT: everything moves to the T-bill ETF. Auto-resumes after
                    `halt_days` trading days (0 = only `usptrader resume`), with the
                    peak reset so it doesn't trip again immediately.
  single stock   a single company may not grow past Z% of equity through buys

Baseline has no limits: it's the yardstick and must stay untouched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .ledger import Ledger


@dataclass(frozen=True)
class RiskLimits:
    weekly_loss: float | None = None
    max_drawdown: float | None = None
    max_single_stock: float | None = None
    halt_days: int = 20

    @property
    def active(self) -> bool:
        return any(v is not None for v in (self.weekly_loss, self.max_drawdown, self.max_single_stock))


NO_LIMITS = RiskLimits()


def limits_from_env(mode: str, default: RiskLimits) -> RiskLimits:
    """RISK_<MODE>_WEEKLY / _DRAWDOWN / _SINGLE (fractions, or "off") and RISK_<MODE>_HALT_DAYS."""

    def val(suffix: str, current: float | None) -> float | None:
        raw = os.environ.get(f"RISK_{mode.upper()}_{suffix}")
        if raw is None or raw == "":
            return current
        return None if raw.lower() in ("off", "none", "0") else float(raw)

    halt = os.environ.get(f"RISK_{mode.upper()}_HALT_DAYS")
    return replace(
        default,
        weekly_loss=val("WEEKLY", default.weekly_loss),
        max_drawdown=val("DRAWDOWN", default.max_drawdown),
        max_single_stock=val("SINGLE", default.max_single_stock),
        halt_days=int(halt) if halt not in (None, "") else default.halt_days,
    )


@dataclass
class RiskState:
    status: str = "ok"  # ok | paused | halted
    reason: str = ""
    events: list[str] | None = None  # what changed in this assessment, for the run log

    @property
    def halted(self) -> bool:
        return self.status == "halted"

    @property
    def paused(self) -> bool:
        return self.status == "paused"


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _halt_info(ledger: Ledger) -> tuple[dict | None, int]:
    """(open halt entry or None, trading days elapsed since it, counted in snapshots)."""
    halt, days = None, 0
    for e in ledger.entries:
        if e["type"] == "risk" and e["action"] in ("halt", "resume"):
            halt, days = (e, 0) if e["action"] == "halt" else (None, 0)
        elif e["type"] == "snapshot" and halt is not None:
            days += 1
    return halt, days


def peak_equity(ledger: Ledger) -> float:
    peak = ledger.deposited_usd()
    for e in ledger.entries:
        if e["type"] == "snapshot":
            peak = max(peak, e["equity_usd"])
        elif e["type"] == "risk" and e["action"] == "resume":
            peak = e["equity_ref"]
    return peak


def week_reference(ledger: Ledger, today: date) -> float:
    """Last week's final equity, or the equity at a later resume (so a lifted halt starts clean)."""
    monday = week_start(today).isoformat()
    ref = ledger.deposited_usd()
    for e in ledger.entries:
        if e["type"] == "snapshot" and e["date"] < monday:
            ref = e["equity_usd"]
        elif e["type"] == "risk" and e["action"] == "resume":
            ref = e["equity_ref"]
    return ref


def assess(ledger: Ledger, equity_now: float, today: date, limits: RiskLimits) -> RiskState:
    """Evaluate the brakes and record any state change in the ledger."""
    events: list[str] = []
    if not limits.active or ledger.deposited_usd() <= 0:
        return RiskState(events=events)

    halt, days = _halt_info(ledger)
    if halt is not None:
        if limits.halt_days and days >= limits.halt_days:
            ledger.record_risk(today, "resume", equity_ref=round(equity_now, 4), auto=True)
            events.append(f"risk halt lifted automatically after {days} trading days")
        else:
            left = f", auto-resume in {limits.halt_days - days} trading days" if limits.halt_days else ""
            return RiskState("halted", f"{halt['reason']} (since {halt['date']}{left})", events)

    if limits.max_drawdown is not None:
        peak = max(peak_equity(ledger), equity_now)
        dd = equity_now / peak - 1 if peak else 0.0
        if dd <= -limits.max_drawdown:
            reason = f"drawdown {dd:.1%} from peak ${peak:,.2f} breached the {limits.max_drawdown:.0%} limit"
            ledger.record_risk(
                today, "halt", reason=reason, drawdown=round(dd, 4), equity=round(equity_now, 4)
            )
            events.append("HALT: " + reason)
            return RiskState("halted", reason, events)

    monday = week_start(today).isoformat()
    paused = [e for e in ledger.of_type("risk") if e["action"] == "pause" and e.get("week") == monday]
    if paused:
        return RiskState("paused", paused[-1]["reason"], events)
    if limits.weekly_loss is not None:
        ref = week_reference(ledger, today)
        wk = equity_now / ref - 1 if ref else 0.0
        if wk <= -limits.weekly_loss:
            reason = f"down {wk:.1%} this week, over the {limits.weekly_loss:.0%} weekly limit"
            ledger.record_risk(today, "pause", week=monday, reason=reason, equity=round(equity_now, 4))
            events.append("PAUSE: " + reason)
            return RiskState("paused", reason, events)
    return RiskState(events=events)


def status_line(ledger: Ledger, limits: RiskLimits, today: date) -> str:
    """Read-only one-liner for the report."""
    if not limits.active:
        return "risk limits: none (benchmark)"
    halt, days = _halt_info(ledger)
    if halt is not None:
        left = f", auto-resume in {max(0, limits.halt_days - days)} trading days" if limits.halt_days else ""
        return f"⛔ HALTED since {halt['date']}: {halt['reason']}{left}"
    monday = week_start(today).isoformat()
    for e in reversed(ledger.of_type("risk")):
        if e["action"] == "pause" and e.get("week") == monday:
            return f"⏸ paused this week: {e['reason']}"
    bits = []
    if limits.weekly_loss is not None:
        bits.append(f"weekly -{limits.weekly_loss:.0%}")
    if limits.max_drawdown is not None:
        bits.append(f"drawdown -{limits.max_drawdown:.0%}")
    if limits.max_single_stock is not None:
        bits.append(f"single stock ≤{limits.max_single_stock:.0%}")
    return "risk limits ok (" + ", ".join(bits) + ")"
