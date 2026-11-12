"""Morning report: how each mode is doing in USD and in the INR you'd actually get back."""

from __future__ import annotations

import logging
import smtplib
from datetime import date
from email.message import EmailMessage

from . import strategies
from .config import Settings
from .costs import indian_fy
from .ledger import Ledger, LedgerStore
from .risk import limits_from_env, status_line
from .runner import ModeOutcome

log = logging.getLogger(__name__)


def mode_metrics(ledger: Ledger) -> dict | None:
    snaps = ledger.of_type("snapshot")
    if not snaps:
        return None
    last = snaps[-1]
    dep_usd, dep_inr = ledger.deposited_usd(), ledger.deposited_inr()
    equity, rate = last["equity_usd"], last["rate"]
    dep_rate = ledger.weighted_deposit_rate()
    peak, max_dd = 0.0, 0.0
    for s in snaps:
        peak = max(peak, s["equity_usd"])
        if peak:
            max_dd = min(max_dd, s["equity_usd"] / peak - 1)
    div_gross, div_withheld = ledger.dividends()
    inr_back = last["inr_if_withdrawn"]
    return {
        "mode": ledger.mode,
        "days": len(snaps),
        "equity_usd": equity,
        "pnl_usd": equity - dep_usd,
        "pnl_pct": equity / dep_usd - 1 if dep_usd else 0.0,
        "inr_in": dep_inr,
        "inr_back": inr_back,
        "pnl_inr": inr_back - dep_inr,
        "pnl_inr_pct": inr_back / dep_inr - 1 if dep_inr else 0.0,
        # INR result split into: what the strategy did, what the rupee did, what the bank took.
        "strategy_inr": (equity - dep_usd) * dep_rate,
        "fx_inr": equity * (rate - dep_rate),
        "friction_inr": (inr_back - dep_inr) - (equity - dep_usd) * dep_rate - equity * (rate - dep_rate),
        "max_dd": max_dd,
        "div_gross": div_gross,
        "div_withheld": div_withheld,
        "trades": len(ledger.of_type("fill")),
        "blocked": len(ledger.of_type("rejection")),
        "rate": rate,
        "holdings": last.get("holdings", {}),
        "cash": last["cash_usd"],
        "mismatches": last.get("mismatches", []),
    }


def build_report(
    settings: Settings, store: LedgerStore, today: date, outcomes: list[ModeOutcome]
) -> tuple[str, str]:
    metrics = [m for m in (mode_metrics(Ledger(x, store, settings.costs)) for x in settings.modes) if m]
    base = next((m for m in metrics if m["mode"] == "baseline"), None)
    halted: list[str] = []

    lines = [f"US paper trader, {today:%a %d %b %Y} (ET)", ""]
    if metrics:
        lines.append(f"USD/INR {metrics[0]['rate']:.2f}  ·  running {max(m['days'] for m in metrics)} days")
        lines.append("")
        lines.append(f"{'mode':<13}{'equity $':>10}{'USD %':>8}{'INR %':>8}{'vs base':>9}{'max DD':>8}")
        for m in metrics:
            vs = f"{(m['pnl_inr_pct'] - base['pnl_inr_pct']) * 100:+.1f}" if base and m is not base else "—"
            lines.append(
                f"{m['mode']:<13}{m['equity_usd']:>10,.2f}{m['pnl_pct'] * 100:>+8.1f}"
                f"{m['pnl_inr_pct'] * 100:>+8.1f}{vs:>9}{m['max_dd'] * 100:>8.1f}"
            )
        lines.append("")
        for m in metrics:
            lines.append(
                f"[{m['mode']}] ₹{m['inr_in']:,.0f} in → ₹{m['inr_back']:,.0f} if withdrawn today "
                f"(strategy ₹{m['strategy_inr']:+,.0f}, rupee ₹{m['fx_inr']:+,.0f}, "
                f"bank FX ₹{m['friction_inr']:+,.0f})"
            )
            held = ", ".join(f"{s} ${v:,.0f}" for s, v in sorted(m["holdings"].items())) or "none"
            lines.append(f"    holdings: {held}; cash ${m['cash']:,.2f}")
            lines.append(
                f"    {m['trades']} fills, {m['blocked']} orders blocked by LRS guard, dividends "
                f"${m['div_gross']:.2f} gross / ${m['div_withheld']:.2f} withheld"
            )
            mode_limits = limits_from_env(m["mode"], strategies.get(m["mode"]).limits)
            led = Ledger(m["mode"], store, settings.costs)
            risk_line = status_line(led, mode_limits, today)
            lines.append("    " + risk_line)
            if risk_line.startswith("⛔"):
                halted.append(m["mode"])
            earn = sum(1 for e in led.of_type("rejection") if e["reason"].startswith("earnings"))
            if earn:
                lines.append(f"    {earn} buys held back by the earnings blackout so far")
            for mm in m["mismatches"]:
                lines.append(f"    ⚠ broker mismatch {mm}")
    lines += ["", "Today:"]
    for o in outcomes:
        status = "OK" if o.ok else f"FAILED: {o.error}"
        lines.append(f"  [{o.mode}] {status}")
        lines.extend(f"     - {x}" for x in o.lines)

    total_inr = 0.0
    fy = indian_fy(today)
    for mode in settings.modes:
        total_inr += sum(
            e["inr"] for e in Ledger(mode, store, settings.costs).of_type("deposit") if e["fy"] == fy
        )
    lines += [
        "",
        f"If this were real: {fy} LRS remittances ₹{total_inr:,.0f} of the ₹10,00,000 TCS-free limit. "
        "Report foreign holdings in Schedule FA.",
        "Paper results. Not investment advice.",
    ]

    if metrics:
        bits = []
        for m in metrics:
            bits.append(f"{m['mode']} {m['pnl_inr_pct'] * 100:+.1f}%")
        subject = f"US paper {today:%d %b}: " + " | ".join(bits)
    else:
        subject = f"US paper {today:%d %b}: no snapshots yet"
    if halted:
        subject = f"⛔ {', '.join(halted)} halted · " + subject
    if any(not o.ok for o in outcomes):
        subject = "⚠ " + subject
    return subject, "\n".join(lines)


def send(settings: Settings, subject: str, body: str) -> bool:
    """Email via SMTP (e.g. Gmail with an app password). Prints instead if SMTP isn't configured."""
    if not (settings.smtp_host and settings.report_to):
        print(f"{subject}\n\n{body}")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.report_from
    msg["To"] = settings.report_to
    msg.set_content(body)
    msg.add_alternative(
        f"<pre style='font:13px/1.45 ui-monospace,Menlo,Consolas,monospace'>{_esc(body)}</pre>",
        subtype="html",
    )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    log.info("report emailed to %s", settings.report_to)
    return True


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
