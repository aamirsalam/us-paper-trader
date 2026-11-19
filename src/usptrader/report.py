"""Morning report: is everything OK, who's ahead, and what happened today.

Layout, most important first:
  1. one status line (all OK / halts / failures / things to check)
  2. one comparison table: return in USD, what you'd keep in rupees after Indian tax, gap to baseline
  3. what happened today, read from the ledger (trades, AI verdicts, blocks, dividends, halts)
  4. one line per mode: holdings and anything unusual; zero-value details are left out
  5. footer: one-time costs, data sources, LRS tracker, disclaimer

Sent as HTML (inline styles, readable on a phone, no background colours so dark mode works)
with a plain-text twin for mail apps that don't render HTML.
"""

from __future__ import annotations

import html
import logging
import smtplib
from dataclasses import dataclass, field
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
MINUS = "−"


# ---- number formatting ----------------------------------------------------------------------------
def pct(x: float, places: int = 2) -> str:
    """+0.03% / −0.02% / 0.00% (no negative zero)."""
    v = round(x * 100, places)
    if v == 0:
        return f"{0:.{places}f}%"
    return f"{'+' if v > 0 else MINUS}{abs(v):.{places}f}%"


def pts(x: float) -> str:
    """Difference of two percentages, in percentage points."""
    v = round(x * 100, 2)
    if v == 0:
        return "0.00 pts"
    return f"{'+' if v > 0 else MINUS}{abs(v):.2f} pts"


def inr(x: float, signed: bool = False) -> str:
    """Indian digit grouping: ₹4,70,704 · +₹29 · −₹1,402."""
    n = round(abs(x))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    sign = ("+" if x > 0 else MINUS if x < 0 else "") if signed else (MINUS if x < 0 and n else "")
    return f"{sign}₹{s}"


def usd(x: float, cents: bool = True) -> str:
    return f"{MINUS if x < 0 else ''}${abs(x):,.{2 if cents else 0}f}"


# ---- data -------------------------------------------------------------------------------------------
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
        "entry_fee_inr": dep_inr - dep_usd * dep_rate,
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


@dataclass
class ModeView:
    mode: str
    virtual: bool
    m: dict
    kept_pct: float
    vs_base: float | None
    tax_total: float
    stcg: float
    risk_line: str
    halted: bool
    paused: bool
    notes: list[str] = field(default_factory=list)  # only things worth reading
    warnings: list[str] = field(default_factory=list)


@dataclass
class Report:
    subject: str
    text: str
    html: str


def _events_today(led: Ledger, today: date) -> list[str]:
    """What this mode actually did today, from its own ledger, shortest wording first."""
    out: list[str] = []
    fills = led.on("fill", today)
    for side, verb in (("buy", "bought"), ("sell", "sold")):
        items = [
            f"{f['symbol']} {usd(f['qty'] * f['price'], cents=False)}" for f in fills if f["side"] == side
        ]
        if items:
            out.append(f"{verb} " + ", ".join(items))
    plans = led.on("plan", today)
    reviews = plans[-1].get("meta", {}).get("ai_reviews", []) if plans else []
    approved = [r["symbol"] for r in reviews if r["decision"] == "buy"]
    if approved:
        out.append("AI approved " + ", ".join(approved))
    out += [f"AI vetoed {r['symbol']}: {r['reason']}" for r in reviews if r["decision"] == "veto"]
    down = [r["symbol"] for r in reviews if r["decision"] == "unavailable"]
    if down:
        out.append(f"AI unavailable for {', '.join(down)}: rules decided")
    for e in led.on("rejection", today):
        out.append(f"held back {e['side']} {e['symbol']}: {e['reason']}")
    for e in led.on("dividend", today):
        out.append(f"dividend {e['symbol']} {usd(e['gross'])} (US withheld {usd(e['withheld'])})")
    for e in led.on("risk", today):
        out.append({"halt": "HALTED", "pause": "paused for the week", "resume": "halt lifted"}[e["action"]]
                   + (f": {e['reason']}" if e.get("reason") else ""))  # fmt: skip
    return out


