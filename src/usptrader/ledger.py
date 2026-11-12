"""Append-only ledger per mode. It, not the broker, is the source of truth.

Alpaca's paper account doesn't simulate dividends, withholding, FX or T+1
settlement for a cash account, so we track all of that here and reconcile
positions against the broker.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .costs import CostModel, indian_fy
from .models import Fill, OrderIntent

EPS = 1e-9


class LedgerStore(Protocol):
    def append(self, mode: str, entry: dict[str, Any]) -> None: ...
    def load(self, mode: str) -> list[dict[str, Any]]: ...


class LocalStore:
    """JSON-lines file per mode. Used for local runs and tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, mode: str) -> Path:
        return self.root / f"{mode}.jsonl"

    def append(self, mode: str, entry: dict[str, Any]) -> None:
        with self._path(mode).open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def load(self, mode: str) -> list[dict[str, Any]]:
        path = self._path(mode)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class MemoryStore:
    """In-process store for backtests."""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def append(self, mode: str, entry: dict[str, Any]) -> None:
        self._data[mode].append(entry)

    def load(self, mode: str) -> list[dict[str, Any]]:
        return list(self._data[mode])


class DynamoStore:
    """DynamoDB table with pk=mode, sk=<epoch_ns>#<uuid>."""

    def __init__(self, table_name: str, region: str | None = None) -> None:
        import boto3

        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def append(self, mode: str, entry: dict[str, Any]) -> None:
        item = json.loads(json.dumps(entry), parse_float=Decimal)
        item["mode"] = mode
        item["sk"] = f"{time.time_ns():020d}#{uuid.uuid4().hex[:8]}"
        self.table.put_item(Item=item)

    def load(self, mode: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("mode").eq(mode)}
        while True:
            resp = self.table.query(**kwargs)
            items.extend(resp["Items"])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        items.sort(key=lambda i: i["sk"])
        out = []
        for item in items:
            item.pop("mode", None)
            item.pop("sk", None)
            out.append(json.loads(json.dumps(item, default=_decimal_default)))
        return out


def _decimal_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o))


@dataclass
class Position:
    qty: float = 0.0
    cost: float = 0.0  # total USD cost basis of the open qty

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty > EPS else 0.0


