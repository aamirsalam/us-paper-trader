"""Earnings dates for single stocks, so the aggressive mode can stay out of overnight gaps.

Sources, all free:
  * Nasdaq earnings calendar (keyless): confirmed upcoming dates for the live trader.
  * SEC EDGAR 8-K Item 2.02 filings ("Results of Operations"): the actual date of every
    past earnings release. Used for backtests, and live as a fallback that projects the
    next date from the company's own reporting rhythm. SEC asks for a contact in the
    User-Agent: set SEC_USER_AGENT="Your Name you@example.com".

If no source answers, the calendar reports "unknown" and the trader fails safe:
no new single-stock buys that day, existing holdings untouched.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import statistics
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    day: date
    confirmed: bool  # False = projected from past rhythm, date uncertain by about a week
    source: str


class EarningsCalendar(Protocol):
    def upcoming(self, symbols: list[str], start: date, end: date) -> dict[str, EarningsEvent]:
        """Events in [start, end]. Raises if the source can't answer."""


class NoEarnings:
    """Blackout disabled (e.g. a backtest without earnings history). Always answers 'none'."""

    source = "disabled"

    def upcoming(self, symbols, start, end):
        return {}


def _get_json(url: str, headers: dict[str, str], timeout: int = 15):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class NasdaqCalendar:
    source = "nasdaq"
    URL = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
    HEADERS = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }

    def __init__(self, fetch=_get_json) -> None:
        self._fetch = fetch

    def upcoming(self, symbols, start, end):
        wanted = {s.upper() for s in symbols}
        found: dict[str, EarningsEvent] = {}
        day = start
        while day <= end:
            if day.weekday() < 5:
                payload = self._fetch(self.URL.format(day=day.isoformat()), self.HEADERS)
                rows = ((payload or {}).get("data") or {}).get("rows") or []
                for row in rows:
                    sym = str(row.get("symbol", "")).upper()
                    if sym in wanted and sym not in found:
                        found[sym] = EarningsEvent(sym, day, True, self.source)
            day += timedelta(days=1)
        return found


class SecEarnings:
    """Earnings release dates from SEC 8-K Item 2.02 filings."""

    source = "sec"
    TICKERS = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
    MORE = "https://data.sec.gov/submissions/{name}"

    def __init__(self, user_agent: str | None = None, fetch=_get_json, cache_dir: str | None = None) -> None:
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT", "")
        if not self.user_agent:
            raise RuntimeError("SEC needs a contact: set SEC_USER_AGENT='Your Name you@example.com'")
        self._fetch = fetch
        self._ciks: dict[str, int] | None = None
        self._history: dict[str, list[date]] = {}
        self.cache = Path(cache_dir) if cache_dir else None

    def _get(self, url: str):
        time.sleep(0.15)  # SEC allows 10 requests/second; stay well under
        return self._fetch(url, {"User-Agent": self.user_agent, "Accept": "application/json"})

    def _cik(self, symbol: str) -> int | None:
        if self._ciks is None:
            data = self._get(self.TICKERS)
            self._ciks = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        return self._ciks.get(symbol.upper().replace(".", "-"))

    def history(self, symbol: str) -> list[date]:
        symbol = symbol.upper()
        if symbol in self._history:
            return self._history[symbol]
        cached = self.cache / f"{symbol}.csv" if self.cache else None
        if cached and cached.exists() and time.time() - cached.stat().st_mtime < 86400:
            dates = [date.fromisoformat(r[0]) for r in csv.reader(cached.open()) if r]
            self._history[symbol] = dates
            return dates
        cik = self._cik(symbol)
        if cik is None:
            self._history[symbol] = []
            return []
        sub = self._get(self.SUBMISSIONS.format(cik=cik))
        blocks = [sub["filings"]["recent"]]
        for extra in sub["filings"].get("files", []):
            blocks.append(self._get(self.MORE.format(name=extra["name"])))
        dates = set()
        for b in blocks:
            for form, items, filed in zip(b["form"], b["items"], b["filingDate"], strict=False):
                if form in ("8-K", "8-K/A") and "2.02" in (items or ""):
                    dates.add(date.fromisoformat(filed))
        out = sorted(dates)
        self._history[symbol] = out
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            with cached.open("w", newline="") as fh:
                csv.writer(fh).writerows([[d.isoformat()] for d in out])
        return out

    def projected(self, symbol: str, today: date) -> date | None:
        """Next release estimated from the median gap between the last eight."""
        past = [d for d in self.history(symbol) if d <= today]
        if len(past) < 3:
            return None
        gaps = [(b - a).days for a, b in zip(past[-9:], past[-8:], strict=False)]
        gaps = [g for g in gaps if 40 <= g <= 140] or [91]
        return past[-1] + timedelta(days=round(statistics.median(gaps)))

    def upcoming(self, symbols, start, end):
        found = {}
        for sym in symbols:
            guess = self.projected(sym, start)
            # A projection is fuzzy: treat anything within a week either side of the window as near.
            if guess and start - timedelta(days=7) <= guess <= end + timedelta(days=7):
                found[sym.upper()] = EarningsEvent(sym.upper(), max(guess, start), False, "sec-projection")
        return found


class HistoricalEarnings:
    """Known release dates per symbol, for backtests (from SEC history or a CSV of symbol,date)."""

    source = "history"

    def __init__(self, dates: dict[str, list[date]]) -> None:
        self.dates = {s.upper(): sorted(d) for s, d in dates.items()}

    @classmethod
    def from_sec(cls, symbols: list[str], sec: SecEarnings) -> HistoricalEarnings:
        return cls({s: sec.history(s) for s in symbols})

    @classmethod
    def from_csv(cls, path: str) -> HistoricalEarnings:
        out: dict[str, list[date]] = {}
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                out.setdefault(row["symbol"].upper(), []).append(date.fromisoformat(row["date"]))
        return cls(out)

    def upcoming(self, symbols, start, end):
        found = {}
        for sym in symbols:
            for d in self.dates.get(sym.upper(), []):
                if start <= d <= end:
                    found[sym.upper()] = EarningsEvent(sym.upper(), d, True, self.source)
                    break
        return found


class ChainCalendar:
    """Try each source in order; the first that answers wins."""

    def __init__(self, *sources) -> None:
        self.sources = [s for s in sources if s is not None]
        self.source = "none"

    def upcoming(self, symbols, start, end):
        errors = []
        for src in self.sources:
            try:
                result = src.upcoming(symbols, start, end)
                self.source = src.source
                return result
            except Exception as exc:  # noqa: BLE001
                log.warning("earnings source %s failed: %s", src.source, exc)
                errors.append(f"{src.source}: {exc}")
        raise RuntimeError("no earnings source answered (" + "; ".join(errors) + ")")


def live_calendar() -> ChainCalendar:
    sec = None
    if os.environ.get("SEC_USER_AGENT"):
        sec = SecEarnings()
    return ChainCalendar(NasdaqCalendar(), sec)