def collect(settings: Settings, ledgers: dict[str, Ledger], today: date) -> list[ModeView]:
    tax_model = TaxModel.from_env()
    views: list[ModeView] = []
    for mode in settings.modes:
        led = ledgers[mode]
        m = mode_metrics(led)
        if not m:
            continue
        t = after_tax(led, tax_model, today)
        kept = t.value_inr / m["inr_in"] - 1 if t and m["inr_in"] else m["pnl_inr_pct"]
        strat = strategies.get(mode)
        risk = status_line(led, limits_from_env(mode, strat.limits), today)
        v = ModeView(
            mode=mode,
            virtual=mode in settings.virtual_modes,
            m=m,
            kept_pct=kept,
            vs_base=None,
            tax_total=t.tax_total_inr if t else 0.0,
            stcg=t.stcg_inr if t else 0.0,
            risk_line=risk,
            halted=risk.startswith("⛔"),
            paused=risk.startswith("⏸"),
        )
        if round(v.tax_total) >= 1:
            v.notes.append(f"tax if sold today {inr(v.tax_total)}")
        if m["div_gross"] > 0:
            v.notes.append(f"dividends {usd(m['div_gross'])} (US withheld {usd(m['div_withheld'])})")
        earn = sum(1 for e in led.of_type("rejection") if e["reason"].startswith("earnings"))
        if earn:
            v.notes.append(f"{earn} buys held back for earnings")
        if getattr(strat, "ai_veto", False):
            reviews = [r for p in led.of_type("plan") for r in p.get("meta", {}).get("ai_reviews", [])]
            vetoes = [r["symbol"] for r in reviews if r["decision"] == "veto"]
            down = sum(1 for r in reviews if r["decision"] == "unavailable")
            note = f"AI: {len(reviews)} reviewed, {len(vetoes)} vetoed"
            note += f" ({', '.join(vetoes[-5:])})" if vetoes else ""
            v.notes.append(note)
            if down:
                v.warnings.append(f"{mode}: {down} buys went ahead without the AI (rules decided)")
        source = ((led.latest_plan() or {}).get("meta") or {}).get("earnings_source")
        if source == "unknown":
            v.warnings.append(f"{mode}: earnings dates unavailable, so no new single-stock buys")
        v.warnings += [f"{mode}: broker mismatch {mm}" for mm in m["mismatches"]]
        views.append(v)
    base = next((v for v in views if v.mode == "baseline"), None)
    for v in views:
        if base and v is not base:
            v.vs_base = v.kept_pct - base.kept_pct
    return views


def build_report(
    settings: Settings,
    store: LedgerStore,
    today: date,
    outcomes: list[ModeOutcome],
    ledgers: dict[str, Ledger] | None = None,
) -> Report:
    # Each ledger is read from the database once and reused for every section below.
    ledgers = ledgers or {m: Ledger(m, store, settings.costs) for m in settings.modes}
    views = collect(settings, ledgers, today)
    tax_model = TaxModel.from_env()

    failed = [o for o in outcomes if not o.ok]
    halted = [v.mode for v in views if v.halted]
    warnings = [w for v in views for w in v.warnings]
    events = {v.mode: _events_today(ledgers[v.mode], today) for v in views}
    days = max((v.m["days"] for v in views), default=0)
    leader = max(views, key=lambda v: v.m["pnl_pct"]) if views else None

    # status, most serious first
    status: list[tuple[str, str]] = []  # (level, text): level is "bad" | "warn" | "ok"
    for o in failed:
        status.append(("bad", f"{o.mode} {o.phase} FAILED: {o.error}"))
    for v in views:
        if v.halted:
            status.append(("bad", f"{v.mode}: {v.risk_line.lstrip('⛔ ')}"))
        elif v.paused:
            status.append(("warn", f"{v.mode}: {v.risk_line.lstrip('⏸ ')}"))
    status += [("warn", w) for w in warnings]
    if not status:
        status.append(("ok", f"All {len(views)} modes OK · no halts · no failed runs"))

    # subject: status first, then the leader by return in USD (the strategy itself; FX and tax
    # are the same story for every mode and live in the table)
    if views:
        spread = max(v.m["pnl_pct"] for v in views) - min(v.m["pnl_pct"] for v in views)
        lead = (
            f"leading: {leader.mode} {pct(leader.m['pnl_pct'])}" if spread >= 0.00005 else "all level so far"
        )
        state = "all OK" if status[0][0] == "ok" else "check report"
        subject = f"US paper · day {days} · {state} · {lead}"
    else:
        subject = f"US paper · {today:%d %b} · no results yet"
    if halted:
        subject = f"⛔ {', '.join(halted)} halted · " + subject
    if failed:
        subject = "⚠ " + subject

    fy = indian_fy(today)
    remitted = sum(e["inr"] for led in ledgers.values() for e in led.of_type("deposit") if e["fy"] == fy)
    first = views[0].m if views else None
    footer = []
    if first:
        fee_in = first["entry_fee_inr"]
        fee_out = max(0.0, -first["friction_inr"] - fee_in)
        footer.append(
            f"USD/INR {first['rate']:.2f}. Bank currency fees: {inr(fee_in)} paid on each mode's "
            f"{inr(first['inr_in'])} deposit, plus about {inr(fee_out)} to bring it home. Both are in the "
            "after-tax column, which is why every mode starts below zero there."
        )
    plans = [led.latest_plan() or {} for led in ledgers.values()]
    sources = sorted({p.get("meta", {}).get("earnings_source", "") for p in plans} - {""})
    if sources:
        footer.append("Earnings dates from: " + ", ".join(sources) + ".")
    footer.append(
        f"After tax = what you'd keep in rupees if you sold everything and brought it home today, "
        f"with Indian tax at a {tax_model.slab_rate:.0%} slab (+{tax_model.cess:.0%} cess). "
        "vs base is the gap to baseline in that column, in percentage points. Return is in USD."
    )
    if any(v.virtual for v in views):
        footer.append("* ledger-only: no broker account; fills simulated at the market price.")
    footer.append(
        f"If this were real: {fy} LRS remittances {inr(remitted)} of the ₹10,00,000 TCS-free limit. "
        "Report foreign holdings in Schedule FA; claim the US dividend tax with Form 67."
    )
    footer.append("Paper results. Tax figures are estimates, not advice: confirm with a CA.")

    title = f"US paper trader · {today:%a %d %b %Y} (ET) · day {days}"
    text = _render_text(title, status, views, leader, events, footer)
    body = _render_html(title, status, views, leader, events, footer)
    return Report(subject, text, body)


