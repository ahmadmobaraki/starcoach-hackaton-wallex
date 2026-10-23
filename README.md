# ARGUS — an auditable AI trading agent for Wallex

ARGUS is a spot-only, long-only trading system for Wallex TMN markets. It combines
a slow 4-hour trend strategy, a real DeepSeek decision agent, a deterministic risk
kernel, a pessimistic shadow broker, a full audit trail, a learning journal and a
read-only dashboard.

The system is built for the challenge environment: it never withdraws funds, never
uses leverage, never shorts, and can only trade an explicit allowlist of liquid TMN
markets. In shadow mode it uses live market data but never sends an order.

## Why a slow trend strategy?

Wallex TMN markets charge **0.25% maker / 0.30% taker** on the challenge account
(measured via `/v1/account/fee`). A round trip therefore costs about **0.55%**
before spread and slippage. A 1-minute mean-reversion prototype lost money after
costs in the 113-day sample (−3.94% net, 48 trades), so the default strategy is a
slower 4-hour trend follower that targets multi-day moves. The failed intraday
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

Every one of the eight symbols was profitable. A 12-symbol variant (BNB, TRX, LTC, ZEC added) returns **+16.46%** with 4.69% drawdown and 39 trades; the live `.env` lists 12 symbols for more opportunity surface. Faster 1h/15m variants were tested and lose after fees, so `STRATEGY_MODE=both` is an opt-in activity sleeve (25% risk), not the default. The AI-gated variant (DeepSeek reviews
the same candidates without outcome knowledge) returns **+12.52%** with **4.34% max
drawdown** and 34 trades: lower return, lower risk, and the AI vetoed the two most
extended late entries. The 2× fee stress is still positive (**+9.36%**, 5.53% DD), and
the 2× slippage stress returns **+15.23%**. See `docs/RESULTS.md` and
`docs/BACKTEST.md` for all assumptions and stress results. A lightweight snapshot is
committed in [`results/`](results/README.md) so judges can open the reports without
running the downloader.

## Signal timeframe

The trader's **signal timeframe is configurable from `.env`**:

```dotenv
SWING_TIMEFRAME_HOURS=4      # default: 8d/40d EMA on 4h bars
SWING_FAST_EMA=48
SWING_SLOW_EMA=240
SWING_HORIZON_SEC=1728000    # 20-day time stop
```

- `SWING_TIMEFRAME_HOURS=4` builds completed 4h bars from 1h candles (default).
- `SWING_TIMEFRAME_HOURS=1` builds hourly bars; with 48/240 EMAs this is a 2d/10d
  trend with hourly decision points. Backtest: +15.34% net, 3.77% DD, 35 trades.
- `SWING_TIMEFRAME_HOURS=2` gives 4d/20d EMAs; +9.60%, 5.19% DD, 42 trades.
- `SWING_TIMEFRAME_HOURS=24` builds daily bars; 48/240 then means 48d/240d and needs
  roughly 300 days of history, so the current 150-day sample cannot validate it.

The 1-minute data feed and 1-second position management do not change with this setting.
New entries are evaluated at each completed signal-timeframe bar; exits are checked every
second. The legacy `STRATEGY_MODE=both/intraday` sleeve still uses 1-minute features.

## What is implemented

- Wallex REST client with retries, rate limits, precision-aware orders, idempotent
  client IDs, order lookup after ambiguous POSTs and a read-only MCP client.
- Centrifugo WebSocket feed with automatic reconnect and REST resync.
- Local order books, trade tape, 1m/1h/4h candles and feature engine.
- Slow 4h trend strategy plus a legacy intraday mean-reversion/momentum engine.
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
make up        # PostgreSQL + trader + dashboard on 127.0.0.1:8080
make logs
make down
make proxy     # optional Caddy profile (ports 80/443)
```

The dashboard is the only exposed service (localhost by default). The trader has
no ports. Caddy is optional; basic auth is enforced by the application itself.

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
The first attempt used the main key and was correctly rejected; see the incident in
`report.md` §7.4. A bounded live test can be started in the foreground with:

```bash
MODE=live LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY FLATTEN_ON_STOP=true make live
```

`FLATTEN_ON_STOP=true` closes positions when you press Ctrl-C and writes
`artifacts/live/<run_id>/` (metrics, decisions, positions, orders, fills, equity,
journal). Shadow remains the default until the operator starts live.

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
- The AI cannot change hard risk limits, enable leverage/shorting, or withdraw funds.
  Lessons are advisory; code and limits are deterministic.

## Disclaimer

This is a hackathon research system. Backtest results are not a promise of future
returns. Wallex fees and crypto volatility can erase an edge quickly. Use shadow
mode first, fund only what you can afford to lose, and keep the kill switch available.
