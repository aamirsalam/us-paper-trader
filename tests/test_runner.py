from datetime import date

import boto3
import pytest
from moto import mock_aws

from usptrader.broker.fake import FakeBroker
from usptrader.config import Settings
from usptrader.ledger import DynamoStore, Ledger, LocalStore
from usptrader.models import Dividend
from usptrader.report import build_report
from usptrader.runner import Trader

MON = date(2026, 11, 9)


def make(tmp_path, modes=("baseline",), holidays=None, today=MON):
    settings = Settings(modes=modes, local_ledger_dir=str(tmp_path))
    store = LocalStore(tmp_path)
    brokers: dict[str, FakeBroker] = {}

    def broker_for(m):
        return brokers.setdefault(m, FakeBroker(1000, today=today, holidays=holidays))

    trader = Trader(
        settings, broker_for, store, today=today, rate_fn=lambda: (88.0, "test"), fill_wait_seconds=0
    )
    return trader, settings, store, brokers


def test_full_day_funds_buys_and_snapshots(tmp_path):
    trader, settings, store, brokers = make(tmp_path)
    for phase in ("premarket", "trade", "postclose"):
        assert all(o.ok for o in trader.run(phase)), phase
    led = Ledger("baseline", store, settings.costs)
    assert led.deposited_usd() == 1000
    assert "VOO" in led.positions()
    snap = led.of_type("snapshot")[-1]
    assert snap["mismatches"] == []
    assert snap["inr_if_withdrawn"] < led.deposited_inr()  # FX spread both ways


def test_phases_are_idempotent(tmp_path):
    trader, settings, store, brokers = make(tmp_path)
    for _ in range(2):
        for phase in ("premarket", "trade", "postclose"):
            trader.run(phase)
    led = Ledger("baseline", store, settings.costs)
    assert len(led.of_type("deposit")) == 1
    assert len(led.of_type("plan")) == 1
    assert len(brokers["baseline"].submitted) == 1
    assert len(led.of_type("snapshot")) == 1


def test_holiday_does_nothing(tmp_path):
    trader, settings, store, brokers = make(tmp_path, holidays={MON})
    out = trader.run("trade")
    assert "not a US trading day" in out[0].lines[0]
    assert Ledger("baseline", store, settings.costs).entries == []


def test_dividend_credited_net_of_withholding(tmp_path):
    trader, settings, store, brokers = make(tmp_path)
    trader.run("trade")
    qty = Ledger("baseline", store, settings.costs).positions()["VOO"].qty
    brokers["baseline"].dividends.append(
        Dividend("d1", "VOO", rate=1.80, ex_date=date(2026, 11, 10), payable_date=MON)
    )
    trader.run("postclose")
    trader.run("postclose")  # no double credit
    divs = Ledger("baseline", store, settings.costs).of_type("dividend")
    assert len(divs) == 1
    assert divs[0]["net"] == pytest.approx(qty * 1.80 * 0.75, rel=1e-6)


def test_one_failing_mode_does_not_stop_others(tmp_path):
    trader, settings, store, brokers = make(tmp_path, modes=("baseline", "nonsense"))
    outs = trader.run("premarket")
    assert outs[0].ok and not outs[1].ok
    subject, body = build_report(settings, store, MON, outs)
    assert subject.startswith("⚠")


def test_report_shows_inr_breakdown(tmp_path):
    trader, settings, store, _ = make(tmp_path, modes=("baseline", "conservative"))
    outs = []
    for phase in ("premarket", "trade", "postclose"):
        outs = trader.run(phase)
    subject, body = build_report(settings, store, MON, outs)
    assert "baseline" in subject and "₹" in body and "Schedule FA" in body


@mock_aws
def test_dynamo_store_round_trip():
    boto3.client("dynamodb", region_name="ap-southeast-2").create_table(
        TableName="t",
        KeySchema=[{"AttributeName": "mode", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "mode", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    store = DynamoStore("t", "ap-southeast-2")
    led = Ledger("m", store, Settings().costs)
    led.record_deposit(MON, 1000, 88.25)
    led.record_plan(MON, {"VOO": 1.0}, {"note": "x"})
    again = Ledger("m", store, Settings().costs)
    assert [e["type"] for e in again.entries] == ["deposit", "plan"]
    assert again.entries[0]["rate"] == 88.25
    assert again.latest_plan()["targets"] == {"VOO": 1.0}
