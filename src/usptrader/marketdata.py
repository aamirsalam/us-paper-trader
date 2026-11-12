"""Daily history for backtests: Yahoo Finance chart API (free, keyless, unofficial), cached locally.

Per symbol we keep split-adjusted open/close (what you'd actually have traded at), the
split+dividend adjusted close (for signals, same as the live trader's Adjustment.ALL bars),
and cash dividends per share, so the backtest can pay them out net of US withholding.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (compatible; us-paper-trader/0.1; backtest)"
CHART = (
    "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
    "?period1={p1}&period2={p2}&interval=1d&events=div,split&includeAdjustedClose=true"
)


@dataclass
class Series:
    symbol: str
    dates: list[date] = field(default_factory=list)
    open: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)
    adjclose: list[float] = field(default_factory=list)
    dividends: dict[date, float] = field(default_factory=dict)  # ex-date -> USD per share
    spliced_from: str = ""

    def __post_init__(self) -> None:
        self._index = {d: i for i, d in enumerate(self.dates)}

    def reindex(self) -> None:
        self._index = {d: i for i, d in enumerate(self.dates)}

    def idx(self, day: date) -> int | None:
        return self._index.get(day)

    def to_json(self) -> dict:
        return {
            "symbol": self.symbol,
            "dates": [d.isoformat() for d in self.dates],
            "open": self.open,
            "close": self.close,
            "adjclose": self.adjclose,
            "dividends": {d.isoformat(): v for d, v in self.dividends.items()},
        }

    @classmethod
    def from_json(cls, raw: dict) -> Series:
        return cls(
            raw["symbol"],
            [date.fromisoformat(d) for d in raw["dates"]],
            raw["open"],
            raw["close"],
            raw["adjclose"],
            {date.fromisoformat(d): v for d, v in raw["dividends"].items()},
        )


@dataclass
class MarketData:
    series: dict[str, Series]
    usdinr: dict[date, float]
    calendar: list[date]  # trading days, from the benchmark's history

    def rate(self, day: date, fallback: float = 88.0) -> float:
        if day in self.usdinr:
            return self.usdinr[day]
        earlier = [d for d in self._fx_days if d <= day]
        return self.usdinr[earlier[-1]] if earlier else fallback

    @property
    def _fx_days(self) -> list[date]:
        if not hasattr(self, "_fx_sorted"):
            self._fx_sorted = sorted(self.usdinr)
        return self._fx_sorted


# ---- Yahoo -------------------------------------------------------------------------------------------
def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def fetch_yahoo(symbol: str, start: date, end: date, opener=urllib.request.urlopen) -> Series:
    url = CHART.format(sym=symbol, p1=_epoch(start), p2=_epoch(end + timedelta(days=1)))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with opener(req, timeout=30) as resp:
                payload = json.load(resp)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                raise RuntimeError(f"Yahoo fetch failed for {symbol}: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    result = (payload.get("chart") or {}).get("result")
    if not result:
        raise RuntimeError(f"no Yahoo data for {symbol}: {payload.get('chart', {}).get('error')}")
    r = result[0]
    stamps = r.get("timestamp") or []
    quote = r["indicators"]["quote"][0]
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or quote["close"]
    s = Series(symbol)
    for i, ts in enumerate(stamps):
        o, c, a = quote["open"][i], quote["close"][i], adj[i]
        if None in (o, c, a) or c <= 0:
            continue
        s.dates.append(datetime.fromtimestamp(ts, ET).date())
        s.open.append(float(o))
        s.close.append(float(c))
        s.adjclose.append(float(a))
    for ev in (r.get("events") or {}).get("dividends", {}).values():
        d = datetime.fromtimestamp(ev["date"], ET).date()
        s.dividends[d] = s.dividends.get(d, 0.0) + float(ev["amount"])
    s.reindex()
    return s


def fetch_usdinr(start: date, end: date, opener=urllib.request.urlopen) -> dict[date, float]:
    s = fetch_yahoo("INR=X", start, end, opener)
    return {d: c for d, c in zip(s.dates, s.close, strict=True) if 40 < c < 200}


class Cache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, start: date, end: date):
        path = self.root / f"{key.replace('=', '_')}.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        if date.fromisoformat(raw["start"]) <= start and date.fromisoformat(raw["end"]) >= end:
            return raw["data"]
        return None

    def put(self, key: str, start: date, end: date, data) -> None:
        path = self.root / f"{key.replace('=', '_')}.json"
        path.write_text(json.dumps({"start": start.isoformat(), "end": end.isoformat(), "data": data}))


def splice(primary: Series, proxy: Series) -> Series:
    """Extend `primary` back in time using `proxy`'s returns (e.g. SGOV before 2020 from BIL)."""
    if not primary.dates or not proxy.dates or proxy.dates[0] >= primary.dates[0]:
        return primary
    first = primary.dates[0]
    j = proxy.idx(first)
    if j is None:
        return primary
    k_close = primary.close[0] / proxy.close[j]
    k_adj = primary.adjclose[0] / proxy.adjclose[j]
    out = Series(primary.symbol, spliced_from=proxy.symbol)
    for i, d in enumerate(proxy.dates):
        if d >= first:
            break
        out.dates.append(d)
        out.open.append(proxy.open[i] * k_close)
        out.close.append(proxy.close[i] * k_close)
        out.adjclose.append(proxy.adjclose[i] * k_adj)
        if d in proxy.dividends:
            out.dividends[d] = proxy.dividends[d] * k_close
    out.dates += primary.dates
    out.open += primary.open
    out.close += primary.close
    out.adjclose += primary.adjclose
    out.dividends.update(primary.dividends)
    out.reindex()
    return out


