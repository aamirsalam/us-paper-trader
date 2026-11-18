# us-paper-trader

A US-market paper-trading dry run that behaves like an **Indian resident's LRS cash account**, not
like a US margin account. Runs as three short scheduled Fargate tasks a day for about **$1/month**.
Fully separate from the NSE project: own repo, own code, own AWS tenant.

```
EventBridge Scheduler (America/New_York, Mon–Fri)
  08:45 ET premarket ─┐
  10:05 ET trade     ─┼─► ECS Fargate task (arm64, 0.5 vCPU / 1 GB, runs minutes, then exits)
  16:30 ET postclose ─┘        │  reads secrets from SSM, writes ledger to DynamoDB
                               ├─► Alpaca paper API ($1,000 paper account per mode; baseline is ledger-only)
                               ├─► ECB USD/INR rate (Frankfurter)
                               └─► report email (SMTP)
```

| Phase | ET | IST (Nov–Mar) | IST (Mar–Nov) |
|---|---|---|---|
| premarket | 08:45 | 19:15 | 18:15 |
| trade | 10:05 | 20:35 | 19:35 |
| postclose + email | 16:30 | 03:00 | 02:00 |

## What makes it "Indian-resident realistic"

- **LRS guard** (`compliance.py`) blocks options, crypto, short sales, margin and leveraged/inverse ETFs,
  and only funds buys from **settled** cash (T+1), as a cash account must. The Alpaca account itself is
  also locked to no-shorting / 1x / no options on first run.
- **Cost model** (`costs.py`): FX markup each way, per-remittance fee, **25% US dividend withholding**,
  sell-side SEC/FINRA fees, and TCS tracking against the ₹10 lakh/FY threshold. Alpaca paper doesn't
  simulate dividends at all; the ledger credits them from Alpaca's corporate-actions feed.
- **Report in INR** splits each mode's result into *strategy*, *rupee move* and *bank FX friction*, and
  compares every mode against the buy-and-hold baseline **after Indian tax** (`tax.py`, below).

## Modes

| Mode | Rule |
|---|---|
| `baseline` | Buy and hold VOO. Everything else has to beat this after costs. |
| `conservative` | VOO 60% / SGOV 40% above VOO's 200-day average, 100% SGOV below it (±2% hysteresis band). |
| `balanced` | Monthly: top 3 of 12 broad/sector ETFs by 6-month return that are also above their 200-day average; empty slots go to SGOV. |
| `aggressive-ai` | `aggressive`'s rules plus an **AI veto**: before a new single-stock buy, Gemini reads the last 14 days of headlines (Alpaca's free news feed), the trend and the earnings date, and may block it; the slot then sits in SGOV until the next rebalance. It can only remove buys, never add or sell. ~2–6 calls a month. Ledger-only (no Alpaca account). Runs beside `aggressive`, so the gap between them is the AI's contribution. Not backtestable: the AI has already read the news of past years. |
| `aggressive` | A notch above balanced (see the backtest caveat below). Twice a month (1st/16th): top 3 of 20 ETFs **and mega-cap stocks** (NVDA, MSFT, AAPL…) on a 3+6-month momentum blend, above their 100-day average. Market brake: max 1 pick while VOO is below its 200-day average. Protective exit: a pick closing below its 50-day average goes to SGOV until the next rebalance. Still unleveraged, so LRS-compliant. |

SGOV (T-bill ETF) is used as "cash" so idle money still earns the T-bill yield, as it would for real.
Expect `aggressive` to trade far more (and so pay more in fees and settlement delays) and to swing
harder; the report's max-drawdown column is the honest place to judge it.

