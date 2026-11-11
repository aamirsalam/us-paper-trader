from datetime import date

import pytest

from usptrader.compliance import ComplianceGuard
from usptrader.costs import CostModel, indian_fy
from usptrader.ledger import Ledger, LocalStore, Position
from usptrader.models import Fill, OrderIntent
from usptrader.rebalance import plan_orders
from usptrader.strategies import Balanced, Conservative

D = date(2026, 11, 11)


@pytest.fixture
def ledger(tmp_path):
    return Ledger("t", LocalStore(tmp_path), CostModel())


# ---- costs ----------------------------------------------------------------------------------------
def test_fx_round_trip_loses_both_spreads():
    c = CostModel(fx_spread_pct=0.01)
    inr_in = c.inr_to_fund(1000, 88)
    inr_out = c.inr_on_withdrawal(1000, 88)
    assert inr_in == pytest.approx(88880)
    assert inr_out == pytest.approx(87120)


def test_tcs_only_above_threshold():
    c = CostModel()
    assert c.tcs(0, 900_000) == 0
    assert c.tcs(900_000, 200_000) == pytest.approx(20_000)  # 20% of the 1L above 10L
    assert c.tcs(1_200_000, 100_000) == pytest.approx(20_000)


def test_dividend_withholding_25pct():
    assert CostModel().dividend_net(10.0) == (7.5, 2.5)


def test_indian_fy():
    assert indian_fy(date(2026, 11, 11)) == "FY2026-27"
    assert indian_fy(date(2027, 3, 31)) == "FY2026-27"
    assert indian_fy(date(2027, 4, 1)) == "FY2027-28"


# ---- compliance -----------------------------------------------------------------------------------
GUARD = ComplianceGuard()


def test_blocks_short_sale():
    d = GUARD.check(OrderIntent("AAPL", "sell", qty=1), positions={}, available_cash=1000)
    assert not d.approved and "short" in d.reason


def test_trims_oversized_sell():
    d = GUARD.check(
        OrderIntent("AAPL", "sell", qty=5), positions={"AAPL": Position(2, 300)}, available_cash=0
    )
    assert d.approved and d.adjusted and d.intent.qty == 2


def test_buy_limited_to_settled_cash():
    d = GUARD.check(OrderIntent("VOO", "buy", notional=500), positions={}, available_cash=123.456)
    assert d.intent.notional == 123.45 and d.adjusted


def test_no_cash_means_no_buy():
    d = GUARD.check(OrderIntent("VOO", "buy", notional=500), positions={}, available_cash=0.5)
    assert not d.approved and "margin" in d.reason


@pytest.mark.parametrize("sym", ["TQQQ", "SQQQ", "UVXY", "tsll"])
def test_leveraged_etfs_blocked(sym):
    d = GUARD.check(OrderIntent(sym, "buy", notional=50), positions={}, available_cash=1000)
    assert not d.approved


def test_options_blocked():
    d = GUARD.check(
        OrderIntent("AAPL250117C00200000", "buy", notional=50, asset_class="us_option"),
        positions={},
        available_cash=1000,
    )
    assert not d.approved and "LRS" in d.reason


# ---- ledger ---------------------------------------------------------------------------------------
def test_sale_proceeds_settle_t_plus_1(ledger):
    ledger.record_deposit(D, 1000, 88)
    ledger.record_fill(Fill("o1", "VOO", "buy", 2, 400, D), date(2026, 11, 12))
    assert ledger.settled_cash(D) == pytest.approx(200)
    ledger.record_fill(Fill("o2", "VOO", "sell", 1, 410, date(2026, 11, 12)), date(2026, 11, 13))
    proceeds = 410 - CostModel().sell_fees(410, 1)
    assert ledger.cash() == pytest.approx(200 + proceeds)
    assert ledger.settled_cash(date(2026, 11, 12)) == pytest.approx(200)  # not yet settled
    assert ledger.settled_cash(date(2026, 11, 13)) == pytest.approx(200 + proceeds)


def test_realized_pnl_and_avg_cost(ledger):
    ledger.record_deposit(D, 1000, 88)
    ledger.record_fill(Fill("a", "X", "buy", 1, 100, D), D)
    ledger.record_fill(Fill("b", "X", "buy", 1, 200, D), D)
    assert ledger.positions()["X"].avg_cost == 150
    ledger.record_fill(Fill("c", "X", "sell", 1, 180, D), D)
    assert ledger.realized_pnl() == pytest.approx(30 - CostModel().sell_fees(180, 1))
    assert ledger.positions()["X"].qty == pytest.approx(1)


def test_fill_recorded_once(ledger):
    ledger.record_deposit(D, 1000, 88)
    f = Fill("o1", "VOO", "buy", 1, 400, D)
    assert ledger.record_fill(f, D)
    assert ledger.record_fill(f, D) is None
    assert len(ledger.of_type("fill")) == 1


def test_ledger_survives_reload(tmp_path):
    store = LocalStore(tmp_path)
    a = Ledger("m", store, CostModel())
    a.record_deposit(D, 1000, 88)
    a.record_dividend("div1", "VOO", 2, 1.5, D)
    b = Ledger("m", store, CostModel())
    assert b.cash() == pytest.approx(1000 + 2.25)
    assert b.dividends() == (3.0, 0.75)