SPLICES = {"SGOV": "BIL"}  # SGOV launched May 2020; BIL (1-3 month T-bills) since 2007


def load_yahoo(symbols: list[str], start: date, end: date, cache_dir: str | Path) -> MarketData:
    cache = Cache(cache_dir)
    fetch_start = start - timedelta(days=420)  # warm-up for 200-day averages and 6-month returns
    series: dict[str, Series] = {}
    wanted = set(symbols) | {"VOO"} | {SPLICES[s] for s in symbols if s in SPLICES}
    for sym in sorted(wanted):
        raw = cache.get(sym, fetch_start, end)
        if raw is None:
            log.info("fetching %s from Yahoo", sym)
            s = fetch_yahoo(sym, fetch_start, end)
            cache.put(sym, fetch_start, end, s.to_json())
            time.sleep(0.3)
        else:
            s = Series.from_json(raw)
        series[sym] = s
    for sym, proxy in SPLICES.items():
        if sym in series and proxy in series:
            series[sym] = splice(series[sym], series[proxy])
    fx = cache.get("INR=X", fetch_start, end)
    if fx is None:
        rates = fetch_usdinr(fetch_start, end)
        cache.put("INR=X", fetch_start, end, {d.isoformat(): v for d, v in rates.items()})
    else:
        rates = {date.fromisoformat(d): v for d, v in fx.items()}
    calendar = [d for d in series["VOO"].dates if start <= d <= end]
    return MarketData(series, rates, calendar)


def synthetic(symbols: list[str], start: date, end: date, seed: int = 11) -> MarketData:
    """Random-walk market for offline tests and CI. Not meant to say anything about strategies."""
    days, d = [], start - timedelta(days=420)
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    series = {}
    for sym in sorted(set(symbols) | {"VOO"}):
        rng = random.Random(f"{seed}-{sym}")
        drift = 0.00005 if sym in ("SGOV", "BIL") else rng.uniform(-0.0002, 0.0008)
        vol = 0.0003 if sym in ("SGOV", "BIL") else rng.uniform(0.008, 0.025)
        price = 20 + rng.random() * 300
        s = Series(sym)
        for i, day in enumerate(days):
            o = price * math.exp(rng.gauss(0, vol / 3))
            price = o * math.exp(drift + rng.gauss(0, vol))
            s.dates.append(day)
            s.open.append(round(o, 4))
            s.close.append(round(price, 4))
            s.adjclose.append(round(price, 4))
            if i % 63 == 30:
                s.dividends[day] = round(price * 0.004, 4)
        s.reindex()
        series[sym] = s
    rates, r = {}, 66.0
    rng = random.Random(seed)
    for day in days:
        r *= math.exp(0.00012 + rng.gauss(0, 0.003))
        rates[day] = round(r, 4)
    return MarketData(series, rates, [x for x in days if start <= x <= end])
