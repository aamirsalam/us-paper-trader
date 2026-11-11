"""The three daily phases. Each is idempotent, so a re-run on the same day is harmless.

premarket  ~08:45 ET  decide target weights from yesterday's closes, store the plan
trade      ~10:05 ET  turn the plan into guarded orders and submit them
postclose  ~16:30 ET  sync fills, credit dividends, snapshot, email the report
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import strategies
from .broker.base import Broker
from .compliance import ComplianceGuard
from .config import Settings
from .fx import usd_inr
from .ledger import Ledger, LedgerStore
from .rebalance import plan_orders

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
PHASES = ("premarket", "trade", "postclose")


def today_et() -> date:
    return datetime.now(ET).date()


@dataclass
class ModeOutcome:
    mode: str
    phase: str
    ok: bool = True
    lines: list[str] = field(default_factory=list)
    error: str = ""


class Trader:
    def __init__(
        self,
        settings: Settings,
        broker_for: Callable[[str], Broker],
        store: LedgerStore,
        *,
        today: date | None = None,
        rate_fn: Callable[[], tuple[float, str]] | None = None,
        fill_wait_seconds: int = 90,
    ) -> None:
        self.settings = settings
        self.broker_for = broker_for
        self.store = store
        self.today = today or today_et()
        self.rate_fn = rate_fn or (lambda: usd_inr(settings.fallback_usdinr))
        self.fill_wait_seconds = fill_wait_seconds
        self.guard = ComplianceGuard()
        self._rate: tuple[float, str] | None = None

    def rate(self) -> tuple[float, str]:
        if self._rate is None:
            self._rate = self.rate_fn()
        return self._rate

    # ---- entry point --------------------------------------------------------------------------
    def run(self, phase: str) -> list[ModeOutcome]:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}")
        outcomes: list[ModeOutcome] = []
        for mode in self.settings.modes:
            out = ModeOutcome(mode, phase)
            try:
                broker = self.broker_for(mode)
                if not broker.is_trading_day(self.today):
                    out.lines.append(f"{self.today} is not a US trading day, nothing to do")
                    outcomes.append(out)
                    continue
                ledger = Ledger(mode, self.store, self.settings.costs)
                strategy = strategies.get(mode)
                self._ensure_funded(ledger, broker, out)
                getattr(self, f"_{phase}")(ledger, broker, strategy, out)
            except Exception as exc:  # one broken mode must not stop the others
                log.exception("%s/%s failed", mode, phase)
                out.ok, out.error = False, f"{type(exc).__name__}: {exc}"
            outcomes.append(out)
        return outcomes

    # ---- phases -------------------------------------------------------------------------------
    def _ensure_funded(self, ledger: Ledger, broker: Broker, out: ModeOutcome) -> None:
        if ledger.of_type("deposit"):
            return
        changed = broker.lock_down_account()
        if changed:
            out.lines.append("broker account locked to cash-only: " + ", ".join(changed))
        rate, src = self.rate()
        dep = ledger.record_deposit(self.today, self.settings.starting_capital_usd, rate)
        out.lines.append(
            f"funded ${dep['usd']:,.2f} at {rate:.2f} ({src}) = ₹{dep['inr']:,.0f} incl. FX spread"
        )

    def _premarket(self, ledger, broker, strategy, out) -> None:
        if ledger.on("plan", self.today):
            out.lines.append("plan already made today")
            return
        closes = broker.daily_closes(list(strategy.universe), strategy.lookback_days)
        prev = ledger.latest_plan()
        result = strategy.decide(closes, self.today, prev["state"] if prev else {})
        ledger.record_plan(self.today, result.targets, {**result.state, "note": result.note})
        weights = ", ".join(f"{s} {w:.0%}" for s, w in sorted(result.targets.items()))
        out.lines.append(f"plan: {weights} ({result.note})")

    def _trade(self, ledger, broker, strategy, out) -> None:
        self._sync_orders(ledger, broker, out)
        if ledger.on("order", self.today):
            out.lines.append("orders already placed today")
            return
        plans = ledger.on("plan", self.today)
        if not plans:
            self._premarket(ledger, broker, strategy, out)
            plans = ledger.on("plan", self.today)
        targets = plans[-1]["targets"]

        positions = ledger.positions()
        symbols = sorted(set(targets) | set(positions))
        prices = broker.latest_prices(symbols)
        equity = ledger.cash() + sum(p.qty * prices.get(s, 0) for s, p in positions.items())
        intents = plan_orders(
            targets,
            positions,
            prices,
            equity,
            drift_threshold=strategy.drift_threshold,
            min_trade=strategy.min_trade,
        )
        if not intents:
            out.lines.append("on target, no orders")
            return

        available = min(ledger.settled_cash(self.today), broker.cash_available())
        placed: list[str] = []
        for intent in intents:
            decision = self.guard.check(intent, positions=positions, available_cash=available)
            if not decision.approved:
                ledger.record_rejection(self.today, intent, decision.reason)
                out.lines.append(f"blocked {intent.side} {intent.symbol}: {decision.reason}")
                continue
            order = decision.intent
            cid = f"{ledger.mode}-{self.today:%Y%m%d}-{order.symbol}-{order.side}"
            oid = broker.submit(order, cid)
            ledger.record_order(self.today, oid, order)
            if order.side == "buy":
                available -= order.notional
            size = f"${order.notional:,.2f}" if order.notional else f"{order.qty:.4f} sh"
            note = f" ({decision.reason})" if decision.adjusted else ""
            out.lines.append(f"{order.side} {order.symbol} {size} [{order.reason}]{note}")
            placed.append(oid)

        deadline = time.monotonic() + self.fill_wait_seconds
        while ledger.open_orders() and time.monotonic() < deadline:
            self._sync_orders(ledger, broker, out, quiet=True)
            if ledger.open_orders():
                time.sleep(3)
        self._sync_orders(ledger, broker, out)

    def _postclose(self, ledger, broker, strategy, out) -> None:
        self._sync_orders(ledger, broker, out)
        self._credit_dividends(ledger, broker, out)

        positions = ledger.positions()
        prices = broker.latest_prices(sorted(positions)) if positions else {}
        mkt = {s: p.qty * prices.get(s, 0.0) for s, p in positions.items()}
        cash = ledger.cash()
        equity = cash + sum(mkt.values())
        rate, src = self.rate()

        broker_pos = broker.positions()
        ours = {s: p.qty for s, p in positions.items()}
        mismatches = [
            f"{s}: ledger {ours.get(s, 0):.6f} vs broker {broker_pos.get(s, 0):.6f}"
            for s in sorted(set(ours) | set(broker_pos))
            if abs(ours.get(s, 0) - broker_pos.get(s, 0)) > 1e-4
        ]
        if not ledger.on("snapshot", self.today):
            ledger.record_snapshot(
                self.today,
                equity_usd=round(equity, 4),
                cash_usd=round(cash, 4),
                settled_usd=round(ledger.settled_cash(self.today), 4),
                holdings={s: round(v, 4) for s, v in mkt.items()},
                rate=rate,
                rate_source=src,
                inr_if_withdrawn=round(self.settings.costs.inr_on_withdrawal(equity, rate), 2),
                mismatches=mismatches,
            )
        out.lines.append(f"equity ${equity:,.2f} (cash ${cash:,.2f})")
        out.lines.extend(f"RECON {m}" for m in mismatches)

    # ---- helpers ------------------------------------------------------------------------------
    def _sync_orders(self, ledger: Ledger, broker: Broker, out: ModeOutcome, quiet: bool = False) -> None:
        for order in ledger.open_orders():
            status, fill = broker.order_fill(order["order_id"])
            if fill and status in ("filled", "dead"):
                ledger.record_fill(fill, broker.next_trading_day(fill.filled_on))
                if not quiet:
                    out.lines.append(f"filled {fill.side} {fill.symbol} {fill.qty:.4f} @ ${fill.price:,.2f}")
            if status == "dead":
                ledger.close_order(self.today, order["order_id"], "order ended unfilled or partly filled")
                if not quiet:
                    out.lines.append(f"order {order['symbol']} {order['side']} ended without full fill")

    def _credit_dividends(self, ledger: Ledger, broker: Broker, out: ModeOutcome) -> None:
        held_ever = sorted({e["symbol"] for e in ledger.of_type("fill")})
        if not held_ever:
            return
        for div in broker.cash_dividends(held_ever, self.today - timedelta(days=60), self.today):
            if div.payable_date > self.today:
                continue
            qty = ledger.positions(as_of=div.ex_date - timedelta(days=1)).get(div.symbol)
            if not qty:
                continue
            entry = ledger.record_dividend(div.action_id, div.symbol, qty.qty, div.rate, div.payable_date)
            if entry:
                out.lines.append(
                    f"dividend {div.symbol}: ${entry['gross']:.2f} gross, "
                    f"${entry['withheld']:.2f} US withholding, ${entry['net']:.2f} net"
                )
