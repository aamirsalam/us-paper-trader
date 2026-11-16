from datetime import date

import pytest

from usptrader.costs import CostModel
from usptrader.ledger import Ledger, MemoryStore
from usptrader.models import Fill
from usptrader.tax import (
    TaxModel,
    add_months,
    after_tax,
    build,
    is_long_term,
    month_end_before,
    rule115_rates,
    set_off,
)

M = TaxModel(slab_rate=0.30, cess=0.04)  # short-term 31.2%, long-term 13%
FLAT = lambda d: 80.0  # noqa: E731  constant USD/INR keeps the arithmetic readable


def ledger():
    led = Ledger("m", MemoryStore(), CostModel(fx_spread_pct=0.0, sec_fee_rate=0.0, finra_taf_per_share=0.0))
    led.record_deposit(date(2024, 4, 1), 1000, 80)
    return led


def buy(led, oid, day, qty, price, sym="X"):
    led.record_fill(Fill(oid, sym, "buy", qty, price, day), day, 80)


def sell(led, oid, day, qty, price, sym="X"):
    led.record_fill(Fill(oid, sym, "sell", qty, price, day), day, 80)


def snap(led, day, equity, holdings, rate=80.0):
    led.record_snapshot(
        day, equity_usd=equity, cash_usd=0, holdings=holdings, rate=rate, inr_if_withdrawn=equity * rate
    )


# ---- building blocks ------------------------------------------------------------------------------
def test_rates():
    assert M.slab_eff == pytest.approx(0.312)
    assert M.ltcg_eff == pytest.approx(0.13)


def test_long_term_needs_more_than_24_months():
    b = date(2024, 5, 10)
    assert not is_long_term(b, date(2026, 5, 10), 24)  # exactly 24 months is still short-term
    assert is_long_term(b, date(2026, 5, 11), 24)
    assert add_months(date(2024, 2, 29), 24) == date(2026, 2, 28)


@pytest.mark.parametrize(
    "st, lt, c_st, c_lt, expect",
    [
        (100, 50, 0, 0, (100, 50, 0, 0)),
        (-30, 50, 0, 0, (0, 20, 0, 0)),  # short-term loss eats long-term gain
        (50, -30, 0, 0, (50, 0, 0, 30)),  # long-term loss can't touch short-term gain
        (-80, 50, 0, 0, (0, 0, 30, 0)),  # leftover short-term loss carries forward
        (40, 40, 100, 0, (0, 0, 20, 0)),  # carried short-term loss: short-term first, then long-term
        (40, 40, 0, 100, (40, 0, 0, 60)),  # carried long-term loss only against long-term
    ],
)
def test_set_off_rules(st, lt, c_st, c_lt, expect):
    assert set_off(st, lt, c_st, c_lt) == pytest.approx(expect)


# ---- gains ----------------------------------------------------------------------------------------
def test_fifo_lots_split_short_and_long_term():
    led = ledger()
    buy(led, "b1", date(2024, 4, 10), 1, 100)
    buy(led, "b2", date(2026, 1, 10), 1, 150)
    sell(led, "s1", date(2026, 6, 10), 2, 200)  # first lot held 26 months, second 5 months
    y = build(led, M, date(2026, 6, 30), FLAT).years["FY2026-27"]
    assert y.ltcg == pytest.approx(100 * 80)
    assert y.stcg == pytest.approx(50 * 80)
    assert y.tax_gains == pytest.approx(8000 * 0.13 + 4000 * 0.312)


def test_rupee_fall_is_taxed_even_when_dollar_price_is_flat():
    led = ledger()
    buy(led, "b1", date(2024, 4, 10), 1, 100)
    sell(led, "s1", date(2024, 9, 10), 1, 100)
    rate = lambda d: 80.0 if d < date(2024, 6, 1) else 88.0  # noqa: E731
    y = build(led, M, date(2024, 9, 30), rate).years["FY2024-25"]
    assert y.stcg == pytest.approx(100 * 8)  # no dollar gain, ₹800 taxable