`baseline` and `aggressive-ai` are **ledger-only**: Alpaca allows three paper accounts per login, and
neither needs its own (baseline is a single buy of VOO; aggressive-ai mirrors aggressive's orders). It takes prices, calendar and dividends from another mode's Alpaca connection and
fills on our own books at the market price plus 2 bps. Change with `virtual_modes` in `terraform.tfvars`.

## Indian tax (what you'd actually keep)

Every report and backtest has an **after-tax** column: the rupees left if you sold everything and
brought it home today.

| Item | Treatment |
|---|---|
| Lots | First-in, first-out, each with its own purchase date |
| Rupee conversion | Cost and sale each converted at the rate on the last day of the month before that transaction (Rule 115; mid-market rate stands in for SBI's TT buying rate). A weaker rupee is taxed as gain. |
| Held over 24 months | Long-term: 12.5% + 4% cess = 13% |
| Held 24 months or less | Short-term: your slab rate + cess (default 30% → 31.2%) |
| Losses | Short-term losses offset any gain, long-term only long-term; unused losses carry forward |
| Dividends | Slab rate on the gross, less credit for the 25% US withholding (Form 67), capped at the Indian tax |
| Timing | Each finished financial year's tax comes out of the portfolio at year end, so later growth on it is lost |

Set your own marginal rate with `TAX_SLAB_RATE` in `app_env` (also `TAX_SURCHARGE`, `TAX_CESS`,
`TAX_LTCG_RATE`). Not modelled: basic exemption and rebates, the 8-year expiry of carried losses, and
the chance that frequent trading is assessed as business income. These are estimates to compare
modes, not a tax computation: confirm with a CA before filing.

## Earnings blackout (aggressive mode)

Single stocks gap overnight on earnings; ETFs don't report. So for the ten stocks in `aggressive`:

- **No buys** (new picks or top-ups) if the company reports within the next **5 trading days**.
- **Exit first** if a held stock reports **today or tomorrow** (confirmed date); it's eligible again
  at the next rebalance.
- **Fail safe**: if no earnings source answers, no new single-stock buys that day; holdings untouched.

Dates come from Nasdaq's public earnings calendar (keyless). Nasdaq often refuses cloud servers, so set
`sec_user_agent` in `terraform.tfvars` from day one: if Nasdaq doesn't answer, the next date is projected from the company's SEC 8-K Item 2.02 history (a fuzzier ±1 week window
that blocks buys but never forces an exit). The email shows which source was used each day. Backtests
use the actual SEC filing dates.

## Loss limits

Checked at every trade phase with live prices. Sells and parking cash in SGOV are never blocked.

| Mode | Weekly loss → pause new buys for the week | Drawdown from peak → halt to T-bills | Single stock cap |
|---|---|---|---|
| baseline | — | — | — |
| conservative | 3% | 12% | — |
| balanced | 4% | 15% | — |
| aggressive | 6% | 20% | 40% of equity |

A halt lifts itself after 20 trading days with the peak reset (`RISK_<MODE>_HALT_DAYS=0` makes it
manual-only). Lift it early with `./scripts/run-phase.sh resume --mode aggressive`. The email subject
starts with ⛔ while any mode is halted. Override any limit with `RISK_<MODE>_WEEKLY`, `_DRAWDOWN`,
`_SINGLE` (fraction or `off`) in `app_env`.

## Backtest

```bash
usptrader backtest --start 2016-01-04 --out ./data/backtest      # Yahoo daily bars, cached in ./data/cache
usptrader backtest --no-limits                                   # compare without loss limits
SEC_USER_AGENT="Your Name you@example.com" usptrader backtest    # include the earnings blackout
```

It replays the **real** trader day by day (same strategies, LRS guard, loss limits, ledger and cost
model): decisions on yesterday's close, fills at today's open plus 5 bps, T+1 settlement, dividends
paid 14 days after ex-date net of 25% withholding, FX spread in and out at the historical USD/INR rate.
SGOV history before its 2020 launch is spliced from BIL.

**Results, 2016-01-04 → 2026-11-13** ($1,000 per mode, limits on, earnings blackout off, 30% slab):

| Mode | CAGR USD | CAGR INR | **CAGR INR after tax** | Max drawdown | Return/vol |
|---|---|---|---|---|---|
| baseline (VOO) | 14.3% | 17.8% | **16.5%** | -34.0% | 0.81 |
| conservative | 6.8% | 10.1% | **7.6%** | -13.8% | 0.90 |
| balanced | 9.9% | 13.3% | **9.5%** | -19.2% | 0.66 |
| aggressive | 24.8% | 28.7% | **20.5%** | -27.6% | 1.08 |
| *aggressive, ETFs only* | *7.9%* | *11.3%* | ***8.1%*** | *-15.0%* | *0.58* |

After-tax CAGR in INR at other slab rates:

| Mode | 5% slab | 20% slab | 30% slab |
|---|---|---|---|
| baseline | 16.6% | 16.6% | 16.5% |
| conservative | 9.5% | 8.4% | 7.6% |
| balanced | 12.7% | 10.9% | 9.5% |
| aggressive | 27.4% | 23.3% | 20.5% |
| *aggressive, ETFs only* | *10.8%* | *9.2%* | *8.1%* |

How to read it:
- **Tax widens buy-and-hold's lead.** Baseline loses 1.3 points a year to tax at any slab, because it
  only ever pays the 13% long-term rate once, at the end. The trading modes realise nearly all their
  gains short-term and lose 2.5 to 8 points a year at a 30% slab.
- **Buy-and-hold won.** Conservative and balanced trail VOO by 7 to 9 points a year after tax; what they
  buy is a smaller worst-case drop.
- **Aggressive's number is mostly hindsight.** Its stock list is today's winners. With only its ETFs
  the same rules make 8.1% after tax, half of baseline. Treat 20.5% as an upper bound.
- **Your slab matters a lot for the active modes** and hardly at all for baseline.
- **Loss limits are insurance, not alpha.** Turning them off adds 0.2–0.8% a year before tax; they cut
  balanced's worst drawdown from -24% to -19% and barely change the others.
- About 3.5 points of the INR return is the rupee weakening (66 → 88), not the strategy, and that part
  is taxed too.

## Isolation from the NSE stack (same AWS account)

| Layer | How |
|---|---|
| Terraform | Own root module and own state file (local by default; `backend_s3.tf.example` moves it to S3) |
| Network | Own VPC `10.42.0.0/16` (or the default VPC if guardrails require), no NAT; own security group: no inbound, egress 443/587 only |
| IAM | Role policies scoped to `us-paper-trader*` resources, plus a permissions boundary where the account allows managed policies |
| Data / secrets | Own DynamoDB table, own SSM path `/us-paper-trader/*`, own log group |
| Compute | Own ECS cluster and task definition, own scheduler group |
| Cost | Every resource tagged `project=us-paper-trader`, own budget alarm |
| Alerts | Own SNS topic: an email when any run exits with an error or fails to start |
| LLM quotas | Use a separate Gemini/GCP project when the AI mode is added |

Don't enable AWS Organizations or IAM Identity Center for this: on the Free plan it ends the credits.

## Cost

| Item | ~USD/month |
|---|---|
| Fargate arm64, ~1 task-hour/day × 22 days | 0.45–0.60 |
| Public IPv4 while tasks run | 0.10 |
| ECR image storage | 0.05 |
| DynamoDB (provisioned 5 read / 2 write, always-free), SSM standard, logs, scheduler, SNS alerts | 0 |
| Budget alarm (free if it's one of the account's first two budgets) | 0–0.60 |
| Alpaca paper, Frankfurter FX, GitHub Actions | 0 |

**Account deadlines** (account opened ~7 Oct 2026): the AWS Free plan closes the account at 6 months
(**~7 Apr 2027**) unless upgraded to the Paid plan. Upgrading keeps unused credits, which last until
~7 Oct 2027. Upgrade in March 2027.

## Setup (once, from your laptop)

Commands are PowerShell on Windows; each script also has a bash twin (`.sh`) where noted.

1. **Alpaca**: sign up (paper-only works from India), then in the dashboard create **three** paper
   accounts (conservative, balanced, aggressive: Alpaca's limit per login) with a **$1,000** balance
   each and generate API keys for each. Baseline needs none.
2. **Email**: a Gmail app password for the sending account (Google Account → Security → App passwords).
3. **Sign in and check the account's guardrails**
   ```powershell
   aws login --region ap-southeast-2
   . .\scripts\aws-session.ps1                    # signs in if needed, hands the session to Terraform
   .\scripts\preflight.ps1
   ```
   Free-plan accounts sit under AWS guardrails that block some actions (GitHub OIDC, sometimes VPCs or
   managed IAM policies). The pre-flight dry-runs each one and prints the `terraform.tfvars` switches
   to set. Nothing it tests is left behind.
4. **Terraform** (state is a local file in `infra\envs\prod`, git-ignored, like the NSE stack's: back it up)
   ```powershell
   cd infra\envs\prod
   copy terraform.tfvars.example terraform.tfvars  # alert_email, sec_user_agent, switches from step 3
   terraform init
   terraform plan                                  # everything should be named us-paper-trader*
   terraform apply                                 # schedules stay DISABLED
   cd ..\..\..
   ```
   AWS then emails a subscription confirmation to the alert address: click it, or failure alerts
   won't arrive.
5. **Image**: with Docker Desktop running, `.\scripts\push-image.ps1` (builds linux/arm64, pushes `:live`).
   If your account allows GitHub OIDC, the *deploy* workflow can do this on every push instead: set
   repo variables `AWS_DEPLOY_ROLE_ARN` (from `terraform output -json tenant`) and `AWS_REGION`.
6. **Secrets**: `.\scripts\put-secrets.ps1` (or `put-secrets.sh`). Hidden prompts for each mode's
   Alpaca key ID and secret, `ai/gemini_api_key` (a Gemini key from its own Google project, so it
   doesn't share another project's free quota), `smtp/user`, `smtp/password` (the app password) and
   `report/to`. Without a Gemini key, aggressive-ai simply follows the rules and the report says so.
7. **Smoke test** on a US trading day: `.\scripts\run-phase.ps1 premarket`, then `trade` while the
   market is open (8:00 PM–2:30 AM IST until March), then `postclose` (you should get the email).
8. **Go**: set `schedules_enabled = true` in `terraform.tfvars` and `terraform apply`.

After about a day, set `activate_cost_tag = true` and apply again so the budget can filter on the tag.
If Terraform says the token has expired, run `. .\scripts\aws-session.ps1` again: it clears the stale keys, signs in if needed and exports fresh ones.

## Local use

```bash
pip install -e ".[dev]"
pytest -q
usptrader simulate --days 60          # offline, fake market, local ledger in ./data
```

Running a real phase locally needs `ALPACA_KEY_ID_<MODE>` / `ALPACA_SECRET_<MODE>` in the environment;
the ledger defaults to `./data/ledger/*.jsonl`.

## Layout

```
src/usptrader/
  runner.py       phases; idempotent, one failing mode doesn't stop the others
  compliance.py   LRS guard
  risk.py         weekly pause, drawdown halt, single-stock cap
  tax.py          Indian after-tax ledger: FIFO lots, short/long-term, set-off, dividend credit
  earnings.py     Nasdaq calendar, SEC 8-K 2.02 history, fail-safe chain
  backtest.py     day-by-day replay of the real trader; marketdata.py fetches and caches Yahoo
  costs.py        Indian-investor cost model
  ledger.py       append-only ledger (DynamoDB or local JSONL), positions, T+1 settled cash
  rebalance.py    targets -> orders, sells first, drift band
  strategies/     baseline, conservative, balanced, aggressive
  broker/         Alpaca paper adapter (live keys refused), virtual (ledger-only), historical, fake
  report.py       INR-aware morning email
infra/modules/tenant   the isolated AWS tenant
infra/envs/prod        root module
```

Paper trading only. Not investment advice. Check LRS/FEMA, tax and Schedule FA treatment with a CA
before putting real money in.
