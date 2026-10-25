# ARGUS — an auditable AI trading agent for Wallex

ARGUS is a spot-only, long-only trading system for Wallex TMN markets. It combines
a configurable slow trend strategy (4-hour default), a real DeepSeek decision agent, a deterministic risk
kernel, a pessimistic shadow broker, a full audit trail, a learning journal and a
read-only dashboard.

The system is built for the challenge environment: it never withdraws funds, never
uses leverage, never shorts, and can only trade an explicit allowlist of liquid TMN
markets. In shadow mode it uses live market data but never sends an order.

## Why a slow trend strategy?

Wallex TMN markets charge **0.25% maker / 0.30% taker** on the challenge account
(measured via `/v1/account/fee`). A round trip therefore costs about **0.55%**
before spread and slippage. A 1-minute mean-reversion prototype lost money after
costs in the 113-day sample (−3.94% net, 48 trades), and true 1m/2m/5m tests are also
negative. The default strategy is therefore a slower trend follower (4-hour bars by
default) that targets multi-day moves. The failed intraday
research is kept and selectable via `STRATEGY_MODE=intraday` for transparency.

## Headline backtest

Event-driven 1-minute simulation, 2026-07-01 → 2026-10-21, 113 days, eight TMN
symbols, measured fees, 8 bps slippage, next-bar execution, conservative trailing
stops, no lookahead.

| Metric | Value |
|---|---:|
| Net return | **+15.79%** |
| Max drawdown | 5.39% |
| Trades | 38 |
| Win rate | 68.4% |
| Profit factor | 3.83 |
| Sharpe / Sortino | 3.73 / 3.90 |
| Calmar | 2.93 |
| Fees paid | 91,749 TMN |
| Turnover | 6.1× |
| PnL without best trade | +694,165 TMN |

These headline rows use the historical kernel gate `CANDIDATE_THRESHOLD=0.65`. On
2026-10-24 the TREND gate was aligned with `SWING_MIN_SCORE=0.60`; the current live
shape (1h, 11 symbols) measures +20.21% / 5.40% DD / 43 trades base, +17.26% / 6.04% /
37 under 2× fees, and +20.53% / 5.43% / 42 under 2× slippage. The 2× fee drawdown sits
at the live 6% kill, so the older 0.65-gate rows above remain the conservative
headline. See `docs/RESULTS.md` §2e.

Every one of the eight symbols was profitable. A 12-symbol variant (BNB, TRX, LTC, ZEC added) returns **+16.46%** with 4.69% drawdown and 39 trades; ZEC/TMN is not tradable in the credit account, so the live `.env` now lists the 11 permitted symbols. Faster intraday variants (1h/15m trend prototypes) lose after fees; the 1h **slow swing** is a different configuration and is positive (see below). `STRATEGY_MODE=both` is an opt-in activity sleeve (25% risk), not the default. The AI-gated variant (DeepSeek reviews
the same candidates without outcome knowledge) returns **+12.52%** with **4.34% max
drawdown** and 34 trades: lower return, lower risk, and the AI vetoed the two most
extended late entries. The 2× fee stress is still positive (**+9.36%**, 5.53% DD), and
the 2× slippage stress returns **+15.23%**. See `docs/RESULTS.md` and
`docs/BACKTEST.md` for all assumptions and stress results. A lightweight snapshot is
committed in [`results/`](results/README.md) so judges can open the reports without
running the downloader.

## Strategy timeframes — comparison and how to run

ARGUS can run a **slow trend** on hourly-to-daily bars, or a separate **1-minute intraday engine**.
They are different engines, not just a number in one setting. All results below are from
2026-07-01 → 2026-10-21, measured Wallex fees (0.25% maker / 0.30% taker), 8 bps
slippage, no lookahead. Intraday results use the 25% risk sleeve.

### Which one should I use?

