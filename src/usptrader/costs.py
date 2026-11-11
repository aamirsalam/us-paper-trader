"""What an Indian resident actually pays to trade US stocks under LRS.

Defaults are deliberately a little pessimistic. Every value is overridable from
the environment (see config.py) so you can plug in your real bank/broker quotes.
Verify regulatory rates (SEC fee, FINRA TAF, TCS) before relying on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CostModel:
    # Bank/platform markup over the mid-market USD/INR rate, each direction (incl. GST on conversion).
    fx_spread_pct: float = 0.0075
    # Fixed charge per remittance or repatriation, in INR (wire fee, GST etc.).
    remittance_fee_inr: float = 0.0
    # US withholding on dividends for Indian residents under the India-US tax treaty.
    dividend_withholding: float = 0.25
    # TCS on LRS investment remittances above the per-FY threshold. Refundable, but locks cash.
    tcs_rate: float = 0.20
    tcs_threshold_inr: float = 1_000_000.0
    # Regulatory pass-through fees on sells. Alpaca passes these through on live accounts.
    sec_fee_rate: float = 0.0000278  # USD per USD of sell proceeds (verify current SEC rate)
    finra_taf_per_share: float = 0.000166
    finra_taf_max: float = 8.30

    def inr_to_fund(self, usd: float, mid_rate: float) -> float:
        """INR leaving your Indian bank to land `usd` in the brokerage account."""
        return usd * mid_rate * (1 + self.fx_spread_pct) + self.remittance_fee_inr

    def inr_on_withdrawal(self, usd: float, mid_rate: float) -> float:
        """INR reaching your Indian bank if `usd` is withdrawn today."""
        return max(0.0, usd * mid_rate * (1 - self.fx_spread_pct) - self.remittance_fee_inr)

    def dividend_net(self, gross_usd: float) -> tuple[float, float]:
        withheld = round(gross_usd * self.dividend_withholding, 6)
        return round(gross_usd - withheld, 6), withheld

    def tcs(self, remitted_so_far_fy_inr: float, amount_inr: float) -> float:
        """TCS collected on this remittance given what was already sent this financial year."""
        taxable_before = max(0.0, remitted_so_far_fy_inr - self.tcs_threshold_inr)
        taxable_after = max(0.0, remitted_so_far_fy_inr + amount_inr - self.tcs_threshold_inr)
        return round((taxable_after - taxable_before) * self.tcs_rate, 2)

    def sell_fees(self, proceeds_usd: float, qty: float) -> float:
        sec = proceeds_usd * self.sec_fee_rate
        taf = min(qty * self.finra_taf_per_share, self.finra_taf_max)
        return round(sec + taf, 4)


def indian_fy(d: date) -> str:
    """Indian financial year label, e.g. 2026-11-11 -> 'FY2026-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY{start}-{str(start + 1)[-2:]}"