def test_deposit_tracks_tcs_per_fy(ledger):
    ledger.record_deposit(D, 10_000, 88)  # ~₹8.9L
    second = ledger.record_deposit(D, 5_000, 88)  # crosses ₹10L
    assert second["tcs_inr"] > 0


# ---- rebalance ------------------------------------------------------------------------------------
def test_sells_come_before_buys():
    orders = plan_orders({"B": 1.0}, {"A": Position(2, 200)}, {"A": 100, "B": 50}, 1000)
    assert [o.side for o in orders] == ["sell", "buy"]
    assert orders[0].qty == 2


def test_drift_band_skips_small_moves():
    pos = {"A": Position(9.8, 980)}
    assert plan_orders({"A": 1.0}, pos, {"A": 100}, 1000, drift_threshold=0.05) == []


def test_weights_over_one_rejected():
    with pytest.raises(ValueError):
        plan_orders({"A": 0.7, "B": 0.5}, {}, {"A": 1, "B": 1}, 1000)


# ---- strategies -----------------------------------------------------------------------------------
def _rising(n=300, start=100.0, step=0.002):
    out, p = [], start
    for _ in range(n):
        p *= 1 + step
        out.append(p)
    return out


def test_conservative_risk_off_in_downtrend():
    falling = list(reversed(_rising()))
    r = Conservative().decide({"VOO": falling}, D, {"regime": "on"})
    assert r.targets == {"SGOV": 1.0} and r.state["regime"] == "off"


def test_conservative_hysteresis_holds_regime_inside_band():
    flat = [100.0] * 250
    r = Conservative().decide({"VOO": flat}, D, {"regime": "off"})
    assert r.state["regime"] == "off"


def test_balanced_picks_top_momentum_and_holds_for_month():
    closes = {s: _rising(step=0.001) for s in Balanced.candidates}
    closes["XLE"] = _rising(step=0.004)
    closes["XLK"] = _rising(step=0.003)
    first = Balanced().decide(closes, D, {})
    assert set(first.targets) == {"XLE", "XLK", next(iter(set(first.targets) - {"XLE", "XLK"}))}
    assert sum(first.targets.values()) == pytest.approx(1)
    again = Balanced().decide({}, date(2026, 11, 20), first.state)
    assert again.targets == first.targets


def test_balanced_parks_in_tbills_when_nothing_trends():
    falling = list(reversed(_rising()))
    r = Balanced().decide({s: falling for s in Balanced.candidates}, D, {})
    assert r.targets == {"SGOV": 1.0}


# ---- aggressive -----------------------------------------------------------------------------------
from usptrader.strategies import Aggressive  # noqa: E402


def _closes_for_aggressive(fast=("NVDA", "SMH", "AVGO"), voo_step=0.001):
    closes = {s: _rising(step=0.0005) for s in Aggressive.candidates}
    for i, s in enumerate(fast):
        closes[s] = _rising(step=0.004 - i * 0.0005)
    closes["VOO"] = _rising(step=voo_step)
    return closes


def test_aggressive_picks_top3_including_single_stocks():
    r = Aggressive().decide(_closes_for_aggressive(), D, {})
    assert set(r.targets) == {"NVDA", "SMH", "AVGO"}
    assert sum(r.targets.values()) == pytest.approx(1)
    assert r.state["period"] == "2026-11-a"


def test_aggressive_market_brake_limits_to_one_pick():
    closes = _closes_for_aggressive()
    closes["VOO"] = list(reversed(_rising()))
    r = Aggressive().decide(closes, D, {})
    assert r.targets["NVDA"] == pytest.approx(1 / 3)
    assert r.targets["SGOV"] == pytest.approx(2 / 3)
    assert "brake" in r.note


def test_aggressive_rebalances_twice_a_month_and_holds_between():
    first = Aggressive().decide(_closes_for_aggressive(), D, {})
    same_period = Aggressive().decide(_closes_for_aggressive(), date(2026, 11, 13), first.state)
    assert same_period.targets == first.targets and "holding" in same_period.note
    new_period = Aggressive().decide(
        _closes_for_aggressive(fast=("META", "XLK", "TSLA")), date(2026, 11, 16), first.state
    )
    assert set(new_period.targets) == {"META", "XLK", "TSLA"}


def test_aggressive_protective_exit_below_50d():
    first = Aggressive().decide(_closes_for_aggressive(), D, {})
    closes = _closes_for_aggressive()
    closes["NVDA"] = closes["NVDA"][:-1] + [closes["NVDA"][-60] * 0.9]  # sharp drop below 50d avg
    r = Aggressive().decide(closes, date(2026, 11, 12), first.state)
    assert "NVDA" not in r.targets and r.targets["SGOV"] == pytest.approx(1 / 3)
    assert "NVDA" not in r.state["picks"]


def test_aggressive_never_picks_leveraged_products():
    from usptrader.compliance import LEVERAGED_OR_INVERSE

    assert not set(Aggressive.universe) & LEVERAGED_OR_INVERSE