> **Recommended for live: 4-hour swing on the 12-symbol `.env` universe.**
> It has the best measured return (+16.46%, 4.69% max DD, 39 trades).
>
> **If you want more decision points: 1-hour swing.** It has slightly lower return but
> lower drawdown (+15.34%, 3.77% DD, 35 trades) and evaluates 24 times per day instead
> of 6.
>
> **Do not run 1m/2m/5m live.** All three are negative after fees; they are research
> and shadow-only modes.

| Strategy | Important `.env` | Net | Max DD | Trades | Verdict |
|---|---|---:|---:|---:|---|
| **4h swing** | `SWING_TIMEFRAME_HOURS=4` | **+15.79%** (8 symbols) | 5.39% | 38 | Recommended default |
| **4h swing, 12 symbols** | same + `UNIVERSE_MAX_SYMBOLS=12` | **+16.46%** | 4.69% | 39 | Historical; ZEC/TMN excluded from live |
| **1h swing** | `SWING_TIMEFRAME_HOURS=1` | +15.34% | 3.77% | 35 | Recommended if you want hourly decisions |
| 2h swing | `SWING_TIMEFRAME_HOURS=2` | +9.60% | 5.19% | 42 | Positive, but lower return |
| 1m intraday | `STRATEGY_MODE=intraday`, `INTRADAY_TIMEFRAME_MINUTES=1` | −1.39% | 1.43% | 113 | Not viable |
| 2m intraday | `INTRADAY_TIMEFRAME_MINUTES=2` | −1.55% | 1.68% | 103 | Not viable |
| 5m intraday | `INTRADAY_TIMEFRAME_MINUTES=5` | −2.77% | 2.86% | 115 | Not viable |
| daily swing | `SWING_TIMEFRAME_HOURS=24` | — | — | — | Not validated; needs ~300 days |

The 1h/2h and intraday rows are **8-symbol** tests; the 4h 12-symbol live universe is +16.46% / 4.69% / 39. Re-run with the same symbols when comparing.

The 4h AI-gated ablation is +12.52% / 4.34% DD / 34 trades: lower return, lower risk,
and a different trade set because DeepSeek vetoed the two most extended entries. The
2× fee stress is still +9.36% (5.53% DD); 2× slippage is +15.23% (5.34% DD).

### 1) 4-hour swing — recommended

```dotenv
STRATEGY_MODE=swing
SWING_TIMEFRAME_HOURS=4
SWING_FAST_EMA=48
SWING_SLOW_EMA=240
SWING_HORIZON_SEC=1728000
UNIVERSE_MAX_SYMBOLS=11
```

```bash
# Backtest this strategy (explicit comparable universe; ZEC/TMN is excluded as
# non-tradable in the credit account, so this run has 11 symbols)
python -m wallex_trader backtest \
  --symbols BTCTMN,ETHTMN,SOLTMN,USDTTMN,XRPTMN,DOGETMN,PEPETMN,XAUTTMN,BNBTMN,TRXTMN,LTCTMN \
  --start 2026-07-01 --end 2026-10-21

# Live-data rehearsal; never sends an order
make shadow

# Real money, bounded, foreground; closes positions on Ctrl-C
MODE=live LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY FLATTEN_ON_STOP=true make live
```

The 4h setting is already in `.env`. It evaluates at 00:00, 04:00, 08:00, 12:00,
16:00 and 20:00 UTC. The position/risk loop still runs every second.

### 2) 1-hour swing — recommended alternative

```dotenv
STRATEGY_MODE=swing
SWING_TIMEFRAME_HOURS=1
SWING_FAST_EMA=48
SWING_SLOW_EMA=240
```

With 48/240 bar counts, this is a **2-day / 10-day EMA** trend with hourly decisions.
It was tested at **+15.34% / 3.77% DD / 35 trades** on eight symbols. It is a good choice
if 4-hour decision points feel too slow, but it is a different strategy and should get
its own 12-symbol/AI-gated backtest before scaling live.

```bash
SWING_TIMEFRAME_HOURS=1 make shadow
SWING_TIMEFRAME_HOURS=1 MODE=live LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY FLATTEN_ON_STOP=true make live
```

### 3) 2-hour swing — optional

```dotenv
SWING_TIMEFRAME_HOURS=2
```

