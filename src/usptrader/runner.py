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
from .earnings import EarningsCalendar, live_calendar
from .fx import usd_inr
from .ledger import EPS, Ledger, LedgerStore
from .models import OrderIntent
from .rebalance import plan_orders
from .risk import NO_LIMITS, RiskLimits, RiskState, assess, limits_from_env
from .strategies import CASH_PROXY, StrategyContext, StrategyResult
from .strategies.base import sma, total_return

_FROM_ENV = object()

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
PHASES = ("premarket", "trade", "postclose")
BLACKOUT_TRADING_DAYS = 5  # no single-stock buys if it reports within this many trading days


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
        earnings: EarningsCalendar | None = None,
        limits_enabled: bool = True,
        reviewer=_FROM_ENV,
    ) -> None:
        self.settings = settings
        self.broker_for = broker_for
        self.store = store
        self.today = today or today_et()
        self.rate_fn = rate_fn or (lambda: usd_inr(settings.fallback_usdinr))
        self.fill_wait_seconds = fill_wait_seconds
        self.guard = ComplianceGuard()
        self.limits_enabled = limits_enabled
        self._earnings = earnings
        self._reviewer = reviewer
        self._rate: tuple[float, str] | None = None
        self._ledgers: dict[str, Ledger] = {}

    def set_day(self, day: date) -> None:
        """Move to another trading date (backtests reuse one Trader for speed)."""
        self.today = day
        self._rate = None

    @property
    def earnings(self) -> EarningsCalendar:
        if self._earnings is None:
            self._earnings = live_calendar()
        return self._earnings

    @property
    def reviewer(self):
        """The AI reviewer for ai_veto modes, built from GEMINI_API_KEY on first use (None if unset)."""
        if self._reviewer is _FROM_ENV:
            from .ai import GeminiReviewer

            self._reviewer = GeminiReviewer.from_env()
        return self._reviewer

    def ledgers(self) -> dict[str, Ledger]:
        return {m: self.ledger(m) for m in self.settings.modes}

    def ledger(self, mode: str) -> Ledger:
        if mode not in self._ledgers:
            self._ledgers[mode] = Ledger(mode, self.store, self.settings.costs)
        return self._ledgers[mode]

    def limits(self, strategy) -> RiskLimits:
        if not self.limits_enabled:
            return NO_LIMITS
        return limits_from_env(strategy.name, strategy.limits)

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
                ledger = self.ledger(mode)
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
        try:  # a refused setting must not stop the mode: our own guard enforces the same rules
            changed = broker.lock_down_account()
            if changed:
                out.lines.append("broker account locked to cash-only: " + ", ".join(changed))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: could not apply cash-only settings at the broker: %s", ledger.mode, exc)
            out.lines.append(f"⚠ broker refused cash-only settings ({exc}); relying on the LRS guard")
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
        context, meta = self._earnings_context(ledger, broker, strategy, out)
        result = strategy.decide(closes, self.today, prev["state"] if prev else {}, context)
        if getattr(strategy, "ai_veto", False):
            result = self._ai_veto(ledger, broker, strategy, result, closes, meta, out)
        ledger.record_plan(self.today, result.targets, result.state, {**meta, "note": result.note})
        weights = ", ".join(f"{s} {w:.0%}" for s, w in sorted(result.targets.items()))
        out.lines.append(f"plan: {weights} ({result.note})")

    def _ai_veto(self, ledger, broker, strategy, result, closes, meta, out) -> StrategyResult:
        """Ask the AI about each single stock the plan would newly buy; drop the ones it vetoes.

        Only removes buys (the slot goes to T-bills), never adds or sells anything. Each stock is
        reviewed once per rebalance period: approvals are remembered in the strategy state.
        """
        held = set(ledger.positions())
        state = dict(result.state)
        approved = set(state.get("ai_ok", []))
        todo = [
            s
            for s, w in sorted(result.targets.items())
            if w > 0 and s in strategy.single_stocks and s not in held and s not in approved
        ]
        if not todo:
            return result
        from .ai import Review, StockBrief

        reasons = meta.get("earnings_reason", {})
        reviews: list[Review] = []
        for sym in todo:
            series = closes.get(sym, [])
            last = series[-1] if series else None
            avg50, avg200 = sma(series, 50), sma(series, 200)
            try:
                headlines = broker.news(sym, 14)
            except Exception as exc:  # noqa: BLE001  review without news rather than skip it
                log.warning("%s: news for %s unavailable: %s", ledger.mode, sym, exc)
                headlines = []
            brief = StockBrief(
                symbol=sym,
                ret_1m=total_return(series, 21),
                ret_3m=total_return(series, 63),
                ret_6m=total_return(series, 126),
                vs_50d=last / avg50 - 1 if last and avg50 else None,
                vs_200d=last / avg200 - 1 if last and avg200 else None,
                earnings=reasons.get(sym, "no report within the next 5 trading days"),
                headlines=headlines,
            )
            reviewer = self.reviewer
            if reviewer is None:
                review = Review(sym, "unavailable", 0.0, "GEMINI_API_KEY not set")
            else:
                review = reviewer.review(brief)
            reviews.append(review)

        targets = dict(result.targets)
        vetoed = [r.symbol for r in reviews if r.vetoed]
        for sym in vetoed:
            weight = targets.pop(sym, 0.0)
            targets[CASH_PROXY] = round(targets.get(CASH_PROXY, 0.0) + weight, 10)
        if "picks" in state:
            state["picks"] = [s for s in state["picks"] if s not in vetoed]
        state["ai_ok"] = sorted(approved | {r.symbol for r in reviews if r.decision == "buy"})
        meta["ai_reviews"] = [r.to_meta() for r in reviews]

        for r in reviews:
            tag = {"veto": "AI VETO", "buy": "AI ok", "unavailable": "AI unavailable, rules decide"}
            tag = tag[r.decision]
            conf = f" ({r.confidence:.0%})" if r.decision != "unavailable" else ""
            out.lines.append(f"{tag} {r.symbol}{conf}: {r.reason}")
        note = result.note + (f"; AI vetoed {', '.join(vetoed)}" if vetoed else "")
        return StrategyResult(targets, state, note)

    def _earnings_context(self, ledger, broker, strategy, out) -> tuple[StrategyContext, dict]:
        singles = sorted(set(strategy.single_stocks))
        if not singles:
            return StrategyContext(), {}
        end = self.today
        for _ in range(BLACKOUT_TRADING_DAYS):
            end = broker.next_trading_day(end)
        tomorrow = broker.next_trading_day(self.today)
        try:
            events = self.earnings.upcoming(singles, self.today, end)
        except Exception as exc:  # noqa: BLE001  fail safe: no new single-stock buys today
            out.lines.append(
                f"⚠ earnings dates unavailable ({exc}); no new single-stock buys today. "
                "Set SEC_USER_AGENT to enable the SEC fallback."
            )
            return StrategyContext(blackout=frozenset(singles)), {
                "earnings_blackout": singles,
                "earnings_reason": {s: "earnings dates unavailable" for s in singles},
                "earnings_source": "unknown",
            }
        blackout = sorted(events)
        exit_now = sorted(s for s, e in events.items() if e.confirmed and e.day <= tomorrow)
        reasons = {
            s: f"reports {e.day:%a %d %b}{'' if e.confirmed else ' (estimated)'}" for s, e in events.items()
        }
        if blackout:
            out.lines.append("earnings blackout: " + ", ".join(f"{s} {reasons[s]}" for s in blackout))
        source = getattr(self.earnings, "source", type(self.earnings).__name__)
        meta = {"earnings_blackout": blackout, "earnings_reason": reasons, "earnings_source": source}
        return StrategyContext(frozenset(blackout), frozenset(exit_now)), meta

    def _trade(self, ledger, broker, strategy, out) -> None:
        self._sync_orders(ledger, broker, out)
        if ledger.on("order", self.today):
            out.lines.append("orders already placed today")
            return
        plans = ledger.on("plan", self.today)
        if not plans:
            self._premarket(ledger, broker, strategy, out)
            plans = ledger.on("plan", self.today)
        plan = plans[-1]
        targets = plan["targets"]
        meta = plan.get("meta", {})

        positions = ledger.positions()
        symbols = sorted(set(targets) | set(positions) | {CASH_PROXY})
        prices = broker.latest_prices(symbols)
        equity = ledger.cash() + sum(p.qty * prices.get(s, 0) for s, p in positions.items())

        limits = self.limits(strategy)
        risk = assess(ledger, equity, self.today, limits)
        out.lines.extend(risk.events or [])
        if risk.halted:
            targets = {CASH_PROXY: 1.0}
            out.lines.append(f"risk halt in force, holding T-bills only: {risk.reason}")
        elif risk.paused:
            out.lines.append(f"weekly loss pause, no new risk buys: {risk.reason}")

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
            intent, why = self._risk_gate(intent, strategy, limits, risk, meta, positions, prices, equity)
            if intent is None:
                ledger.record_rejection(self.today, why[0], why[1])
                out.lines.append(f"blocked {why[0].side} {why[0].symbol}: {why[1]}")
                continue
            decision = self.guard.check(intent, positions=positions, available_cash=available)
            if not decision.approved:
                ledger.record_rejection(self.today, intent, decision.reason)
                out.lines.append(f"blocked {intent.side} {intent.symbol}: {decision.reason}")
                continue
            order = decision.intent
            cid = f"{ledger.mode}-{self.today:%Y%m%d}-{order.symbol}-{order.side}"
            try:  # one refused order must not cancel the rest of the day's orders
                oid = broker.submit(order, cid)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: broker rejected %s %s: %s", ledger.mode, order.side, order.symbol, exc)
                ledger.record_rejection(self.today, order, f"broker rejected: {exc}"[:300])
                out.lines.append(f"⚠ broker rejected {order.side} {order.symbol}: {exc}")
                continue
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

        mismatches: list[str] = []
        if getattr(broker, "reconciles", True):
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
    @staticmethod
    def _risk_gate(
        intent: OrderIntent,
        strategy,
        limits: RiskLimits,
        risk: RiskState,
        meta: dict,
        positions: dict,
        prices: dict[str, float],
        equity: float,
    ) -> tuple[OrderIntent | None, tuple[OrderIntent, str] | None]:
        """Loss limits and earnings blackout. Sells always pass: getting out must stay possible."""
        if intent.side == "sell" or intent.symbol == CASH_PROXY:
            return intent, None
        if risk.paused or risk.halted:
            return None, (intent, f"loss limit: {risk.reason}")
        if intent.symbol in set(meta.get("earnings_blackout", [])):
            reason = meta.get("earnings_reason", {}).get(intent.symbol, "near earnings")
            return None, (intent, f"earnings blackout ({reason})")
        cap = limits.max_single_stock
        if cap is not None and intent.symbol in strategy.single_stocks and intent.notional:
            held = positions.get(intent.symbol)
            current = held.qty * prices.get(intent.symbol, 0) if held and held.qty > EPS else 0.0
            room = cap * equity - current
            if room < 1:
                return None, (intent, f"single-stock cap {cap:.0%} of equity reached")
            if intent.notional > room:
                notional = int(room * 100) / 100
                return OrderIntent(intent.symbol, "buy", notional=notional, reason=intent.reason), None
        return intent, None

    def _sync_orders(self, ledger: Ledger, broker: Broker, out: ModeOutcome, quiet: bool = False) -> None:
        for order in ledger.open_orders():
            status, fill = broker.order_fill(order["order_id"])
            if fill and status in ("filled", "dead"):
                ledger.record_fill(fill, broker.next_trading_day(fill.filled_on), self.rate()[0])
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
            entry = ledger.record_dividend(
                div.action_id, div.symbol, qty.qty, div.rate, div.payable_date, self.rate()[0]
            )
            if entry:
                out.lines.append(
                    f"dividend {div.symbol}: ${entry['gross']:.2f} gross, "
                    f"${entry['withheld']:.2f} US withholding, ${entry['net']:.2f} net"
                )
