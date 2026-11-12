from datetime import date, timedelta

import pytest

from usptrader.broker.fake import FakeBroker
from usptrader.config import Settings
from usptrader.costs import CostModel
from usptrader.earnings import (
    ChainCalendar,
    EarningsEvent,
    HistoricalEarnings,
    NasdaqCalendar,
    NoEarnings,
    SecEarnings,
)
from usptrader.ledger import Ledger, LocalStore
from usptrader.models import OrderIntent
from usptrader.risk import RiskLimits, assess, limits_from_env, status_line
from usptrader.runner import Trader
from usptrader.strategies import Aggressive, StrategyContext

MON = date(2026, 11, 9)


def _ledger(tmp_path, equity_path=()):
    led = Ledger("m", LocalStore(tmp_path), CostModel())
    led.record_deposit(date(2026, 10, 1), 1000, 88)
    d = date(2026, 10, 1)
    for eq in equity_path:
        d += timedelta(days=1)
        led.record_snapshot(d, equity_usd=eq, rate=88, inr_if_withdrawn=eq * 87)
    return led


# ---- risk -----------------------------------------------------------------------------------------
LIM = RiskLimits(weekly_loss=0.04, max_drawdown=0.15, halt_days=3)


def test_drawdown_halts_and_records_once(tmp_path):
    led = _ledger(tmp_path, [1000, 1200])
    st = assess(led, 1000, MON, LIM)  # -16.7% from the 1200 peak
    assert st.halted and "drawdown" in st.reason
    again = assess(led, 1000, MON, LIM)
    assert again.halted
    assert len([e for e in led.of_type("risk") if e["action"] == "halt"]) == 1


def test_halt_auto_resumes_after_n_trading_days_with_peak_reset(tmp_path):
    led = _ledger(tmp_path, [1000, 1200])
    assess(led, 1000, MON, LIM)
    for i in range(3):
        led.record_snapshot(MON + timedelta(days=i), equity_usd=990, rate=88, inr_if_withdrawn=1)
    st = assess(led, 995, MON + timedelta(days=3), LIM)
    assert st.status == "ok" and "lifted" in st.events[0]
    # peak is now 995, so 990 isn't a 15% drawdown any more
    assert assess(led, 990, MON + timedelta(days=3), LIM).status == "ok"


def test_weekly_loss_pauses_for_the_week_only(tmp_path):
    led = _ledger(tmp_path, [1000])
    led.record_snapshot(date(2026, 11, 6), equity_usd=1000, rate=88, inr_if_withdrawn=1)  # last Friday
    st = assess(led, 955, MON, LIM)
    assert st.paused
    assert assess(led, 990, MON + timedelta(days=2), LIM).paused  # still paused this week
    assert assess(led, 990, MON + timedelta(days=7), LIM).status == "ok"  # new week


def test_no_limits_means_no_entries(tmp_path):
    led = _ledger(tmp_path, [1000, 2000])
    assert assess(led, 500, MON, RiskLimits()).status == "ok"
    assert led.of_type("risk") == []
    assert "benchmark" in status_line(led, RiskLimits(), MON)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("RISK_BALANCED_WEEKLY", "0.1")
    monkeypatch.setenv("RISK_BALANCED_DRAWDOWN", "off")
    monkeypatch.setenv("RISK_BALANCED_HALT_DAYS", "0")
    lim = limits_from_env("balanced", RiskLimits(0.04, 0.15))
    assert (lim.weekly_loss, lim.max_drawdown, lim.halt_days) == (0.1, None, 0)


# ---- earnings sources -----------------------------------------------------------------------------
def test_nasdaq_parses_rows_and_skips_weekends():
    calls = []

    def fetch(url, headers):
        calls.append(url)
        if url.endswith("2026-11-12"):
            return {"data": {"rows": [{"symbol": "CSCO"}, {"symbol": "nvda"}]}}
        return {"data": {"rows": None}}

    got = NasdaqCalendar(fetch).upcoming(["NVDA", "AAPL"], date(2026, 11, 12), date(2026, 11, 16))
    assert got == {"NVDA": EarningsEvent("NVDA", date(2026, 11, 12), True, "nasdaq")}
    assert len(calls) == 3  # Thu, Fri, Mon


def _sec_fetch(url, headers):
    assert "User-Agent" in headers
    if url.endswith("company_tickers.json"):
        return {"0": {"ticker": "NVDA", "cik_str": 1045810}}
    filed = ["2026-08-27", "2026-05-28", "2026-02-25", "2025-11-19", "2025-08-27", "2025-06-30"]
    return {
        "filings": {
            "recent": {
                "form": ["8-K"] * 5 + ["10-Q"],
                "items": ["2.02,9.01", "2.02,9.01", "2.02", "2.02,9.01", "2.02", ""],
                "filingDate": filed,
            }
        }
    }


def test_sec_history_and_projection(monkeypatch):
    monkeypatch.setattr("usptrader.earnings.time.sleep", lambda s: None)
    sec = SecEarnings("Test test@example.com", fetch=_sec_fetch)
    hist = sec.history("NVDA")
    assert hist[0] == date(2025, 8, 27) and hist[-1] == date(2026, 8, 27) and len(hist) == 5
    proj = sec.projected("NVDA", date(2026, 11, 1))
    assert date(2026, 11, 20) <= proj <= date(2026, 12, 2)
    near = sec.upcoming(["NVDA"], date(2026, 11, 20), date(2026, 11, 27))
    assert near["NVDA"].confirmed is False