Tested at +9.60% / 5.19% DD / 42 trades. It is positive but dominated by 4h or 1h in
this sample; use it only if you specifically want a 4-day / 20-day trend.

```bash
SWING_TIMEFRAME_HOURS=2 make backtest
SWING_TIMEFRAME_HOURS=2 make shadow
```

### 4) 1m / 2m / 5m intraday — research and shadow only

```dotenv
STRATEGY_MODE=intraday
INTRADAY_TIMEFRAME_MINUTES=1     # 1, 2, or 5
INTRADAY_RISK_MULTIPLIER=0.25
```

The intraday engine uses 1-minute data, resamples to the configured bar, scans every
5 seconds, and evaluates completed bars. These are true N-minute backtests, with the
same fee model:

| Bar | Net | Max DD | Trades |
|---|---:|---:|---:|
| 1m | −1.39% | 1.43% | 113 |
| 2m | −1.55% | 1.68% | 103 |
| 5m | −2.77% | 2.86% | 115 |

Short live-data shadow samples (12 symbols, 2–3 minutes) produced **0 candidates and
0 fills** because the fee-aware signal had no candidate; the 113-day 1m stream averages
only ~1.2 decisions/day. That is not a bug and not a reason to force trades.

```bash
# Backtest a specific intraday bar size
STRATEGY_MODE=intraday INTRADAY_TIMEFRAME_MINUTES=2 make backtest

# Shadow only; use a tiny sleeve while experimenting
STRATEGY_MODE=intraday INTRADAY_TIMEFRAME_MINUTES=2 INTRADAY_RISK_MULTIPLIER=0.10 make shadow
```

Do **not** use this mode with real money on Wallex's 25/30 bps fee schedule.

### 5) Hybrid (slow swing + intraday sleeve) — opt-in only

```dotenv
STRATEGY_MODE=both
SWING_TIMEFRAME_HOURS=4
INTRADAY_TIMEFRAME_MINUTES=5
INTRADAY_RISK_MULTIPLIER=0.10
```

It runs both engines; the intraday sleeve trades more but has negative measured
expectancy. Use it only if activity points matter more than profit/loss control.

```bash
STRATEGY_MODE=both INTRADAY_RISK_MULTIPLIER=0.10 make shadow
```

### 6) 10-day horizon — more turnover, still positive

This is not a different timeframe; it shortens holding time:

```dotenv
STRATEGY_MODE=swing
SWING_TIMEFRAME_HOURS=4
SWING_HORIZON_SEC=864000
```

Tested at **+11.82% / 4.45% DD / 52 trades**. Lower return than the 20-day default,
but more turnover. Re-run the backtest and cost stress if you change it.

### Switching rules

- `SWING_TIMEFRAME_HOURS` accepts **1–24 hours** and only applies to the swing engine.
- `INTRADAY_TIMEFRAME_MINUTES` accepts **1–15 minutes** and only applies when
  `STRATEGY_MODE=intraday` or `both`.
- `SWING_FAST_EMA` and `SWING_SLOW_EMA` are **bar counts**, not days. At 4h, 48/240 =
  8d/40d; at 1h, 48/240 = 2d/10d. Changing both the timeframe and the EMA counts is a
  new strategy: re-run the backtest, 2× fees, 2× slippage and AI ablation.
- Daily (`24`) needs about 300 days of 1h history; the current downloaded sample is
  150 days, so daily is not validated.
- `make shadow` uses live market data and never sends an order. `make live` requires
  the prop account key, `LIVE_CONFIRM`, and ideally `FLATTEN_ON_STOP=true`.

## Logs and run artifacts

Every shadow/live run is identified by a `run_id` printed in the `engine started` log.

- Console: JSON logs to stdout/stderr.
- Aggregate log: `logs/trader.jsonl` (all runs, rotated).
- **Per-run log:** `logs/runs/<run_id>.jsonl`; every line carries `run_id` and `mode`.
- Shadow artifacts: `artifacts/shadow/<run_id>/` (metrics, decisions, positions, orders,
  fills, equity, journal).
