"""usptrader <command> [--date YYYY-MM-DD]

usptrader premarket            decide today's targets
usptrader trade                place guarded orders
usptrader postclose            sync fills, dividends, snapshot, email report
usptrader report               rebuild and print/email the report from the ledger
usptrader simulate --days 30   offline smoke run of all phases on a fake broker
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from .config import Settings
from .ledger import DynamoStore, LedgerStore, LocalStore
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
    day, done = start, 0
    while done < days:
        if day.weekday() < 5:
            for b in brokers.values():
                b._today = day
                b.advance()
            trader = Trader(
                settings,
                lambda m, d=day: brokers.setdefault(m, FakeBroker(settings.starting_capital_usd, today=d)),
                store,
                today=day,
                rate_fn=lambda: (settings.fallback_usdinr, "fixed"),
                fill_wait_seconds=0,
            )
            outcomes = []
            for phase in PHASES:
                outcomes = trader.run(phase)
            done += 1
        day += timedelta(days=1)
    subject, body = build_report(settings, store, day - timedelta(days=1), outcomes)
    print(f"{subject}\n\n{body}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="usptrader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("command", choices=[*PHASES, "report", "simulate"])
    p.add_argument("--date", type=date.fromisoformat, help="override the trading date (ET)")
    p.add_argument("--days", type=int, default=30, help="days to simulate")
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

    store = _store(settings)
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
