# Operations

## Environment

Copy `.env.example` to `.env` and fill:

- `WALLEX_API_KEY` — required for private data/live; public shadow can run without it.
- `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_DEEP_MODEL`.
- `DASHBOARD_BASIC_AUTH_PASSWORD`, `DASHBOARD_KILL_TOKEN`.
- `POSTGRES_*` for Docker; local defaults to SQLite.

`MODE=shadow` is the safe default. `LIVE_CONFIRM` must be empty unless `MODE=live`.
The Wallex key may contain `|`; never `source .env`. The application and Compose use
a dotenv parser.

## Preflight

```bash
make preflight
# or
python -m wallex_trader preflight
```

Checks: markets, depth, trades, 1m candles, private balances, account fees, open
orders, clock skew, WebSocket, DeepSeek, MCP and live balance sufficiency. It writes
a timestamped JSON under `artifacts/preflight/`. It never places an order.

The main account has 0 TMN, but the hackathon prop sub-account is funded with
5,000,000 TMN; the prop account's API key is set in `.env`. Live preflight passes
`api_key_matches_sub_account` with 0 open orders. The first main-key rejection is
documented in `report.md` §7.4.
Shadow preflight passes.

## Starting

### Local

```bash
make shadow                # foreground; Ctrl-C stops cleanly
make dashboard             # separate process
```

### Docker

```bash
make up                    # db + trader + dashboard
make logs
make down
```

The dashboard is bound to `127.0.0.1:8080`; the trader has no exposed port. The
optional Caddy profile (`make proxy`) exposes 80/443. Application basic auth is
always enforced when a password is configured.

## Dashboard

- URL: `http://127.0.0.1:8080/test-results` (or `/` redirects).
- Username/password from `.env`.
- Tabs: Overview, Decisions, Positions, Agent memory, Backtests, Shadow, System, Audit.
- Health: `/healthz` (no auth) returns database health, trader heartbeat age, mode.
- Kill switch: `POST /api/kill` with `x-kill-token` and
  `{"confirm":"KILL","reason":"..."}` in shadow, `KILL_LIVE` in live.
- Export: `/api/export/decisions.jsonl`.

The dashboard is read-only except for the kill endpoint. It cannot place orders.

## Live checklist

1. Account funded above `MIN_NOTIONAL_TMN`; no stale open orders.
2. `make preflight` passes in live mode.
3. `MODE=live`, `LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY`.
4. Run a canary with the live YAML caps (250k max order, 10% position, 0.5% risk).
5. Watch reconciliation for 5 minutes; any mismatch kills the engine.
6. Confirm fees and fills in the dashboard and compare to the exchange.
7. Only then consider scaling caps (do not change hard limits in code without a
   new backtest).

Live mode refuses to start if the balance is below the minimum, no key, no exact
confirmation phrase, or `LIVE_ORDER_DRY_RUN` is set.

## Kill switch and flatten

```bash
make kill                         # writes data/KILL_SWITCH
python -m wallex_trader kill --reason "manual"
python -m wallex_trader flatten   # shadow flatten
# live flatten requires MODE=live + --confirm-live
```

The engine checks the file every second/equity cycle. On activation it cancels all
orders, flattens if `FLATTEN_ON_KILL=true`, writes critical risk/system events and
stops new entries. It does not auto-resume. Remove `data/KILL_SWITCH` and restart
deliberately after inspection.

## Stop and restart

- `Ctrl-C`/SIGTERM requests a graceful stop. The engine cancels open orders, saves
  final equity and position snapshots, writes shadow artifacts and closes the DB.
- Open positions are not automatically flattened on a normal stop unless
  `FLATTEN_ON_STOP=true`, which is recommended for a bounded live test. Live restores
  positions from the latest **live** `positions` snapshot and reconciles balances on
  the next start. Shadow starts a fresh simulated portfolio per run.
- On stop, the run summary is written to `artifacts/live/<run_id>/` (or shadow) with
  metrics, decisions, orders, fills, positions, equity and journal.
- If the process is killed hard, the run remains `running`; the next start marks old
  running runs `interrupted`. Check the DB and exchange before resuming live.
- `replay` verifies fills and hashes after a restart.

## Heartbeat and health

- Trader heartbeat every `HEARTBEAT_SEC` (15s).
- `python -m wallex_trader health --max-age 120` exits 1 if the heartbeat is stale.
- Docker health checks use this command for the trader and `/healthz` for the dashboard.
- The dashboard shows the latest WS stats, REST error rate, agent stats and kill state.

## Logs

- JSON lines to stdout/stderr and `logs/trader.jsonl`, rotated 20MB × 10.
- `logs/preflight.jsonl`, `logs/backtest.jsonl`, `logs/download.jsonl`, etc.
- Secrets are redacted from structured context; never log full API keys.
- Docker uses json-file logging with rotation (20MB × 5).

## Database and backups

- PostgreSQL data volume `pgdata`; local SQLite under `data/argus.db` (WAL).
- Back up `pgdata` or run `pg_dump`; artifacts are on the host `./artifacts`.
- The DB stores decisions, orders, fills, positions, equity, agent notes and LLM calls.
- The journal is also append-only on disk; back it up with artifacts.

## Troubleshooting

| Symptom | Cause / action |
|---|---|
| `401 authorization header is missing` | key not loaded; check `.env`, don't source it |
| `HTTP 503` on WebSocket | Wallex transient; engine reconnects with backoff; >30s halts |
| `quantity rounds to zero` / `min notional` | size below exchange minimum; AI min size and precision checks |
| `price more than 2% from mid` | tick/precision issue or bad limit offset; inspect market metadata |
| `DeepSeek returned empty content` | retried with larger budget/low effort; frequent → raise max tokens |
| `database is locked` (SQLite) | dashboard + trader contention; WAL should handle; prefer PostgreSQL for long runs |
| Dashboard 401 | use credentials from `.env`; browser basic auth |
| Kill switch active | inspect `risk_events`, clear `data/KILL_SWITCH`, restart deliberately |
| Live refuses to start | balance < minimum, missing confirmation, open orders, or preflight fail |
| Order 422 / code 1006, balance 0.39 TMN | API key is the main-account key while funds are in the prop sub-account. Use the prop account's API key and clear `WALLEX_SUB_ACCOUNT_CLIENT_ID`; preflight `api_key_matches_sub_account` must pass. |
| `deque already at its maximum size` | fixed in CandleStore; out-of-order candles are ignored instead of inserted into a full deque |
| Clock skew | NTP sync; live halts above 5s (Date-header granularity) |

## Challenge endgame

Set `CHALLENGE_START`/`CHALLENGE_END` (UTC). The engine:

- blocks new entries when remaining ≤ `NO_NEW_ENTRIES_BEFORE_END_SEC` (2h);
- flattens all positions when remaining ≤ `FLATTEN_BEFORE_END_SEC` (30m);
- writes an `endgame` exit reason and risk/system events.

If the timestamps are empty, endgame is disabled and positions can be held indefinitely
(subject to the 20-day time stop).