- Live artifacts: `artifacts/live/<run_id>/` (same files, after flatten-on-stop if set).
- Database: `runs`, `decisions`, `orders`, `fills`, `positions`, `equity`, notes.

```bash
tail -f logs/runs/<run_id>.jsonl
```

## What is implemented

- Wallex REST client with retries, rate limits, precision-aware orders, idempotent
  client IDs, order lookup after ambiguous POSTs and a read-only MCP client.
- Centrifugo WebSocket feed with automatic reconnect and REST resync.
- Local order books, trade tape, 1m/1h/4h candles and feature engine.
- Configurable slow trend (4h default, 1h–24h) plus a legacy intraday mean-reversion/momentum engine (1m/2m/5m).
- DeepSeek agent with strict JSON schema validation, prompt/response hashes, daily
  budget, circuit breaker, caching and optional MCP escalation.
- Trade reflection and session review: lessons are stored in PostgreSQL and an
  append-only Markdown journal, then injected into future prompts.
- Risk kernel: position sizing, caps, stops, portfolio heat, correlated exposure,
  cash buffer, daily-loss and drawdown de-risking, kill switch, self-trade prevention.
- Shadow broker with pessimistic fills; live broker that can only place spot limits.
- OMS with persist-before-send, timeout/cancel/reprice, partial fills and reconciliation.
- PostgreSQL storage (SQLite for local development) with decisions, orders, fills,
  features, LLM calls, agent notes, equity and risk events.
- FastAPI dashboard at `/test-results` with basic auth, charts, decisions, positions,
  agent memory, backtests, shadow runs, system health and audit export. The LIVE badge
  has a blinking green dot while the live process is running, and the overview shows the
  last scan, candidate count, regime and next 4H decision time.
- Backtest downloader, simulator, HTML/JSON/CSV artifacts, parameter study and AI ablation.
- Docker Compose (PostgreSQL, trader, dashboard, optional Caddy), Makefile and scripts.
- Unit tests, mypy and ruff. Live preflight and shadow smoke tests were run against
  the real Wallex and DeepSeek APIs.

## Quick start (local)

```bash
cp .env.example .env      # fill WALLEX_API_KEY and DEEPSEEK_API_KEY
make install              # uv venv + editable install
make preflight            # REST, WS, MCP, DeepSeek, balances, fees, clock skew
make self-test            # deterministic shadow OMS/risk test (no real orders)
make agent-test           # DeepSeek decision on a synthetic candidate (no orders)
make download             # 1m history (default 2026-05-01 → 2026-10-21, includes warm-up)
make backtest             # event-driven backtest + HTML report
make shadow               # live-data shadow mode (never sends orders)
# Optional activity sleeve:
# STRATEGY_MODE=both INTRADAY_RISK_MULTIPLIER=0.25 make live   # more trades, higher cost drag
make dashboard            # http://127.0.0.1:8080/test-results
```

The dashboard uses HTTP basic auth from `.env`. To find the generated local
password:

```bash
grep DASHBOARD_BASIC_AUTH_PASSWORD .env
```

The kill-switch API additionally requires `DASHBOARD_KILL_TOKEN` in the
`x-kill-token` header and a confirmation body (`{"confirm":"KILL"}` in shadow,
`{"confirm":"KILL_LIVE"}` in live).

### Docker

```bash
make up        # PostgreSQL + trader + dashboard
make logs
make down
make proxy     # optional Caddy profile (ports 80/443)
```

The dashboard listens on `${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-8080}` by
default; set `DASHBOARD_BIND=0.0.0.0` explicitly to expose it beyond localhost. The
trader has no ports. Caddy is optional; basic auth is enforced by the application
itself, and is disabled when `DASHBOARD_BASIC_AUTH_PASSWORD` is empty, so never
expose the dashboard without a password. Use TLS (Caddy) beyond a private network.

### Challenge window

Set the official UTC timestamps in `.env`:

```dotenv
CHALLENGE_START=2026-10-24T00:00:00+00:00
CHALLENGE_END=2026-10-27T00:00:00+00:00
```

