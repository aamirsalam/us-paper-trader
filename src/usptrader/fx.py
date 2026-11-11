"""USD/INR mid-market rate (ECB reference via Frankfurter, free and keyless)."""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)

SOURCES = (
    "https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR",
    "https://api.frankfurter.app/latest?from=USD&to=INR",
)


def usd_inr(fallback: float) -> tuple[float, str]:
    """(rate, source). Falls back to a configured rate so a flaky API never blocks trading."""
    for url in SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                rate = float(json.load(resp)["rates"]["INR"])
                if 40 < rate < 200:
                    return rate, "ecb"
        except Exception as exc:  # noqa: BLE001
            log.warning("fx source %s failed: %s", url, exc)
    return fallback, "fallback"
