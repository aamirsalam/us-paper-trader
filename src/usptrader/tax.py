"""Indian income tax on a resident's US holdings, so modes are compared on what you'd keep.

What is modelled (verify with a CA before filing; rates and rules change):
  * Lots are matched first-in, first-out, each with its own purchase date.
  * Cost and sale proceeds are each converted to rupees at the rate on the last day of the
    month before that transaction (Rule 115 uses SBI's TT buying rate; we use the mid-market
    rate as a stand-in). So a weaker rupee is taxed as part of the gain.
  * Held more than 24 months: long-term, 12.5% flat. Otherwise short-term, at your slab rate.
    Both plus surcharge and 4% cess.
  * Losses: short-term losses offset any gain; long-term losses only long-term gains; unused
    losses carry forward (the 8-year expiry is not modelled).
  * Dividends: taxed at slab on the gross amount, less a foreign tax credit (Form 67) for the
    25% the US withheld, capped at the Indian tax on that income.

What is not: basic exemption and rebates, advance-tax interest, the chance that frequent
trading is assessed as business income, and state-level anything. TCS is an advance you get
back, so it isn't a cost here.

After-tax value: tax for each completed financial year is treated as paid out of the
portfolio at that year's end (so later growth is lost on it), and today's figure also takes
off the tax on this year's gains so far and on selling everything now.
"""

from __future__ import annotations

import os
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from .costs import indian_fy
from .ledger import EPS, Ledger


@dataclass(frozen=True)
class TaxModel:
    slab_rate: float = 0.30  # your marginal rate; set TAX_SLAB_RATE to your own
    surcharge: float = 0.0
    cess: float = 0.04
    ltcg_rate: float = 0.125
    lt_months: int = 24

    @property
    def slab_eff(self) -> float:
        return self.slab_rate * (1 + self.surcharge) * (1 + self.cess)

    @property
    def ltcg_eff(self) -> float:
        return self.ltcg_rate * (1 + self.surcharge) * (1 + self.cess)

    @classmethod
    def from_env(cls) -> TaxModel:
        def f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return float(raw) if raw not in (None, "") else default

        return cls(
            slab_rate=f("TAX_SLAB_RATE", cls.slab_rate),
            surcharge=f("TAX_SURCHARGE", cls.surcharge),
            cess=f("TAX_CESS", cls.cess),
            ltcg_rate=f("TAX_LTCG_RATE", cls.ltcg_rate),
            lt_months=int(f("TAX_LT_MONTHS", cls.lt_months)),
        )


@dataclass
class Lot:
    symbol: str
    bought: date
    qty: float
    price: float  # USD per share
    rate: float  # USD/INR used for the cost


@dataclass
class FYTax:
    fy: str
    stcg: float = 0.0  # net short-term gain realised in the year, INR (can be negative)
    ltcg: float = 0.0
    dividends: float = 0.0  # gross, INR
    us_withheld: float = 0.0  # INR
    carry_in_st: float = 0.0  # losses brought forward into this year
    carry_in_lt: float = 0.0
    taxable_st: float = 0.0
    taxable_lt: float = 0.0
    tax_gains: float = 0.0
    tax_dividends: float = 0.0  # after foreign tax credit
    ftc: float = 0.0
    carry_out_st: float = 0.0
    carry_out_lt: float = 0.0

    @property
    def tax(self) -> float:
        return self.tax_gains + self.tax_dividends


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30,
                      31, 31, 30, 31, 30, 31][m - 1])  # fmt: skip
    return date(y, m, day)


def is_long_term(bought: date, sold: date, months: int) -> bool:
    return sold > add_months(bought, months)


def fy_of(label: str) -> int:
    return int(label[2:6])


def fy_end(label: str) -> date:
    return date(fy_of(label) + 1, 3, 31)