Then the engine blocks new entries 2 hours before the end and flattens 30 minutes
before the end. Leave both empty to disable endgame timers.

### Live mode

Live mode is deliberately hard to start:

1. `MODE=live` and `LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY` (shell env or `.env`)
2. `python -m wallex_trader live --confirm-live`
3. Preflight must pass; the TMN balance must cover the minimum notional; no open
   orders may exist; the canary config caps orders at 250,000 TMN.

The prop sub-account is funded with **5,000,000 TMN** and its API key is now in
`.env`. Live preflight passes (`api_key_matches_sub_account = true`, 0 open orders).
The first attempt used the main key and was correctly rejected; a later live run hit
base-asset fee/quantity accounting. The positions were sold at 07:32 and the flatten
command then found only unsellable dust. The bugs are fixed; see `report.md` §7.5 and
`results/live_incident_20261023.md`. A bounded live test can be started in the foreground with:

```bash
MODE=live LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY FLATTEN_ON_STOP=true make live
```

`FLATTEN_ON_STOP=true` closes positions when you press Ctrl-C and writes
`artifacts/live/<run_id>/` (metrics, decisions, positions, orders, fills, equity,
journal). Shadow remains the default until the operator starts live.

### Deployed VPS (live)

The system also runs on a VPS at **`78.141.219.4`**. Access is key-based; the server
password is not stored in this repository.

```bash
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4
```

Layout and processes on the VPS:

| Item | Value |
|---|---|
| Project | `/opt/argus` |
| Remote `.env` | `/opt/argus/.env` (root-only, mode 600; authoritative) |
| Database | Docker service `db` (Postgres, named volume `argus_pgdata`) |
| Dashboard | Docker service `dashboard`, public `0.0.0.0:8080`, HTTP basic auth |
| Live trader | systemd `argus-live.service`, container `argus-live-trader` |
| Live command | `docker compose run ... live --confirm-live` with `FLATTEN_ON_STOP=true` |
| Restart policy | systemd `Restart=on-failure`, max 5 starts / 5 min; enabled at boot |

Dashboard: <http://78.141.219.4:8080/test-results> — credentials are
`DASHBOARD_BASIC_AUTH_USER` / `DASHBOARD_BASIC_AUTH_PASSWORD` in the remote `.env`.
It is plain HTTP with basic auth; use an SSH tunnel or Caddy/TLS if you need privacy:

```bash
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 -L 8080:127.0.0.1:8080 root@78.141.219.4
# then open http://127.0.0.1:8080/test-results
```

Operations:

```bash
# from your workstation
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 'systemctl status argus-live'
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 'journalctl -u argus-live -f'
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 'cd /opt/argus && docker compose ps'

# stop / restart also sell open positions (FLATTEN_ON_STOP=true)
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 'systemctl stop argus-live'
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 'systemctl start argus-live'
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 'systemctl restart argus-live'

# engine health (live heartbeats only)
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 \
  'docker exec argus-live-trader python -m wallex_trader health --max-age 120 --mode live'
# a container started before 2026-10-24 lacks --mode; omit it until the next restart
```

Deploy a code change (never copy `.env` with `--delete`). **Flatten first**: the
restart below closes every open position because the unit sets
`FLATTEN_ON_STOP=true`. Verify the `positions` table has no open quantity, or accept a
forced market exit and its fees.

```bash
rsync -az --delete -e "ssh -i ~/.ssh/ahmad_mobaraki_ed25519" \
  --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude 'artifacts/' \
  --exclude 'logs/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.mypy_cache/' --exclude '.ruff_cache/' --exclude '.env' \
  ./ root@78.141.219.4:/opt/argus/
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 \
  'cd /opt/argus && docker compose build trader && systemctl restart argus-live'
```

The remote `.env` must set `DASHBOARD_BIND=0.0.0.0` for the public dashboard; the
Compose default is `127.0.0.1`. Never run `make up` on the VPS while `argus-live` is
active: it starts the Compose `trader` service as a second (shadow) engine against the
same DB, API key and WebSocket.