# ---- plain text -------------------------------------------------------------------------------------
def _render_text(title, status, views, leader, events, footer) -> str:
    marks = {"ok": "OK", "warn": "!!", "bad": "XX"}
    lines = [title, ""]
    lines += [f"{marks[level]}  {msg}" for level, msg in status]
    if views:
        lines += ["", f"{'Mode':<15}{'Return':>9}{'After tax':>11}{'vs base':>10}{'Worst':>8}"]
        for v in views:
            name = v.mode + ("*" if v.virtual else "")
            vs = pts(v.vs_base).removesuffix(" pts") if v.vs_base is not None else "—"
            lines.append(
                f"{name:<15}{pct(v.m['pnl_pct']):>9}{pct(v.kept_pct):>11}{vs:>10}{pct(v.m['max_dd'], 1):>8}"
            )
    lines += ["", "Today"]
    happened = [(mode, e) for mode, evs in events.items() for e in evs]
    if happened:
        lines += [f"  {mode}: {e}" for mode, e in happened]
    else:
        lines.append("  Quiet day: no trades.")
    if views:
        lines += ["", "Holdings"]
        for v in views:
            lines.append(f"  {v.mode}: {_holdings(v)}")
    lines += [""] + footer
    return "\n".join(lines)


def _holdings(v: ModeView) -> str:
    held = ", ".join(f"{s} {usd(x, cents=False)}" for s, x in sorted(v.m["holdings"].items())) or "all cash"
    bits = [f"{usd(v.m['equity_usd'])}: {held}", f"cash {usd(v.m['cash'], cents=False)}"]
    if not (v.halted or v.paused) and not v.risk_line.startswith("risk limits: none"):
        bits.append("limits OK")
    return " · ".join(bits + v.notes)


# ---- HTML ---------------------------------------------------------------------------------------------
INK, MUTED, RULE = "#1f2328", "#59636e", "#d1d9e0"
GOOD, BAD, WARN = "#1a7f37", "#cf222e", "#9a6700"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _signed_color(x: float | None) -> str:
    if x is None or round(x * 100, 2) == 0:
        return INK
    return GOOD if x > 0 else BAD


