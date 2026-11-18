"""Replay the real trader, day by day, over history.

It runs the same Trader, strategies, LRS guard, loss limits, earnings blackout, ledger and cost
model as the live system, against a HistoricalBroker. So the backtest pays FX spreads,
waits for T+1 settlement, credits dividends net of 25% withholding, and halts on drawdowns
exactly as the paper run will.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from . import strategies
from .broker.historical import HistoricalBroker
from .config import Settings
from .earnings import EarningsCalendar, NoEarnings
from .ledger import Ledger, MemoryStore
from .marketdata import MarketData
from .runner import Trader
from .tax import TaxModel, after_tax, month_end_before


@dataclass
class ModeStats:
    mode: str
    start_usd: float
    end_usd: float
    inr_in: float
    inr_out: float
    cagr_usd: float
    cagr_inr: float
    max_dd: float
    vol: float
    ret_over_vol: float
    years: dict[int, float]
    fills: int
    fees: float
    div_gross: float
    div_withheld: float
    fx_friction_inr: float
    days_halted: int
    pauses: int
    earnings_blocks: int
    earnings_exits: int
    after_tax_inr: float = 0.0  # kept after Indian tax, sold and brought home at the end
    cagr_after_tax: float = 0.0
    tax_inr: float = 0.0
    stcg_inr: float = 0.0
    ltcg_inr: float = 0.0
    traded: list[str] = field(default_factory=list)
    curve: list[tuple[date, float, float]] = field(repr=False, default_factory=list)


@dataclass
class BacktestResult:
    start: date
    end: date
    stats: list[ModeStats]
    settings: dict
    caveats: list[str]
    failures: list[str]


def run_backtest(
    settings: Settings,
    data: MarketData,
    *,
    slippage_bps: float = 5.0,
    limits_enabled: bool = True,
    earnings: EarningsCalendar | None = None,
    tax_model: TaxModel | None = None,
    progress=None,
) -> BacktestResult:
    # An AI can't be replayed fairly: it has already read the news of those years. In history
    # the AI-veto mode would just copy its rules-only twin, so it's left out of backtests.
    ai_modes = [m for m in settings.modes if getattr(strategies.get(m), "ai_veto", False)]
    if ai_modes:
        settings = replace(settings, modes=tuple(m for m in settings.modes if m not in ai_modes))
    store = MemoryStore()
    brokers: dict[str, HistoricalBroker] = {}

    def broker_for(mode: str) -> HistoricalBroker:
        if mode not in brokers:
            brokers[mode] = HistoricalBroker(data, slippage_bps)
        return brokers[mode]

    day0 = data.calendar[0]
    state = {"day": day0}
    trader = Trader(
        settings,
        broker_for,
        store,
        today=day0,
        rate_fn=lambda: (data.rate(state["day"], settings.fallback_usdinr), "history"),
        fill_wait_seconds=0,
        earnings=earnings or NoEarnings(),
        limits_enabled=limits_enabled,
    )
    failures: list[str] = []
    for n, day in enumerate(data.calendar):
        state["day"] = day
        trader.set_day(day)
        for mode in settings.modes:
            broker_for(mode).set_clock(day, "open")
        for phase in ("premarket", "trade"):
            failures += [f"{day} {o.mode}/{phase}: {o.error}" for o in trader.run(phase) if not o.ok]
        for b in brokers.values():
            b.set_clock(day, "close")
        failures += [f"{day} {o.mode}/postclose: {o.error}" for o in trader.run("postclose") if not o.ok]
        if progress and n % 250 == 0:
            progress(day)

    tax_model = tax_model or TaxModel.from_env()
    rate_for = month_end_before(lambda d: data.rate(d, settings.fallback_usdinr))
    stats = [summarise(trader.ledger(m), tax_model, rate_for) for m in settings.modes]
    caveats = [
        "Paper results on history. Past returns, especially from momentum, don't repeat on schedule.",
        f"After-tax figures assume a {tax_model.slab_rate:.0%} slab (+{tax_model.cess:.0%} cess) on "
        f"short-term gains and dividends, {tax_model.ltcg_rate:.1%} on gains held over "
        f"{tax_model.lt_months} months, each year's tax paid out of the portfolio, and everything "
        "sold at the end. Set TAX_SLAB_RATE to your own rate. Estimates, not tax advice.",
        f"Fills at the next open with {slippage_bps:g} bps slippage; live orders go in at ~10:05 ET.",
        *(
            [
                f"Left out: {', '.join(ai_modes)}. An AI can't be backtested fairly (it has seen the "
                "news of those years); only the live paper run can judge it."
            ]
            if ai_modes
            else []
        ),
    ]
    if "aggressive" in settings.modes:
        caveats.append(
            "Aggressive's stock list (NVDA, META, AVGO, TSLA...) is today's mega-caps, picked with "
            "hindsight. That survivorship bias flatters its past returns: treat them as an upper bound."
        )
    if isinstance(earnings or NoEarnings(), NoEarnings):
        caveats.append("Earnings blackout was OFF (no earnings history loaded); live trading uses it.")
    if any(s.spliced_from for s in data.series.values()):
        spliced = ", ".join(f"{k}←{s.spliced_from}" for k, s in data.series.items() if s.spliced_from)
        caveats.append(f"Spliced history before launch: {spliced}.")
    missing = [
        sym
        for sym in sorted({u for m in settings.modes for u in strategies.get(m).universe})
        if sym not in data.series or data.series[sym].dates[0] > data.calendar[0]
    ]
    if missing:
        caveats.append("Shorter history (ignored until listed): " + ", ".join(missing))
    return BacktestResult(
        data.calendar[0],
        data.calendar[-1],
        stats,
        {
            "slippage_bps": slippage_bps,
            "limits": limits_enabled,
            "capital": settings.starting_capital_usd,
            "slab": tax_model.slab_rate,
        },
        caveats,
        failures,
    )


def summarise(ledger: Ledger, tax_model: TaxModel | None = None, rate_for=None) -> ModeStats:
    snaps = ledger.of_type("snapshot")
    start_usd, inr_in = ledger.deposited_usd(), ledger.deposited_inr()
    curve = [(date.fromisoformat(s["date"]), s["equity_usd"], s["inr_if_withdrawn"]) for s in snaps]
    end_usd = curve[-1][1] if curve else start_usd
    inr_out = curve[-1][2] if curve else inr_in
    years_n = max((curve[-1][0] - curve[0][0]).days / 365.25, 1 / 365.25) if curve else 1.0

    peak, max_dd = start_usd, 0.0
    rets = []
    prev = start_usd
    for _, eq, _ in curve:
        peak = max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1)
        rets.append(eq / prev - 1 if prev else 0.0)
        prev = eq
    vol = statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 2 else 0.0
    cagr = (end_usd / start_usd) ** (1 / years_n) - 1 if start_usd else 0.0
    cagr_inr = (inr_out / inr_in) ** (1 / years_n) - 1 if inr_in else 0.0

    years: dict[int, float] = {}
    base = start_usd
    for i, (d, eq, _) in enumerate(curve):
        last_of_year = i == len(curve) - 1 or curve[i + 1][0].year != d.year
        if last_of_year:
            years[d.year] = eq / base - 1
            base = eq

    fills = ledger.of_type("fill")
    div_gross, div_withheld = ledger.dividends()
    # Bank FX friction: what the spread cost on the way in, plus on the way out if withdrawn at the end.
    rate_in = ledger.weighted_deposit_rate()
    rate_out = snaps[-1]["rate"] if snaps else rate_in
    friction = (inr_in - start_usd * rate_in) + (end_usd * rate_out - inr_out)

    halted_days, halted = 0, False
    for e in ledger.entries:
        if e["type"] == "risk" and e["action"] in ("halt", "resume"):
            halted = e["action"] == "halt"
        elif e["type"] == "snapshot" and halted:
            halted_days += 1
    plans = ledger.of_type("plan")
    kept = after_tax(ledger, tax_model or TaxModel(), curve[-1][0], rate_for) if curve else None
    kept_inr = kept.value_inr if kept else inr_out
    return ModeStats(
        after_tax_inr=kept_inr,
        cagr_after_tax=(kept_inr / inr_in) ** (1 / years_n) - 1 if inr_in and kept_inr > 0 else 0.0,
        tax_inr=kept.tax_total_inr if kept else 0.0,
        stcg_inr=kept.stcg_inr if kept else 0.0,
        ltcg_inr=kept.ltcg_inr if kept else 0.0,
        mode=ledger.mode,
        start_usd=start_usd,
        end_usd=end_usd,
        inr_in=inr_in,
        inr_out=inr_out,
        cagr_usd=cagr,
        cagr_inr=cagr_inr,
        max_dd=max_dd,
        vol=vol,
        ret_over_vol=(cagr / vol) if vol else 0.0,
        years=years,
        fills=len(fills),
        fees=sum(f["fees"] for f in fills),
        div_gross=div_gross,
        div_withheld=div_withheld,
        fx_friction_inr=friction,
        days_halted=halted_days,
        pauses=sum(1 for e in ledger.of_type("risk") if e["action"] == "pause"),
        earnings_blocks=sum(1 for e in ledger.of_type("rejection") if e["reason"].startswith("earnings")),
        earnings_exits=sum(1 for p in plans if "out before earnings" in p.get("meta", {}).get("note", "")),
        traded=sorted({f["symbol"] for f in fills}),
        curve=curve,
    )


def format_result(r: BacktestResult) -> str:
    base = next((s for s in r.stats if s.mode == "baseline"), None)
    lines = [
        f"Backtest {r.start} → {r.end}  ·  ${r.settings['capital']:,.0f} per mode  ·  "
        f"slippage {r.settings['slippage_bps']:g} bps  ·  "
        f"loss limits {'on' if r.settings['limits'] else 'off'}",
        "",
        f"{'mode':<13}{'end $':>10}{'CAGR $':>8}{'CAGR ₹':>8}{'₹ after tax':>12}{'vs base':>8}"
        f"{'max DD':>8}{'vol':>7}{'ret/vol':>8}{'fills':>7}",
    ]
    for s in r.stats:
        vs = f"{(s.cagr_after_tax - base.cagr_after_tax) * 100:+.1f}" if base and s is not base else "—"
        lines.append(
            f"{s.mode:<13}{s.end_usd:>10,.0f}{s.cagr_usd * 100:>+8.1f}{s.cagr_inr * 100:>+8.1f}"
            f"{s.cagr_after_tax * 100:>+12.1f}{vs:>8}"
            f"{s.max_dd * 100:>8.1f}{s.vol * 100:>7.1f}{s.ret_over_vol:>8.2f}{s.fills:>7}"
        )
    lines += ["", f"Indian tax (slab {r.settings.get('slab', 0.30):.0%}, vs base = after-tax CAGR gap):"]
    for s in r.stats:
        share = s.tax_inr / (s.inr_out - s.inr_in) if s.inr_out > s.inr_in else 0.0
        lost = max(0.0, s.inr_out - s.after_tax_inr - s.tax_inr)
        lines.append(
            f"  {s.mode:<12} ₹{s.inr_out:,.0f} before tax → ₹{s.after_tax_inr:,.0f} kept · "
            f"tax ₹{s.tax_inr:,.0f} ({share:.0%} of the gain) + ₹{lost:,.0f} growth lost on tax paid "
            f"along the way · realised short-term ₹{s.stcg_inr:+,.0f}, long-term ₹{s.ltcg_inr:+,.0f}"
        )
    years = sorted({y for s in r.stats for y in s.years})
    lines += ["", "Calendar-year returns (USD, %):", f"{'mode':<13}" + "".join(f"{y:>7}" for y in years)]
    for s in r.stats:
        lines.append(f"{s.mode:<13}" + "".join(f"{s.years.get(y, float('nan')) * 100:>7.1f}" for y in years))
    lines += ["", "Costs and brakes:"]
    for s in r.stats:
        lines.append(
            f"  {s.mode:<12} ₹{s.inr_in:,.0f} in → ₹{s.inr_out:,.0f} out · "
            f"bank FX ₹{s.fx_friction_inr:,.0f} · "
            f"US fees ${s.fees:,.2f} · dividends ${s.div_gross:,.2f} (withheld ${s.div_withheld:,.2f}) · "
            f"{s.days_halted} days halted, {s.pauses} weekly pauses, {s.earnings_blocks} earnings blocks, "
            f"{s.earnings_exits} pre-earnings exits"
        )
    if r.failures:
        lines += ["", f"{len(r.failures)} phase failures, first few:"] + [f"  {f}" for f in r.failures[:5]]
    lines += ["", "Caveats:"] + [f"  - {c}" for c in r.caveats]
    return "\n".join(lines)


def write_outputs(r: BacktestResult, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    curve_path = out / "equity_curves.csv"
    days = sorted({d for s in r.stats for d, _, _ in s.curve})
    by_mode = {s.mode: {d: (usd, inr) for d, usd, inr in s.curve} for s in r.stats}
    with curve_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date"] + [f"{m}_usd" for m in by_mode] + [f"{m}_inr" for m in by_mode])
        for d in days:
            row = [d.isoformat()]
            row += [round(by_mode[m].get(d, (math.nan, math.nan))[0], 2) for m in by_mode]
            row += [round(by_mode[m].get(d, (math.nan, math.nan))[1], 2) for m in by_mode]
            w.writerow(row)
    summary_path = out / "summary.json"
    summary = {
        "start": r.start.isoformat(),
        "end": r.end.isoformat(),
        "settings": r.settings,
        "caveats": r.caveats,
        "modes": [{k: v for k, v in vars(s).items() if k != "curve"} for s in r.stats],
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    (out / "report.txt").write_text(format_result(r))
    return [curve_path, summary_path, out / "report.txt"]
