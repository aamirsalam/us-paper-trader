"""Alpaca paper-trading adapter (alpaca-py). Paper keys only: live is refused on purpose."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, timezone

from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, CorporateActionsType, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.models import AccountConfiguration
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest

from ..models import Dividend, Fill, OrderIntent
from .base import Broker

log = logging.getLogger(__name__)

DEAD = {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED, OrderStatus.DONE_FOR_DAY}


class AlpacaPaperBroker(Broker):
    name = "alpaca-paper"

    def __init__(self, key_id: str, secret: str) -> None:
        self.trading = TradingClient(key_id, secret, paper=True)
        self.data = StockHistoricalDataClient(key_id, secret)
        self.actions = CorporateActionsClient(key_id, secret)
        self._calendar: dict[date, bool] = {}

    # ---- account ------------------------------------------------------------------------------
    def lock_down_account(self) -> list[str]:
        """Make the paper account behave like an LRS cash account at the broker too."""
        cfg = self.trading.get_account_configurations()
        wanted = {
            "no_shorting": True,
            "max_margin_multiplier": "1",
            "max_options_trading_level": 0,
            "fractional_trading": True,
        }
        changed = [k for k, v in wanted.items() if str(getattr(cfg, k, None)) != str(v)]
        if changed:
            new = AccountConfiguration(**{**cfg.model_dump(), **wanted})
            self.trading.set_account_configurations(new)
        return changed

    def cash_available(self) -> float:
        acct = self.trading.get_account()
        return float(acct.non_marginable_buying_power or acct.cash or 0)

    def positions(self) -> dict[str, float]:
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()}

    # ---- calendar -----------------------------------------------------------------------------
    def _load_calendar(self, around: date) -> None:
        req = GetCalendarRequest(start=around - timedelta(days=10), end=around + timedelta(days=20))
        open_days = {d.date for d in self.trading.get_calendar(req)}
        for i in range(-10, 21):
            day = around + timedelta(days=i)
            self._calendar[day] = day in open_days

    def is_trading_day(self, day: date) -> bool:
        if day not in self._calendar:
            self._load_calendar(day)
        return self._calendar[day]

    def next_trading_day(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    # ---- data ---------------------------------------------------------------------------------
    def daily_closes(self, symbols: list[str], days: int) -> dict[str, list[float]]:
        # The free plan serves full-market (SIP) history older than 15 minutes; fall back to IEX.
        end = datetime.now(UTC) - timedelta(minutes=20)
        start = end - timedelta(days=int(days * 1.6) + 10)
        today = datetime.now(UTC).date()
        for feed in (DataFeed.SIP, DataFeed.IEX):
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=symbols,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                    adjustment=Adjustment.ALL,
                    feed=feed,
                )
                bars = self.data.get_stock_bars(req).data
                break
            except APIError as exc:
                log.warning("bars via %s failed: %s", feed.value, exc)
        else:
            raise RuntimeError("could not fetch daily bars")
        out: dict[str, list[float]] = {}
        for sym in symbols:
            series = [b for b in bars.get(sym, []) if b.timestamp.date() < today]
            out[sym] = [float(b.close) for b in series][-days:]
        return out

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        req = StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
        trades = self.data.get_stock_latest_trade(req)
        return {sym: float(t.price) for sym, t in trades.items()}

    def cash_dividends(self, symbols: list[str], start: date, end: date) -> list[Dividend]:
        if not symbols:
            return []
        req = CorporateActionsRequest(
            symbols=symbols, types=[CorporateActionsType.CASH_DIVIDEND], start=start, end=end
        )
        result = self.actions.get_corporate_actions(req)
        out = []
        for action in result.data.get("cash_dividends", []):
            if action.payable_date and action.ex_date:
                out.append(
                    Dividend(
                        action_id=str(action.id),
                        symbol=action.symbol,
                        rate=float(action.rate),
                        ex_date=action.ex_date,
                        payable_date=action.payable_date,
                    )
                )
        return out

    # ---- orders -------------------------------------------------------------------------------
    def submit(self, intent: OrderIntent, client_order_id: str) -> str:
        req = MarketOrderRequest(
            symbol=intent.symbol,
            side=OrderSide.BUY if intent.side == "buy" else OrderSide.SELL,
            notional=round(intent.notional, 2) if intent.notional is not None else None,
            qty=round(intent.qty, 9) if intent.qty is not None else None,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        try:
            return str(self.trading.submit_order(req).id)
        except APIError as exc:
            # Re-runs of the same phase reuse the client id; pick up the earlier order instead.
            if "client_order_id must be unique" in str(exc):
                return str(self.trading.get_order_by_client_id(client_order_id).id)
            raise

    def order_fill(self, order_id: str) -> tuple[str, Fill | None]:
        order = self.trading.get_order_by_id(order_id)
        qty = float(order.filled_qty or 0)
        fill = None
        if qty > 0 and order.filled_avg_price:
            fill = Fill(
                order_id=str(order.id),
                symbol=order.symbol,
                side="buy" if order.side == OrderSide.BUY else "sell",
                qty=qty,
                price=float(order.filled_avg_price),
                filled_on=(order.filled_at or order.submitted_at).astimezone(_ET).date(),
            )
        if order.status == OrderStatus.FILLED:
            return "filled", fill
        if order.status in DEAD:
            return "dead", fill
        return "open", fill


try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone(timedelta(hours=-5))