class Ledger:
    def __init__(self, mode: str, store: LedgerStore, costs: CostModel) -> None:
        self.mode = mode
        self.store = store
        self.costs = costs
        self.entries: list[dict[str, Any]] = []
        self._by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._pos_cache: tuple[int, dict[str, Position]] | None = None
        self._cash_cache: tuple[int, float] | None = None
        for entry in store.load(mode):
            self._index(entry)

    def _index(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)
        self._by_type[entry["type"]].append(entry)

    # ---- writes -------------------------------------------------------------------------------
    def _add(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.store.append(self.mode, entry)
        self._index(entry)
        return entry

    def record_deposit(self, on: date, usd: float, mid_rate: float) -> dict[str, Any]:
        inr = self.costs.inr_to_fund(usd, mid_rate)
        fy = indian_fy(on)
        remitted = sum(e["inr"] for e in self.of_type("deposit") if e["fy"] == fy)
        return self._add(
            {
                "type": "deposit",
                "date": on.isoformat(),
                "usd": round(usd, 2),
                "rate": mid_rate,
                "inr": round(inr, 2),
                "fy": fy,
                "tcs_inr": self.costs.tcs(remitted, inr),
            }
        )

    def record_plan(
        self,
        on: date,
        targets: dict[str, float],
        state: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`state` is the strategy's own memory; `meta` is runner context (earnings, notes)."""
        entry = {"type": "plan", "date": on.isoformat(), "targets": targets, "state": state}
        if meta:
            entry["meta"] = meta
        return self._add(entry)

    def record_risk(self, on: date, action: str, **values: Any) -> dict[str, Any]:
        """action: halt | resume | pause"""
        return self._add({"type": "risk", "date": on.isoformat(), "action": action, **values})

    def record_order(self, on: date, order_id: str, intent: OrderIntent) -> dict[str, Any]:
        return self._add(
            {
                "type": "order",
                "date": on.isoformat(),
                "order_id": order_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "notional": intent.notional,
                "qty": intent.qty,
                "reason": intent.reason,
            }
        )

    def record_fill(self, fill: Fill, settles_on: date) -> dict[str, Any] | None:
        if fill.qty <= EPS or self.has_fill(fill.order_id):
            return None
        gross = fill.qty * fill.price
        fees = self.costs.sell_fees(gross, fill.qty) if fill.side == "sell" else 0.0
        realized = 0.0
        if fill.side == "sell":
            pos = self.positions().get(fill.symbol, Position())
            realized = gross - fees - pos.avg_cost * fill.qty
        return self._add(
            {
                "type": "fill",
                "date": fill.filled_on.isoformat(),
                "settle": settles_on.isoformat(),
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "side": fill.side,
                "qty": round(fill.qty, 9),
                "price": fill.price,
                "fees": fees,
                "realized": round(realized, 6),
            }
        )

    def record_dividend(self, action_id: str, symbol: str, qty: float, rate: float, paid_on: date):
        if any(e.get("action_id") == action_id for e in self.of_type("dividend")):
            return None
        gross = qty * rate
        net, withheld = self.costs.dividend_net(gross)
        return self._add(
            {
                "type": "dividend",
                "date": paid_on.isoformat(),
                "action_id": action_id,
                "symbol": symbol,
                "qty": qty,
                "gross": round(gross, 6),
                "withheld": withheld,
                "net": net,
            }
        )

    def record_rejection(self, on: date, intent: OrderIntent, reason: str) -> dict[str, Any]:
        return self._add(
            {
                "type": "rejection",
                "date": on.isoformat(),
                "symbol": intent.symbol,
                "side": intent.side,
                "reason": reason,
            }
        )

    def record_snapshot(self, on: date, **values: Any) -> dict[str, Any]:
        return self._add({"type": "snapshot", "date": on.isoformat(), **values})

    def record_note(self, on: date, text: str) -> dict[str, Any]:
        return self._add({"type": "note", "date": on.isoformat(), "text": text})

    # ---- reads --------------------------------------------------------------------------------
    def of_type(self, kind: str) -> list[dict[str, Any]]:
        """Entries of one type, oldest first. Treat as read-only."""
        return self._by_type.get(kind, [])

    def on(self, kind: str, day: date) -> list[dict[str, Any]]:
        d = day.isoformat()
        out = []
        for e in reversed(self.of_type(kind)):  # entries are chronological: stop once we're past `day`
            if e["date"] == d:
                out.append(e)
            elif e["date"] < d:
                break
        return out[::-1]

    def has_fill(self, order_id: str) -> bool:
        return any(e.get("order_id") == order_id for e in self.of_type("fill"))

    def open_orders(self) -> list[dict[str, Any]]:
        filled = {e["order_id"] for e in self.of_type("fill")}
        closed = {e["order_id"] for e in self.of_type("note") if "order_id" in e}
        return [e for e in self.of_type("order") if e["order_id"] not in filled | closed]

    def close_order(self, on: date, order_id: str, why: str) -> None:
        self._add({"type": "note", "date": on.isoformat(), "order_id": order_id, "text": why})

    def latest_plan(self) -> dict[str, Any] | None:
        plans = self.of_type("plan")
        return plans[-1] if plans else None

    def positions(self, as_of: date | None = None) -> dict[str, Position]:
        """Open positions from fills dated on or before `as_of` (all fills if None)."""
        n = len(self.of_type("fill"))
        if as_of is None and self._pos_cache and self._pos_cache[0] == n:
            return {s: Position(p.qty, p.cost) for s, p in self._pos_cache[1].items()}
        result = self._positions(as_of)
        if as_of is None:
            self._pos_cache = (n, {s: Position(p.qty, p.cost) for s, p in result.items()})
        return result

    def _positions(self, as_of: date | None) -> dict[str, Position]:
        pos: dict[str, Position] = defaultdict(Position)
        for e in self.of_type("fill"):
            if as_of and date.fromisoformat(e["date"]) > as_of:
                continue
            p = pos[e["symbol"]]
            if e["side"] == "buy":
                p.qty += e["qty"]
                p.cost += e["qty"] * e["price"]
            else:
                avg = p.avg_cost
                p.qty -= e["qty"]
                p.cost -= avg * e["qty"]
                if p.qty <= EPS:
                    p.qty, p.cost = 0.0, 0.0
        return {s: p for s, p in pos.items() if p.qty > EPS}

    def cash(self) -> float:
        key = len(self.entries)
        if self._cash_cache and self._cash_cache[0] == key:
            return self._cash_cache[1]
        value = self._cash()
        self._cash_cache = (key, value)
        return value

    def _cash(self) -> float:
        total = sum(e["usd"] for e in self.of_type("deposit"))
        for e in self.of_type("fill"):
            gross = e["qty"] * e["price"]
            total += -gross if e["side"] == "buy" else gross - e["fees"]
        total += sum(e["net"] for e in self.of_type("dividend"))
        return round(total, 6)

    def settled_cash(self, as_of: date) -> float:
        """Cash usable for buys in a cash account: sale proceeds count only once settled."""
        unsettled = sum(
            e["qty"] * e["price"] - e["fees"]
            for e in self.of_type("fill")
            if e["side"] == "sell" and date.fromisoformat(e["settle"]) > as_of
        )
        return round(self.cash() - unsettled, 6)

    def deposited_usd(self) -> float:
        return sum(e["usd"] for e in self.of_type("deposit"))

    def deposited_inr(self) -> float:
        return sum(e["inr"] for e in self.of_type("deposit"))

    def weighted_deposit_rate(self) -> float:
        usd = self.deposited_usd()
        return sum(e["usd"] * e["rate"] for e in self.of_type("deposit")) / usd if usd else 0.0

    def realized_pnl(self) -> float:
        return sum(e["realized"] for e in self.of_type("fill"))

    def dividends(self) -> tuple[float, float]:
        divs = self.of_type("dividend")
        return sum(e["gross"] for e in divs), sum(e["withheld"] for e in divs)
