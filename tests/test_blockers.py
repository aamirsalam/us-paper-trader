"""Pre-paper-run fixes: ledger-only baseline, tolerant broker setup, per-order errors, one ledger read."""

from datetime import date

import pytest

from usptrader.broker.fake import FakeBroker
from usptrader.broker.virtual import VirtualBroker
from usptrader.config import Settings
from usptrader.earnings import NoEarnings
from usptrader.ledger import Ledger, LocalStore
from usptrader.models import OrderIntent
from usptrader.report import build_report
from usptrader.runner import Trader

MON = date(2026, 11, 9)


def trader_with(tmp_path, broker_for, modes=("baseline",), **kw):
    settings = Settings(modes=modes, local_ledger_dir=str(tmp_path), **kw)
    store = LocalStore(tmp_path)
    t = Trader(
        settings,
        broker_for,
        store,
        today=MON,
        rate_fn=lambda: (88.0, "t"),
        fill_wait_seconds=0,
        earnings=NoEarnings(),
    )
    return t, settings, store


# ---- 1. baseline without a broker account -------------------------------------------------------
def test_virtual_broker_fills_at_market_with_slippage_and_delegates_data():
    data = FakeBroker(1000, today=MON)
    v = VirtualBroker(data, slippage_bps=10, today_fn=lambda: MON)
    px = data.latest_prices(["VOO"])["VOO"]
    oid = v.submit(OrderIntent("VOO", "buy", notional=500), "c1")
    status, fill = v.order_fill(oid)
    assert status == "filled" and fill.price == pytest.approx(px * 1.001)
    assert fill.qty == pytest.approx(500 / (px * 1.001)) and fill.filled_on == MON
    assert data.submitted == []  # nothing was sent to the real broker
    assert v.is_trading_day(MON) and v.daily_closes(["VOO"], 5) == data.daily_closes(["VOO"], 5)
    assert v.order_fill("unknown") == ("dead", None)


def test_baseline_runs_a_full_day_on_the_ledger_alone(tmp_path):
    data = FakeBroker(1000, today=MON)
    virtual = VirtualBroker(data, today_fn=lambda: MON)
    t, settings, store = trader_with(tmp_path, lambda m: virtual)
    for phase in ("premarket", "trade", "postclose"):
        assert all(o.ok for o in t.run(phase)), phase
    led = Ledger("baseline", store, settings.costs)
    assert "VOO" in led.positions() and led.of_type("fill")[0]["rate"] == 88.0
    assert led.of_type("snapshot")[-1]["mismatches"] == []  # no broker side to reconcile against
    assert data.submitted == []


def test_settings_split_virtual_and_broker_modes(monkeypatch):
    monkeypatch.setenv("TRADER_MODES", "baseline,balanced,aggressive")
    monkeypatch.delenv("VIRTUAL_MODES", raising=False)
    s = Settings.from_env()
    assert s.virtual_modes == ("baseline",) and s.broker_modes() == ("balanced", "aggressive")
    monkeypatch.setenv("VIRTUAL_MODES", "")
    assert Settings.from_env().virtual_modes == ()


# ---- 2. a refused account setting doesn't block funding -------------------------------------------
def test_lockdown_failure_is_a_warning_not_a_dead_mode(tmp_path):
    class Fussy(FakeBroker):
        def lock_down_account(self):
            raise RuntimeError("422 max_options_trading_level not allowed")

    fussy = Fussy(1000, today=MON)
    t, settings, store = trader_with(tmp_path, lambda m: fussy)
    out = t.run("premarket")[0]
    assert out.ok and any("refused cash-only settings" in line for line in out.lines)
    assert Ledger("baseline", store, settings.costs).deposited_usd() == 1000


# ---- 3. one rejected order doesn't cancel the rest ------------------------------------------------
def test_rejected_order_is_recorded_and_others_still_go(tmp_path):
    class RejectsVOO(FakeBroker):
        def submit(self, intent, client_order_id):
            if intent.symbol == "VOO":
                raise RuntimeError("asset not fractionable")
            return super().submit(intent, client_order_id)

    broker = RejectsVOO(1000, today=MON)
    t, settings, store = trader_with(tmp_path, lambda m: broker, modes=("conservative",))
    out = t.run("trade")[0]
    assert out.ok and any("broker rejected buy VOO" in line for line in out.lines)
    led = Ledger("conservative", store, settings.costs)
    assert [r["reason"] for r in led.of_type("rejection")] == ["broker rejected: asset not fractionable"]
    assert {f["symbol"] for f in led.of_type("fill")} == {"SGOV"}  # the other leg still traded


# ---- 4 & 6. report: earnings source shown, each ledger read once ----------------------------------
class CountingStore(LocalStore):
    loads = 0

    def load(self, mode):
        CountingStore.loads += 1
        return super().load(mode)


def test_report_reads_each_ledger_once_and_reuses_the_traders(tmp_path):
    settings = Settings(modes=("baseline", "conservative"), local_ledger_dir=str(tmp_path))
    store = CountingStore(tmp_path)
    brokers = {}
    t = Trader(
        settings,
        lambda m: brokers.setdefault(m, FakeBroker(1000, today=MON)),
        store,
        today=MON,
        rate_fn=lambda: (88.0, "t"),
        fill_wait_seconds=0,
        earnings=NoEarnings(),
    )
    outs = []
    for phase in ("premarket", "trade", "postclose"):
        outs = t.run(phase)
    CountingStore.loads = 0
    build_report(settings, store, MON, outs, t.ledgers())
    assert CountingStore.loads == 0  # reused the trader's ledgers
    build_report(settings, store, MON, outs)
    assert CountingStore.loads == 2  # standalone report: once per mode, not three times


def test_report_shows_after_tax_column_and_earnings_source(tmp_path):
    from usptrader.earnings import HistoricalEarnings

    settings = Settings(modes=("baseline", "aggressive"), local_ledger_dir=str(tmp_path))
    store = LocalStore(tmp_path)
    brokers = {}
    t = Trader(
        settings,
        lambda m: brokers.setdefault(m, FakeBroker(1000, today=MON)),
        store,
        today=MON,
        rate_fn=lambda: (88.0, "t"),
        fill_wait_seconds=0,
        earnings=HistoricalEarnings({}),
    )
    outs = []
    for phase in ("premarket", "trade", "postclose"):
        outs = t.run(phase)
    _, body = build_report(settings, store, MON, outs)
    assert "after tax %" in body and "after tax ₹" in body
    assert "earnings dates from: history" in body
    assert "ledger-only mode" in body  # baseline is virtual by default
    assert "Form 67" in body