def _render_html(title, status, views, leader, events, footer) -> str:
    e = html.escape
    num = "text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;padding:7px 0 7px 10px"
    level_color = {"ok": GOOD, "warn": WARN, "bad": BAD}

    parts = [
        f'<div style="max-width:640px;margin:0 auto;padding:4px 0;font-family:{FONT};font-size:14px;'
        f'line-height:1.5;color:{INK}">',
        f'<div style="font-size:13px;color:{MUTED};margin-bottom:10px">{e(title)}</div>',
    ]
    for level, msg in status:
        parts.append(
            f'<div style="border-left:3px solid {level_color[level]};padding:2px 0 2px 10px;margin:0 0 6px;'
            f'font-weight:600">{e(msg)}</div>'
        )

    if views:
        head = "".join(
            f'<th style="{num if i else "text-align:left;padding:7px 0"};font-weight:600;color:{MUTED};'
            f'font-size:12px;border-bottom:1px solid {RULE}">{h}</th>'
            for i, h in enumerate(["Mode", "Return", "After tax", "vs base", "Worst"])
        )
        rows = []
        for v in views:
            weight = "font-weight:600;" if leader and v is leader else ""
            name = e(v.mode) + (f'<span style="color:{MUTED}">*</span>' if v.virtual else "")
            vs = pts(v.vs_base).removesuffix(" pts") if v.vs_base is not None else "—"
            cells = [
                _td(name, f"padding:7px 0;{weight}"),
                _td(pct(v.m["pnl_pct"]), f"{num};{weight}color:{_signed_color(v.m['pnl_pct'])}"),
                _td(pct(v.kept_pct), f"{num};color:{_signed_color(v.kept_pct)}"),
                _td(vs, f"{num};color:{_signed_color(v.vs_base)}"),
                _td(pct(v.m["max_dd"], 1), f"{num};color:{MUTED}"),
            ]
            rows.append("<tr>" + "".join(cells) + "</tr>")
        parts.append(
            _table("width:100%;margin:14px 0 4px")
            + f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )

    parts.append(_section("Today"))
    happened = [(mode, ev) for mode, evs in events.items() for ev in evs]
    if happened:
        parts.append(
            '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">'
        )
        for mode, ev in happened:
            parts.append(
                f'<tr><td style="padding:2px 14px 2px 0;color:{MUTED};white-space:nowrap;vertical-align:top">'
                f'{e(mode)}</td><td style="padding:2px 0">{e(ev)}</td></tr>'
            )
        parts.append("</table>")
    else:
        parts.append(f'<div style="font-size:13px;color:{MUTED}">Quiet day: no trades.</div>')

    if views:
        parts.append(_section("Holdings"))
        parts.append(
            '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">'
        )
        for v in views:
            parts.append(
                f'<tr><td style="padding:2px 14px 2px 0;color:{MUTED};white-space:nowrap;vertical-align:top">'
                f'{e(v.mode)}</td><td style="padding:2px 0">{e(_holdings(v))}</td></tr>'
            )
        parts.append("</table>")

    parts.append(f'<div style="margin-top:22px;padding-top:10px;border-top:1px solid {RULE};font-size:12px;'
                 f'color:{MUTED}">')  # fmt: skip
    parts += [f'<p style="margin:0 0 6px">{e(line)}</p>' for line in footer]
    parts.append("</div></div>")
    return "".join(parts)


def _td(content: str, style: str) -> str:
    return f'<td style="{style};border-bottom:1px solid {RULE}">{content}</td>'


def _table(extra: str = "") -> str:
    return f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;{extra}">'


def _section(name: str) -> str:
    return (
        f'<div style="margin:18px 0 6px;font-size:12px;font-weight:600;letter-spacing:.04em;'
        f'text-transform:uppercase;color:{MUTED}">{name}</div>'
    )


# ---- sending ------------------------------------------------------------------------------------------
def send(settings: Settings, report: Report) -> bool:
    """Email via SMTP (e.g. Gmail with an app password). Prints instead if SMTP isn't configured."""
    if not (settings.smtp_host and settings.report_to):
        print(f"{report.subject}\n\n{report.text}")
        return False
    msg = EmailMessage()
    msg["Subject"] = report.subject
    msg["From"] = settings.report_from
    msg["To"] = settings.report_to
    msg.set_content(report.text)
    msg.add_alternative(f"<!doctype html><html><body style=\"margin:0\">{report.html}</body></html>",
                        subtype="html")  # fmt: skip
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    log.info("report emailed to %s", settings.report_to)
    return True
