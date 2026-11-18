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
from .tax import TaxModel, after_tax

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
    settings: Settings,
    store: LedgerStore,
    today: date,
    outcomes: list[ModeOutcome],
    ledgers: dict[str, Ledger] | None = None,
) -> tuple[str, str]:
    # Each ledger is read from the database once and reused for every section below.
    ledgers = ledgers or {m: Ledger(m, store, settings.costs) for m in settings.modes}
    tax_model = TaxModel.from_env()
    metrics = []
    for mode in settings.modes:
        m = mode_metrics(ledgers[mode])
        if m:
            m["tax"] = after_tax(ledgers[mode], tax_model, today)
            m["kept_pct"] = m["tax"].value_inr / m["inr_in"] - 1 if m["tax"] and m["inr_in"] else 0.0
            metrics.append(m)
    base = next((m for m in metrics if m["mode"] == "baseline"), None)
    halted: list[str] = []

    lines = [f"US paper trader, {today:%a %d %b %Y} (ET)", ""]
    if metrics:
        lines.append(f"USD/INR {metrics[0]['rate']:.2f}  ·  running {max(m['days'] for m in metrics)} days")
        lines.append("")
        lines.append(
            f"{'mode':<13}{'equity $':>10}{'USD %':>8}{'INR %':>8}{'after tax %':>13}"
            f"{'vs base':>9}{'max DD':>8}"
        )
        for m in metrics:
            vs = f"{(m['kept_pct'] - base['kept_pct']) * 100:+.1f}" if base and m is not base else "—"
            lines.append(
                f"{m['mode']:<13}{m['equity_usd']:>10,.2f}{m['pnl_pct'] * 100:>+8.1f}"
                f"{m['pnl_inr_pct'] * 100:>+8.1f}{m['kept_pct'] * 100:>+13.1f}{vs:>9}"
                f"{m['max_dd'] * 100:>8.1f}"
            )
        lines.append(
            f"after tax = sold and brought home today, Indian tax at a {tax_model.slab_rate:.0%} slab "
            f"(+{tax_model.cess:.0%} cess); vs base compares that column"
        )
        lines.append("")
        for m in metrics:
            led = ledgers[m["mode"]]
            lines.append(
                f"[{m['mode']}] ₹{m['inr_in']:,.0f} in → ₹{m['inr_back']:,.0f} if withdrawn today "
                f"(strategy ₹{m['strategy_inr']:+,.0f}, rupee ₹{m['fx_inr']:+,.0f}, "
                f"bank FX ₹{m['friction_inr']:+,.0f})"
            )
            t = m["tax"]
            if t:
                lines.append(
                    f"    after tax ₹{t.value_inr:,.0f}: tax ₹{t.tax_total_inr:,.0f} "
                    f"(past years ₹{t.tax_paid_inr:,.0f}, this year so far ₹{t.tax_accrued_inr:,.0f}, "
                    f"on selling now ₹{t.tax_on_exit_inr:,.0f}); realised gains short-term "
                    f"₹{t.stcg_inr:+,.0f}, long-term ₹{t.ltcg_inr:+,.0f}"
                )
            held = ", ".join(f"{s} ${v:,.0f}" for s, v in sorted(m["holdings"].items())) or "none"
            lines.append(f"    holdings: {held}; cash ${m['cash']:,.2f}")
            lines.append(
                f"    {m['trades']} fills, {m['blocked']} orders blocked or rejected, dividends "
                f"${m['div_gross']:.2f} gross / ${m['div_withheld']:.2f} withheld"
            )
            mode_limits = limits_from_env(m["mode"], strategies.get(m["mode"]).limits)
            risk_line = status_line(led, mode_limits, today)
            lines.append("    " + risk_line)
            if risk_line.startswith("⛔"):
                halted.append(m["mode"])
            plan = led.latest_plan()
            source = (plan or {}).get("meta", {}).get("earnings_source")
            if source:
                flag = "⚠ " if source == "unknown" else ""
                lines.append(f"    {flag}earnings dates from: {source}")
            earn = sum(1 for e in led.of_type("rejection") if e["reason"].startswith("earnings"))
            if earn:
                lines.append(f"    {earn} buys held back by the earnings blackout so far")
            if getattr(strategies.get(m["mode"]), "ai_veto", False):
                reviews = [r for p in led.of_type("plan") for r in p.get("meta", {}).get("ai_reviews", [])]
                if reviews:
                    vetoes = [r["symbol"] for r in reviews if r["decision"] == "veto"]
                    down = sum(1 for r in reviews if r["decision"] == "unavailable")
                    lines.append(
                        f"    AI reviews so far: {len(reviews)}, vetoed {len(vetoes)}"
                        + (f" ({', '.join(vetoes[-5:])})" if vetoes else "")
                        + (f", ⚠ {down} without AI (rules decided)" if down else "")
                    )
                else:
                    lines.append("    AI reviews so far: none (no new single-stock buys yet)")
            if m["mode"] in settings.virtual_modes:
                lines.append("    ledger-only mode: fills simulated at market price, no broker account")
            for mm in m["mismatches"]:
                lines.append(f"    ⚠ broker mismatch {mm}")
    lines += ["", "Today:"]
    for o in outcomes:
        status = "OK" if o.ok else f"FAILED: {o.error}"
        lines.append(f"  [{o.mode}] {status}")
        lines.extend(f"     - {x}" for x in o.lines)

    fy = indian_fy(today)
    total_inr = sum(e["inr"] for led in ledgers.values() for e in led.of_type("deposit") if e["fy"] == fy)
    lines += [
        "",
        f"If this were real: {fy} LRS remittances ₹{total_inr:,.0f} of the ₹10,00,000 TCS-free limit. "
        "Report foreign holdings in Schedule FA; claim the US dividend tax with Form 67.",
        "Paper results. Tax figures are estimates, not advice: confirm with a CA.",
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