def set_off(st: float, lt: float, carry_st: float, carry_lt: float) -> tuple[float, float, float, float]:
    """(taxable_st, taxable_lt, carry_st_out, carry_lt_out) under the Indian set-off rules."""
    if st < 0 < lt:  # this year's short-term loss can absorb long-term gains
        use = min(-st, lt)
        st, lt = st + use, lt - use
    if st > 0 and carry_st > 0:
        use = min(st, carry_st)
        st, carry_st = st - use, carry_st - use
    if lt > 0 and carry_st > 0:
        use = min(lt, carry_st)
        lt, carry_st = lt - use, carry_st - use
    if lt > 0 and carry_lt > 0:
        use = min(lt, carry_lt)
        lt, carry_lt = lt - use, carry_lt - use
    if st < 0:
        carry_st, st = carry_st - st, 0.0
    if lt < 0:
        carry_lt, lt = carry_lt - lt, 0.0
    return st, lt, carry_st, carry_lt


@dataclass
class AfterTax:
    value_inr: float  # what you'd keep if you sold everything and brought it home today
    tax_paid_inr: float  # completed financial years
    tax_accrued_inr: float  # this financial year, on what's been realised so far
    tax_on_exit_inr: float  # extra tax from selling the open positions now
    stcg_inr: float  # all realised short-term gains to date (net)
    ltcg_inr: float
    drag_factor: float  # share of the portfolio left after past years' tax came out of it

    @property
    def tax_total_inr(self) -> float:
        return self.tax_paid_inr + self.tax_accrued_inr + self.tax_on_exit_inr


@dataclass
class TaxBook:
    model: TaxModel
    rate_for: Callable[[date], float]
    years: dict[str, FYTax] = field(default_factory=dict)
    lots: dict[str, deque[Lot]] = field(default_factory=dict)

    def year(self, label: str) -> FYTax:
        if label not in self.years:
            self.years[label] = FYTax(label)
        return self.years[label]

    def finalise(self, through: str) -> None:
        """Apply set-off, carry-forward and rates to every year up to and including `through`."""
        labels = sorted(set(self.years) | {through}, key=fy_of)
        first, last = fy_of(labels[0]), fy_of(through)
        carry_st = carry_lt = 0.0
        for start in range(first, max(last, fy_of(labels[-1])) + 1):
            y = self.year(f"FY{start}-{str(start + 1)[-2:]}")
            y.carry_in_st, y.carry_in_lt = carry_st, carry_lt
            self._rate_year(y)
            carry_st, carry_lt = y.carry_out_st, y.carry_out_lt
        self.years = dict(sorted(self.years.items(), key=lambda kv: fy_of(kv[0])))

    def what_if(self, y: FYTax, extra_st: float, extra_lt: float) -> float:
        """Tax on gains for year y if these extra gains were realised too. Stores nothing."""
        st, lt, _, _ = set_off(y.stcg + extra_st, y.ltcg + extra_lt, y.carry_in_st, y.carry_in_lt)
        return st * self.model.slab_eff + lt * self.model.ltcg_eff

    def _rate_year(self, y: FYTax) -> None:
        st, lt, c_st, c_lt = set_off(y.stcg, y.ltcg, y.carry_in_st, y.carry_in_lt)
        tax_gains = st * self.model.slab_eff + lt * self.model.ltcg_eff
        gross_div_tax = y.dividends * self.model.slab_eff
        y.ftc = min(y.us_withheld, gross_div_tax)
        y.taxable_st, y.taxable_lt = st, lt
        y.tax_gains, y.tax_dividends = tax_gains, gross_div_tax - y.ftc
        y.carry_out_st, y.carry_out_lt = c_st, c_lt

    def exit_gains(self, prices: dict[str, float], today: date) -> tuple[float, float]:
        """(short-term, long-term) rupee gain from selling every open lot at `prices` today."""
        rate_now = self.rate_for(today)
        st = lt = 0.0
        for symbol, lots in self.lots.items():
            price = prices.get(symbol)
            if price is None:
                continue
            for lot in lots:
                gain = lot.qty * (price * rate_now - lot.price * lot.rate)
                if is_long_term(lot.bought, today, self.model.lt_months):
                    lt += gain
                else:
                    st += gain
        return st, lt