def test_loss_carries_into_next_financial_year():
    led = ledger()
    buy(led, "b1", date(2024, 4, 10), 1, 100)
    sell(led, "s1", date(2024, 6, 10), 1, 60)  # -₹3,200 in FY2024-25
    buy(led, "b2", date(2025, 4, 10), 1, 100)
    sell(led, "s2", date(2025, 6, 10), 1, 150)  # +₹4,000 in FY2025-26
    years = build(led, M, date(2025, 6, 30), FLAT).years
    assert years["FY2024-25"].tax == 0 and years["FY2024-25"].carry_out_st == pytest.approx(3200)
    assert years["FY2025-26"].taxable_st == pytest.approx(800)


def test_dividend_tax_net_of_foreign_tax_credit():
    led = ledger()
    buy(led, "b1", date(2024, 4, 10), 10, 10)
    led.record_dividend("d1", "X", 10, 1.0, date(2024, 7, 1), 80)  # $10 gross, $2.50 withheld
    y = build(led, M, date(2024, 7, 31), FLAT).years["FY2024-25"]
    assert y.dividends == pytest.approx(800) and y.us_withheld == pytest.approx(200)
    assert y.ftc == pytest.approx(200)
    assert y.tax_dividends == pytest.approx(800 * 0.312 - 200)


def test_credit_is_capped_at_indian_tax_for_low_slab():
    led = ledger()
    buy(led, "b1", date(2024, 4, 10), 10, 10)
    led.record_dividend("d1", "X", 10, 1.0, date(2024, 7, 1), 80)
    low = TaxModel(slab_rate=0.10, cess=0.04)
    y = build(led, low, date(2024, 7, 31), FLAT).years["FY2024-25"]
    assert y.tax_dividends == 0 and y.ftc == pytest.approx(800 * 0.104)  # extra US tax is simply lost


# ---- after-tax value --------------------------------------------------------------------------------
def test_buy_and_hold_exit_tax_switches_to_long_term_rate():
    led = ledger()
    buy(led, "b1", date(2024, 4, 10), 10, 100)
    snap(led, date(2025, 4, 10), 1500, {"X": 1500})
    short = after_tax(led, M, date(2025, 4, 10), FLAT)
    assert short.tax_on_exit_inr == pytest.approx(500 * 80 * 0.312)
    assert short.value_inr == pytest.approx(1500 * 80 - 500 * 80 * 0.312)
    snap(led, date(2026, 4, 13), 1500, {"X": 1500})
    long = after_tax(led, M, date(2026, 4, 13), FLAT)
    assert long.tax_on_exit_inr == pytest.approx(500 * 80 * 0.13)


def test_past_years_tax_shrinks_the_portfolio():
    led = ledger()
    buy(led, "b1", date(2024, 4, 10), 10, 100)
    sell(led, "s1", date(2024, 8, 10), 10, 150)  # ₹40,000 short-term gain in FY2024-25
    snap(led, date(2025, 3, 31), 1500, {})
    snap(led, date(2025, 6, 30), 1500, {})
    t = after_tax(led, M, date(2025, 6, 30), FLAT)
    tax = 40000 * 0.312
    assert t.tax_paid_inr == pytest.approx(tax) and t.tax_accrued_inr == 0
    assert t.drag_factor == pytest.approx(1 - tax / (1500 * 80))
    assert t.value_inr == pytest.approx(1500 * 80 - tax)


def test_no_snapshot_means_no_figure():
    assert after_tax(ledger(), M, date(2024, 4, 2)) is None


# ---- rates ------------------------------------------------------------------------------------------
def test_rule115_uses_last_rate_before_the_month():
    led = ledger()  # deposit on 2024-04-01 at 80
    snap(led, date(2024, 4, 29), 1000, {}, rate=83)
    snap(led, date(2024, 5, 15), 1000, {}, rate=85)
    lookup = rule115_rates(led)
    assert lookup(date(2024, 5, 20)) == 83  # last known rate before 1 May
    assert lookup(date(2024, 4, 20)) == 80  # nothing before April yet: latest known up to the day
    assert month_end_before(lambda d: float(d.day))(date(2024, 5, 20)) == 30.0
