"""Settings come from environment variables. On AWS, secrets are injected from SSM by ECS."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .costs import CostModel

DEFAULT_MODES = ("baseline", "conservative", "balanced")


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    modes: tuple[str, ...] = DEFAULT_MODES
    starting_capital_usd: float = 1000.0
    ledger_backend: str = "local"  # "local" | "dynamodb"
    ledger_table: str = "us-paper-trader-ledger"
    local_ledger_dir: str = "./data/ledger"
    aws_region: str | None = None
    fallback_usdinr: float = 88.0
    costs: CostModel = field(default_factory=CostModel)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    report_to: str = ""
    report_from: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        modes = tuple(m.strip() for m in e("TRADER_MODES", ",".join(DEFAULT_MODES)).split(",") if m.strip())
        costs = CostModel(
            fx_spread_pct=_f("FX_SPREAD_PCT", CostModel.fx_spread_pct),
            remittance_fee_inr=_f("REMITTANCE_FEE_INR", CostModel.remittance_fee_inr),
            dividend_withholding=_f("DIVIDEND_WITHHOLDING", CostModel.dividend_withholding),
            tcs_rate=_f("TCS_RATE", CostModel.tcs_rate),
            tcs_threshold_inr=_f("TCS_THRESHOLD_INR", CostModel.tcs_threshold_inr),
        )
        return cls(
            modes=modes,
            starting_capital_usd=_f("STARTING_CAPITAL_USD", 1000.0),
            ledger_backend=e("LEDGER_BACKEND", "local"),
            ledger_table=e("LEDGER_TABLE", "us-paper-trader-ledger"),
            local_ledger_dir=e("LOCAL_LEDGER_DIR", "./data/ledger"),
            aws_region=e("AWS_REGION") or e("AWS_DEFAULT_REGION"),
            fallback_usdinr=_f("FALLBACK_USDINR", 88.0),
            costs=costs,
            smtp_host=e("SMTP_HOST", ""),
            smtp_port=int(e("SMTP_PORT", "587")),
            smtp_user=e("SMTP_USER", ""),
            smtp_password=e("SMTP_PASSWORD", ""),
            report_to=e("REPORT_TO", ""),
            report_from=e("REPORT_FROM", "") or e("SMTP_USER", ""),
        )

    @staticmethod
    def alpaca_keys(mode: str) -> tuple[str, str]:
        """Each mode trades its own Alpaca paper account: ALPACA_KEY_ID_<MODE> / ALPACA_SECRET_<MODE>."""
        suffix = mode.upper()
        key = os.environ.get(f"ALPACA_KEY_ID_{suffix}")
        secret = os.environ.get(f"ALPACA_SECRET_{suffix}")
        if not key or not secret:
            raise RuntimeError(f"missing ALPACA_KEY_ID_{suffix} / ALPACA_SECRET_{suffix}")
        return key, secret