def test_sec_requires_contact(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError):
        SecEarnings()


def test_chain_falls_through_and_raises_when_all_fail():
    class Boom:
        source = "boom"

        def upcoming(self, *a):
            raise OSError("down")

    chain = ChainCalendar(Boom(), HistoricalEarnings({"AAPL": [date(2026, 11, 13)]}))
    assert "AAPL" in chain.upcoming(["AAPL"], MON, MON + timedelta(days=7))
    assert chain.source == "history"
    with pytest.raises(RuntimeError):
        ChainCalendar(Boom()).upcoming(["AAPL"], MON, MON)


# ---- aggressive with earnings context -----------------------------------------------------------
def _rising(n=300, step=0.002):
    out, p = [], 100.0
    for _ in range(n):
        p *= 1 + step
        out.append(p)
    return out


def _closes():
    c = {s: _rising(step=0.0005) for s in Aggressive.candidates}
    c.update(NVDA=_rising(step=0.004), SMH=_rising(step=0.0035), AVGO=_rising(step=0.003))
    c["META"] = _rising(step=0.0025)
    c["VOO"] = _rising(step=0.001)
    return c


def test_aggressive_skips_blackout_stock_at_rebalance():
    ctx = StrategyContext(blackout=frozenset({"NVDA"}))
    r = Aggressive().decide(_closes(), MON, {}, ctx)
    assert "NVDA" not in r.targets and set(r.targets) == {"SMH", "AVGO", "META"}
    assert "skipped for earnings: NVDA" in r.note


def test_aggressive_exits_held_stock_before_earnings_but_not_etfs():
    first = Aggressive().decide(_closes(), MON, {})
    ctx = StrategyContext(blackout=frozenset({"NVDA", "AVGO"}), exit_now=frozenset({"NVDA"}))
    r = Aggressive().decide(_closes(), MON + timedelta(days=1), first.state, ctx)
    assert "NVDA" not in r.targets and "AVGO" in r.targets  # AVGO only in blackout: held, not added
    assert "out before earnings: NVDA" in r.note


# ---- runner integration -------------------------------------------------------------------------
def _trader(tmp_path, earnings, modes=("aggressive",)):
    settings = Settings(modes=modes, local_ledger_dir=str(tmp_path))
    store = LocalStore(tmp_path)
    brokers = {}
    trader = Trader(
        settings,
        lambda m: brokers.setdefault(m, FakeBroker(1000, today=MON)),
        store,
        today=MON,
        rate_fn=lambda: (88.0, "t"),
        fill_wait_seconds=0,
        earnings=earnings,
    )
    return trader, settings, store, brokers


def test_unknown_earnings_fails_safe_no_single_stock_buys(tmp_path):
    class Down:
        source = "down"

        def upcoming(self, *a):
            raise OSError("no network")

    trader, settings, store, brokers = _trader(tmp_path, Down())
    out = trader.run("premarket")[0]
    assert out.ok and any("unavailable" in line for line in out.lines)
    trader.run("trade")
    bought = {i.symbol for i in brokers["aggressive"].submitted if i.side == "buy"}
    assert not bought & set(Aggressive.single_stocks)


def test_known_earnings_recorded_in_plan_meta(tmp_path):
    cal = HistoricalEarnings({"AAPL": [MON + timedelta(days=2)]})
    trader, settings, store, _ = _trader(tmp_path, cal)
    trader.run("premarket")
    meta = Ledger("aggressive", store, settings.costs).latest_plan()["meta"]
    assert meta["earnings_blackout"] == ["AAPL"] and "reports" in meta["earnings_reason"]["AAPL"]


def test_risk_gate_blocks_blackout_adds_and_caps_single_stock():
    from usptrader.ledger import Position
    from usptrader.risk import RiskState

    gate = Trader._risk_gate
    s = Aggressive()
    lim = RiskLimits(max_single_stock=0.40)
    meta = {"earnings_blackout": ["AVGO"], "earnings_reason": {"AVGO": "reports Thu"}}
    ok = RiskState()
    pos = {"NVDA": Position(1, 300)}
    prices = {"NVDA": 350.0, "AVGO": 100.0}

    blocked, why = gate(OrderIntent("AVGO", "buy", notional=50), s, lim, ok, meta, pos, prices, 1000)
    assert blocked is None and "earnings blackout" in why[1]
    trimmed, _ = gate(OrderIntent("NVDA", "buy", notional=200), s, lim, ok, meta, pos, prices, 1000)
    assert trimmed.notional == pytest.approx(50)  # 40% of 1000 minus 350 held
    sell, _ = gate(OrderIntent("NVDA", "sell", qty=1), s, lim, RiskState("halted", "x"), meta, pos, prices, 1)
    assert sell is not None  # exits always allowed
    etf, _ = gate(
        OrderIntent("SGOV", "buy", notional=900), s, lim, RiskState("paused", "x"), meta, pos, prices, 1
    )
    assert etf is not None  # parking in T-bills is always allowed


def test_no_earnings_calendar_is_inert():
    assert NoEarnings().upcoming(["AAPL"], MON, MON) == {}