def rule115_rates(ledger: Ledger) -> Callable[[date], float]:
    """Rate lookup built from what the ledger has seen: last known rate before the 1st of the month."""
    known: dict[date, float] = {}
    for e in ledger.entries:
        if e["type"] in ("deposit", "snapshot", "fill", "dividend") and e.get("rate"):
            known[date.fromisoformat(e["date"])] = float(e["rate"])
    days = sorted(known)

    def lookup(d: date) -> float:
        if not days:
            return 0.0
        i = bisect_left(days, d.replace(day=1))  # first known day in d's month or later
        if i > 0:
            return known[days[i - 1]]
        j = bisect_right(days, d)  # nothing before this month yet: latest rate up to d
        return known[days[j - 1]] if j > 0 else known[days[0]]

    return lookup


def month_end_before(rate_on: Callable[[date], float]) -> Callable[[date], float]:
    """Wrap a daily rate function (e.g. backtest history) into the Rule 115 lookup."""
    return lambda d: rate_on(d.replace(day=1) - timedelta(days=1))


def build(
    ledger: Ledger, model: TaxModel, today: date, rate_for: Callable[[date], float] | None = None
) -> TaxBook:
    book = TaxBook(model, rate_for or rule115_rates(ledger))
    for e in ledger.entries:
        if e["type"] == "fill":
            d = date.fromisoformat(e["date"])
            rate = book.rate_for(d) or e.get("rate", 0.0)
            lots = book.lots.setdefault(e["symbol"], deque())
            if e["side"] == "buy":
                lots.append(Lot(e["symbol"], d, e["qty"], e["price"], rate))
                continue
            year = book.year(indian_fy(d))
            net_per_share = (e["qty"] * e["price"] - e["fees"]) / e["qty"]
            left = e["qty"]
            while left > EPS and lots:
                lot = lots[0]
                take = min(left, lot.qty)
                gain = take * (net_per_share * rate - lot.price * lot.rate)
                if is_long_term(lot.bought, d, model.lt_months):
                    year.ltcg += gain
                else:
                    year.stcg += gain
                lot.qty -= take
                left -= take
                if lot.qty <= EPS:
                    lots.popleft()
        elif e["type"] == "dividend":
            d = date.fromisoformat(e["date"])
            rate = book.rate_for(d) or e.get("rate", 0.0)
            year = book.year(indian_fy(d))
            year.dividends += e["gross"] * rate
            year.us_withheld += e["withheld"] * rate
    book.finalise(indian_fy(today))
    return book


def after_tax(
    ledger: Ledger,
    model: TaxModel,
    today: date,
    rate_for: Callable[[date], float] | None = None,
) -> AfterTax | None:
    """After-tax position as of the latest snapshot. None until there is a snapshot."""
    snaps = ledger.of_type("snapshot")
    if not snaps:
        return None
    book = build(ledger, model, today, rate_for)
    last = snaps[-1]
    current = indian_fy(today)

    # Tax for each finished year comes out of the portfolio at that year's end.
    factor, paid = 1.0, 0.0
    for label, y in book.years.items():
        if label == current or y.tax <= 0:
            continue
        end = fy_end(label).isoformat()
        at_end = [s for s in snaps if s["date"] <= end]
        if not at_end:
            continue
        base_inr = at_end[-1]["equity_usd"] * at_end[-1]["rate"]
        factor *= max(0.0, 1 - y.tax / base_inr) if base_inr > 0 else 1.0
        paid += y.tax

    positions = ledger.positions()
    prices = {
        s: v / positions[s].qty for s, v in last.get("holdings", {}).items() if s in positions and v > 0
    }
    this_year = book.year(current)
    exit_st, exit_lt = book.exit_gains(prices, today)
    exit_tax = max(0.0, book.what_if(this_year, exit_st, exit_lt) - this_year.tax_gains)
    value = factor * (last["inr_if_withdrawn"] - this_year.tax - exit_tax)
    return AfterTax(
        value_inr=value,
        tax_paid_inr=paid,
        tax_accrued_inr=this_year.tax,
        tax_on_exit_inr=exit_tax,
        stcg_inr=sum(y.stcg for y in book.years.values()),
        ltcg_inr=sum(y.ltcg for y in book.years.values()),
        drag_factor=factor,
    )
