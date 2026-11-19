from datetime import date

import pytest

from usptrader.broker.fake import FakeBroker
from usptrader.config import Settings
from usptrader.earnings import NoEarnings
from usptrader.ledger import LocalStore
from usptrader.report import build_report, inr, pct, pts, usd
from usptrader.runner import Trader

MON = date(2026, 11, 9)


@pytest.mark.parametrize(
    "x, out",
    [(0.0003, "+0.03%"), (-0.0002, "\u22120.02%"), (-0.00001, "0.00%"), (0.0, "0.00%"), (0.1234, "+12.34%")],
)
def test_pct_has_two_places_and_no_negative_zero(x, out):
    assert pct(x) == out


@pytest.mark.parametrize(
    "x, signed, out",
    [(470704, False, "₹4,70,704"), (1000000, False, "₹10,00,000"), (94141, False, "₹94,141"),
     (999, False, "₹999"), (29, True, "+₹29"), (-1402, True, "\u2212₹1,402"), (0, True, "₹0"),
     (123456789, False, "₹12,34,56,789")],
)  # fmt: skip
def test_inr_uses_indian_grouping(x, signed, out):
    assert inr(x, signed) == out


def test_pts_and_usd():
    assert pts(0.0012) == "+0.12 pts" and pts(-0.00001) == "0.00 pts"
    assert usd(1000.31) == "$1,000.31" and usd(329.6, cents=False) == "$330"


def _day(tmp_path, modes=("baseline", "balanced")):
    settings = Settings(modes=modes, local_ledger_dir=str(tmp_path))
    store = LocalStore(tmp_path)
    brokers = {}
    t = Trader(settings, lambda m: brokers.setdefault(m, FakeBroker(1000, today=MON)), store, today=MON,
               rate_fn=lambda: (88.0, "t"), fill_wait_seconds=0, earnings=NoEarnings())  # fmt: skip
    outs = []
    for phase in ("premarket", "trade", "postclose"):
        outs = t.run(phase)
    return settings, store, outs


def test_todays_trades_come_from_the_ledger_not_the_last_job(tmp_path):
    settings, store, outs = _day(tmp_path)
    r = build_report(settings, store, MON, outs)  # outs are postclose's only: no trade lines in them
    assert "baseline: bought VOO $" in r.text
    assert "balanced: bought" in r.text
    assert "Quiet day" not in r.text


def test_quiet_day_and_zero_details_are_hidden(tmp_path):
    settings, store, outs = _day(tmp_path)
    r = build_report(settings, store, date(2026, 11, 10), [])  # nothing happened on the 10th
    assert "Quiet day: no trades." in r.text
    for noise in ("dividends $0.00", "0 orders blocked", "past years ₹0", "bank FX"):
        assert noise not in r.text


def test_layout_order_and_status_first(tmp_path):
    settings, store, outs = _day(tmp_path)
    r = build_report(settings, store, MON, outs)
    t = r.text
    assert (
        t.index("All 2 modes OK") < t.index("Mode") < t.index("Today") < t.index("Holdings") < t.index("LRS")
    )
    assert r.subject.startswith("US paper · day 1 · all OK · ")
    assert t.count("Bank currency fees") == 1  # the currency fees are explained once, not per mode
