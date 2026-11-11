# us-paper-trader

A US-market paper-trading dry run that behaves like an **Indian resident's LRS cash account**, not
like a US margin account. Runs as three short scheduled Fargate tasks a day for about **$1/month**.
Fully separate from the NSE project: own repo, own code, own AWS tenant.

```
EventBridge Scheduler (America/New_York, Mon–Fri)
  08:45 ET premarket ─┐
  10:05 ET trade     ─┼─► ECS Fargate task (arm64, 0.5 vCPU / 1 GB, runs minutes, then exits)
  16:30 ET postclose ─┘        │  reads secrets from SSM, writes ledger to DynamoDB
                               ├─► Alpaca paper API (one $1,000 paper account per mode)
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
  compares every mode against the buy-and-hold baseline.

## Modes

| Mode | Rule |
|---|---|
| `baseline` | Buy and hold VOO. Everything else has to beat this after costs. |
| `conservative` | VOO 60% / SGOV 40% above VOO's 200-day average, 100% SGOV below it (±2% hysteresis band). |
| `balanced` | Monthly: top 3 of 12 broad/sector ETFs by 6-month return that are also above their 200-day average; empty slots go to SGOV. |
| `aggressive` | A notch above balanced. Twice a month (1st/16th): top 3 of 20 ETFs **and mega-cap stocks** (NVDA, MSFT, AAPL…) on a 3+6-month momentum blend, above their 100-day average. Market brake: max 1 pick while VOO is below its 200-day average. Protective exit: a pick closing below its 50-day average goes to SGOV until the next rebalance. Still unleveraged, so LRS-compliant. |

SGOV (T-bill ETF) is used as "cash" so idle money still earns the T-bill yield, as it would for real.
Expect `aggressive` to trade far more (and so pay more in fees and settlement delays) and to swing
harder; the report's max-drawdown column is the honest place to judge it.

## Isolation from the NSE stack (same AWS account)

| Layer | How |
|---|---|
| Terraform | Own root module and state key (`us-paper-trader/prod/terraform.tfstate`) |
| Network | Own VPC `10.42.0.0/16`, public subnets, no NAT, no inbound, egress 443/587 only |
| IAM | Every role has a permissions boundary that only allows `us-paper-trader*` resources |
| Data / secrets | Own DynamoDB table, own SSM path `/us-paper-trader/*`, own log group |
| Compute | Own ECS cluster and task definition, own scheduler group |
| Cost | Every resource tagged `project=us-paper-trader`, own budget alarm |
| LLM quotas | Use a separate Gemini/GCP project when the AI mode is added |

Don't enable AWS Organizations or IAM Identity Center for this: on the Free plan it ends the credits.

## Cost

| Item | ~USD/month |
|---|---|
| Fargate arm64, ~1 task-hour/day × 22 days | 0.45–0.60 |
| Public IPv4 while tasks run | 0.10 |
| ECR image storage | 0.05 |
| DynamoDB (provisioned 2/2, always-free), SSM standard, logs, scheduler | 0 |
| Budget alarm (free if it's one of the account's first two budgets) | 0–0.60 |
| Alpaca paper, Frankfurter FX, GitHub Actions | 0 |

**Account deadlines** (account opened ~7 Oct 2026): the AWS Free plan closes the account at 6 months
(**~7 Apr 2027**) unless upgraded to the Paid plan. Upgrading keeps unused credits, which last until
~7 Oct 2027. Upgrade in March 2027.

## Setup (once, from your laptop)

1. **Alpaca**: sign up (paper-only works from India), then in the dashboard create one paper account
   per mode (four: baseline, conservative, balanced, aggressive) with a **$1,000** balance each and
   generate API keys for each.
2. **Email**: a Gmail app password for the sending account (Google Account → Security → App passwords).
3. **Terraform**
   ```bash
   cd infra/envs/prod
   cp backend.hcl.example backend.hcl            # state bucket (the NSE one is fine)
   cp terraform.tfvars.example terraform.tfvars  # alert email, OIDC provider ARN
   terraform init -backend-config=backend.hcl
   terraform apply                               # schedules stay DISABLED
   ```
4. **Image**: in GitHub → Settings → Variables, set `AWS_DEPLOY_ROLE_ARN`
   (`terraform output -json tenant | jq -r .github_deploy_role_arn`) and `AWS_REGION=ap-southeast-2`,
   then run the *deploy* workflow.
5. **Secrets**: `./scripts/put-secrets.sh`
6. **Smoke test** on a US trading day: `./scripts/run-phase.sh premarket`, then `trade` after 09:30 ET,
   then `postclose` (you should get the email).
7. **Go**: set `schedules_enabled = true` in `terraform.tfvars` and `terraform apply`.

After about a day, set `activate_cost_tag = true` and apply again so the budget can filter on the tag.

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
  costs.py        Indian-investor cost model
  ledger.py       append-only ledger (DynamoDB or local JSONL), positions, T+1 settled cash
  rebalance.py    targets -> orders, sells first, drift band
  strategies/     baseline, conservative, balanced
  broker/         Alpaca paper adapter (live keys refused), fake broker for tests
  report.py       INR-aware morning email
infra/modules/tenant   the isolated AWS tenant
infra/envs/prod        root module
```

Paper trading only. Not investment advice. Check LRS/FEMA, tax and Schedule FA treatment with a CA
before putting real money in.
