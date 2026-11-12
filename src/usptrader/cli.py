"""usptrader <command> [options]

usptrader premarket                       decide today's targets (with earnings blackout)
usptrader trade                           place guarded orders (loss limits applied)
usptrader postclose                       sync fills, dividends, snapshot, email report
usptrader report                          rebuild and print/email the report from the ledger
usptrader resume --mode aggressive        lift a drawdown halt early
usptrader backtest --start 2016-01-01     replay all modes over real history (Yahoo, cached)
usptrader simulate --days 30              offline smoke run of all phases on a fake broker
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta

from .config import Settings
from .earnings import NoEarnings
from .ledger import DynamoStore, Ledger, LedgerStore, LocalStore
from .report import build_report, send
from .runner import PHASES, Trader, today_et


def _store(settings: Settings) -> LedgerStore:
    if settings.ledger_backend == "dynamodb":
        return DynamoStore(settings.ledger_table, settings.aws_region)
    return LocalStore(settings.local_ledger_dir)


def _alpaca_factory(settings: Settings):
    from .broker.alpaca_paper import AlpacaPaperBroker

    cache: dict = {}

    def make(mode: str):
        if mode not in cache:
            cache[mode] = AlpacaPaperBroker(*settings.alpaca_keys(mode))
        return cache[mode]

    return make


def simulate(settings: Settings, days: int, start: date) -> None:
    """Run every phase on consecutive weekdays against FakeBroker, using a local ledger."""
    from .broker.fake import FakeBroker

    store = _store(settings)
    brokers: dict[str, FakeBroker] = {}
    trader = Trader(
        settings,
        lambda m: brokers.setdefault(m, FakeBroker(settings.starting_capital_usd, today=start)),
        store,
        today=start,
        rate_fn=lambda: (settings.fallback_usdinr, "fixed"),
        fill_wait_seconds=0,
        earnings=NoEarnings(),
    )
    day, done, outcomes = start, 0, []
    while done < days:
        if day.weekday() < 5:
            for b in brokers.values():
                b._today = day
                b.advance()
            trader.set_day(day)
            for phase in PHASES:
                outcomes = trader.run(phase)
            done += 1
        day += timedelta(days=1)
    subject, body = build_report(settings, store, day - timedelta(days=1), outcomes)
    print(f"{subject}\n\n{body}")


def backtest(settings: Settings, args) -> int:
    from . import strategies
    from .backtest import format_result, run_backtest, write_outputs
    from .earnings import HistoricalEarnings, SecEarnings
    from .marketdata import load_yahoo, synthetic

    end = args.end or today_et() - timedelta(days=1)
    universe = sorted({s for m in settings.modes for s in strategies.get(m).universe})
    if args.source == "synthetic":
        data = synthetic(universe, args.start, end)
    else:
        data = load_yahoo(universe, args.start, end, args.cache)

    singles = sorted({s for m in settings.modes for s in strategies.get(m).single_stocks})
    choice = args.earnings
    if choice == "auto":
        choice = "sec" if os.environ.get("SEC_USER_AGENT") and singles else "none"
    if choice == "none" or not singles:
        earnings = NoEarnings()
    elif choice == "sec":
        sec = SecEarnings(cache_dir=os.path.join(args.cache, "earnings"))
        earnings = HistoricalEarnings.from_sec(singles, sec)
    else:
        earnings = HistoricalEarnings.from_csv(choice)

    result = run_backtest(
        settings,
        data,
        slippage_bps=args.slippage_bps,
        limits_enabled=not args.no_limits,
        earnings=earnings,
        progress=lambda d: logging.info("backtest at %s", d),
    )
    print(format_result(result))
    if args.out:
        for path in write_outputs(result, args.out):
            print(f"wrote {path}")
    return 0 if not result.failures else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="usptrader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("command", choices=[*PHASES, "report", "resume", "backtest", "simulate"])
    p.add_argument("--date", type=date.fromisoformat, help="override the trading date (ET)")
    p.add_argument("--mode", help="mode for `resume`")
    p.add_argument("--days", type=int, default=30, help="days to simulate")
    p.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 4), help="backtest start")
    p.add_argument("--end", type=date.fromisoformat, help="backtest end (default yesterday)")
    p.add_argument("--source", choices=["yahoo", "synthetic"], default="yahoo")
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--no-limits", action="store_true", help="backtest without loss limits")
    p.add_argument(
        "--earnings",
        default="auto",
        help="auto | sec | none | path/to/earnings.csv (symbol,date). auto = sec if SEC_USER_AGENT is set",
    )
    p.add_argument("--cache", default="./data/cache", help="backtest download cache")
    p.add_argument("--out", help="write equity curves, summary.json and report.txt here")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    today = args.date or today_et()

    if args.command == "simulate":
        simulate(settings, args.days, today - timedelta(days=int(args.days * 1.5)))
        return 0
    if args.command == "backtest":
        return backtest(settings, args)

    store = _store(settings)
    if args.command == "resume":
        if args.mode not in settings.modes:
            p.error(f"--mode must be one of {', '.join(settings.modes)}")
        ledger = Ledger(args.mode, store, settings.costs)
        snaps = ledger.of_type("snapshot")
        ref = snaps[-1]["equity_usd"] if snaps else ledger.deposited_usd()
        ledger.record_risk(today, "resume", equity_ref=ref, auto=False)
        print(f"{args.mode}: halt lifted, drawdown peak reset to ${ref:,.2f}")
        return 0

    outcomes = []
    if args.command in PHASES:
        trader = Trader(settings, _alpaca_factory(settings), store, today=today)
        outcomes = trader.run(args.command)
        for o in outcomes:
            logging.info(
                "[%s/%s] %s %s", o.mode, o.phase, "ok" if o.ok else "FAILED " + o.error, " | ".join(o.lines)
            )
    if args.command in ("postclose", "report"):
        subject, body = build_report(settings, store, today, outcomes)
        send(settings, subject, body)
    return 0 if all(o.ok for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