Only one live engine may run at a time: the fixed container name `argus-live-trader` is
the primary guard, and `live.lock` is an OS advisory `flock` on the shared data volume
that also excludes a differently named live container. Do not start a second live
process or a tmux copy while `argus-live` is active.

## Commands

| Command | Purpose |
|---|---|
| `python -m wallex_trader preflight` | REST/WS/MCP/DeepSeek/account checks, no orders |
| `python -m wallex_trader run` | run `MODE` from `.env` |
| `python -m wallex_trader shadow` | force shadow mode |
| `python -m wallex_trader live --confirm-live` | guarded live mode |
| `python -m wallex_trader download --start ... --end ...` | download UDF history |
| `python -m wallex_trader backtest` | run the backtest profile |
| `python -m wallex_trader study` | parameter sensitivity grid |
| `python -m wallex_trader ablation` | deterministic vs AI-gated comparison |
| `python -m wallex_trader ai-review --decisions ... --out ...` | offline AI review of backtest candidates |
| `python -m wallex_trader replay --run-id ...` | verify a stored run |
| `python -m wallex_trader self-test --ai --reflection` | OMS/risk + AI + reflection smoke test |
| `python -m wallex_trader dashboard` | read-only dashboard |
| `python -m wallex_trader health` | heartbeat check for container health |
| `python -m wallex_trader kill --reason ...` | file kill switch |
| `python -m wallex_trader flatten` | flatten positions (shadow by default) |

## Repository map

```
src/wallex_trader/
  exchange/      REST, WebSocket, MCP, auth, rate limits
  marketdata/    order books, candles, trade tape, features, feed
  strategy/      universe, signals, swing, regime, portfolio, exits
  agent/         DeepSeek client, schemas, prompts, memory, reflection, ablation
  risk/          limits, risk kernel, kill switch
  execution/     precision, state machine, shadow broker, live broker, OMS, reconciler
  storage/       SQLAlchemy models, database, repositories
  backtest/      downloader, precompute, fills, simulator, metrics, report, study, AI review
  dashboard/     FastAPI app, templates, static assets
  engine.py      orchestration loops
  cli.py         command line interface
configs/         base + shadow/live/backtest YAML
docs/            architecture, strategy, AI, risk, backtest, shadow, operations, parameters, compliance, decisions, results
scripts/         preflight, download, shadow, live, flatten, smoke test
artifacts/       backtests, shadow runs, preflight, ablations, selftest, journal
data/            raw/curated candles, manifests, run cache, local SQLite DB
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Strategy](docs/STRATEGY.md)
- [AI agent and learning loop](docs/AI_AGENT.md)
- [Risk management](docs/RISK.md)
- [Backtest methodology and results](docs/BACKTEST.md)
- [Shadow testing](docs/SHADOW_TESTING.md)
- [Operations](docs/OPERATIONS.md)
- [Parameters and robustness](docs/PARAMETERS.md)
- [Compliance](docs/COMPLIANCE.md)
- [Decision log and audit](docs/DECISION_LOG.md)
- [Results summary](docs/RESULTS.md)

## Security notes

- Never commit `.env`. It is in `.gitignore`.
- Do not `source .env` in a shell: the Wallex key may contain `|` or other shell
  characters. The application and Docker Compose load it with a proper dotenv parser.
- The dashboard has no order-entry controls. The only mutation endpoint is the
  kill switch, protected by a separate token.
- The VPS dashboard is exposed over plain HTTP with basic auth. Use the SSH tunnel
  above or terminate TLS in Caddy for anything sensitive. The VPS root password is
  never stored in this repository; use the SSH key.
- The AI cannot change hard risk limits, enable leverage/shorting, or withdraw funds.
  Lessons are advisory; code and limits are deterministic.

## Disclaimer

This is a hackathon research system. Backtest results are not a promise of future
returns. Wallex fees and crypto volatility can erase an edge quickly. Use shadow
mode first, fund only what you can afford to lose, and keep the kill switch available.
