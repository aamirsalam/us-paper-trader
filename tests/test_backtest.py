import io
import json
from datetime import date

import pytest

from usptrader.backtest import format_result, run_backtest, write_outputs
from usptrader.broker.historical import HistoricalBroker
from usptrader.config import Settings
from usptrader.earnings import HistoricalEarnings
from usptrader.marketdata import Series, fetch_yahoo, splice, synthetic
from usptrader.models import OrderIntent
from usptrader.strategies import Aggressive

START, END = date(2024, 1, 2), date(2025, 6, 30)


@pytest.fixture(scope="module")
def data():
    syms = sorted(
        set(Aggressive.universe)
        | {"QQQ", "XLK", "XLV", "XLF", "XLE", "XLI", "XLY", "XLP", "XLU", "XLC", "XLB"}
    )
    return synthetic(syms, START, END)


def test_backtest_runs_all_modes_and_respects_cash_rules(data):
    r = run_backtest(Settings(), data)
    assert not r.failures
    assert [s.mode for s in r.stats] == ["baseline", "conservative", "balanced", "aggressive"]
    for s in r.stats:
        assert s.curve and s.end_usd > 0
        assert s.inr_out > 0 and s.fx_friction_inr > 0
    base = r.stats[0]
    assert base.fills >= 1 and base.div_withheld == pytest.approx(base.div_gross * 0.25, rel=1e-6)
    text = format_result(r)
    assert "Calendar-year returns" in text and "Caveats" in text and "survivorship" in text


def test_backtest_is_deterministic(data):
    a = run_backtest(Settings(modes=("balanced",)), data)
    b = run_backtest(Settings(modes=("balanced",)), data)
    assert a.stats[0].end_usd == b.stats[0].end_usd


def test_limits_toggle_changes_only_limited_modes(data):
    on = run_backtest(Settings(modes=("baseline",)), data, limits_enabled=True)
    off = run_backtest(Settings(modes=("baseline",)), data, limits_enabled=False)
    assert on.stats[0].end_usd == off.stats[0].end_usd


def test_earnings_history_blocks_single_stock_buys(data):
    # Every stock "reports" every week, so each one is always inside its 5-day blackout.
    weekly = [date.fromordinal(date(2023, 1, 4).toordinal() + 7 * i) for i in range(140)]
    cal = HistoricalEarnings({s: weekly for s in Aggressive.single_stocks})
    free = run_backtest(Settings(modes=("aggressive",)), data)
    blocked = run_backtest(Settings(modes=("aggressive",)), data, earnings=cal)
    assert set(free.stats[0].traded) & set(Aggressive.single_stocks)  # it does buy stocks normally
    assert not set(blocked.stats[0].traded) & set(Aggressive.single_stocks)
    assert "Earnings blackout was OFF" not in " ".join(blocked.caveats)


def test_write_outputs(tmp_path, data):
    r = run_backtest(Settings(modes=("baseline",)), data)
    paths = write_outputs(r, tmp_path)
    assert {p.name for p in paths} == {"equity_curves.csv", "summary.json", "report.txt"}
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["modes"][0]["mode"] == "baseline"
    assert (tmp_path / "equity_curves.csv").read_text().startswith("date,baseline_usd,baseline_inr")


def test_historical_broker_no_lookahead_and_open_fills(data):
    b = HistoricalBroker(data, slippage_bps=10)
    day = data.calendar[300]
    b.set_clock(day, "open")
    s = data.series["VOO"]
    i = s.idx(day)
    closes = b.daily_closes(["VOO"], 5)["VOO"]
    assert closes == s.adjclose[i - 5 : i]  # strictly before today
    oid = b.submit(OrderIntent("VOO", "buy", notional=100), "x")
    _, fill = b.order_fill(oid)
    assert fill.price == pytest.approx(s.open[i] * 1.001)
    b.set_clock(day, "close")
    assert b.latest_prices(["VOO"])["VOO"] == s.close[i]


def test_splice_extends_history_with_proxy_returns():
    proxy = Series(
        "BIL",
        [date(2020, 1, d) for d in (2, 3, 6, 7)],
        [90, 90, 91, 91],
        [90, 90.9, 91, 91.9],
        [90, 90.9, 91, 91.9],
        {date(2020, 1, 3): 0.1},
    )
    primary = Series("SGOV", [date(2020, 1, 6), date(2020, 1, 7)], [100, 101], [100, 101], [100, 101], {})
    out = splice(primary, proxy)
    assert out.dates[0] == date(2020, 1, 2) and out.spliced_from == "BIL"
    assert out.close[2] == 100 and out.close[0] == pytest.approx(90 * 100 / 91)
    assert out.dividends[date(2020, 1, 3)] == pytest.approx(0.1 * 100 / 91)


def test_fetch_yahoo_parses_payload_and_skips_gaps():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1704205800, 1704292200, 1704378600],
                    "indicators": {
                        "quote": [{"open": [10, None, 12], "close": [11, 11.5, 12.5]}],
                        "adjclose": [{"adjclose": [10.5, 11, 12]}],
                    },
                    "events": {"dividends": {"1704292200": {"amount": 0.2, "date": 1704292200}}},
                }
            ]
        }
    }

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    s = fetch_yahoo("X", START, END, opener=lambda req, timeout: Resp(json.dumps(payload).encode()))
    assert s.dates == [date(2024, 1, 2), date(2024, 1, 4)]
    assert s.dividends == {date(2024, 1, 3): 0.2}
