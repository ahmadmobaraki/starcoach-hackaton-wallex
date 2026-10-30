# AGENTS.md — instructions for AI coding agents

This file is the authoritative operating guide for any AI agent or human engineer
working on ARGUS in this repository. Read it before changing code, configuration,
strategy, risk, prompts, docs, or infrastructure. If a rule here conflicts with a
casual request, ask for explicit confirmation; do not silently weaken a safety control.

Project: `/var/www/projects/hackatons/wallex`
Type: Python 3.12, async, spot-only AI trading system for Wallex TMN markets
Last verified: 2026-10-23

---

## 0. Thirty-second orientation

ARGUS is a complete trading system, not a notebook:

- Live Wallex REST + WebSocket + MCP clients.
- A deterministic **slow trend strategy** with a configurable signal timeframe (default 4h) and a legacy 1m intraday engine.
- DeepSeek makes the actual entry decision, chooses size/order style/stop/TP/horizon,
  can veto, can request normal exits, and writes post-trade lessons.
- A deterministic risk kernel can veto or force exits and owns all hard limits.
- A shadow broker and a guarded live broker share the same OMS/portfolio path.
- PostgreSQL/SQLite audit trail, FastAPI dashboard, HTML backtest reports, Docker.
- Current live state: funded prop account (5,000,000 TMN), live preflight passes. Two
  buys filled in a later live run; the base-asset fee/entry-metadata incident (§18.15)
  was resolved by selling at 07:32. Cash is **4,996,524 TMN**, dust ~3 TMN, no open
  orders. Earlier: the first attempt used the main-account key and was rejected; the
  second ran 24 minutes with no 4h signal and correctly did nothing.

The single most important economic fact: Wallex TMN fees are **0.25% maker / 0.30%
taker**, so a taker round trip costs about **55–60 bps** before spread/slippage. This is
why the default is a multi-day trend strategy, not a scalper.

---

## 1. Mission and non-negotiables

1. **Spot only, long only.** No leverage, margin, futures, borrowing, or shorting.
2. **The risk kernel is sovereign.** The AI cannot change hard limits, enable leverage,
   withdraw funds, or place an order that the kernel/broker rejects.
3. **No manual trading.** The dashboard has no order buttons. Kill/flatten are risk
   operations, not profit-seeking controls.
4. **No secrets in code, logs, docs, commits, or shell output.** `.env` is gitignored.
5. **Live orders require explicit human intent.** Never start live mode on your own
   initiative. Use a preflight, the canary caps, and a bounded run.
6. **Don't force trades.** No signal means no trade. Activity is worth 15 points;
   profit is 90 and loss control is 30. High-frequency scalping loses after fees in
   this market (see §7.3).
7. **Every decision is auditable.** Keep decision, prompt, response hash, risk checks,
   orders, fills and agent notes intact.
8. **Preserve reproducibility.** Update configs, tests, locks, docs and results together.

---

## 2. Quick command reference

```bash
# Environment
make install                 # uv sync --frozen --extra dev
make lint                    # ruff
make typecheck               # mypy
make test                    # pytest (72 passed, 1 live skipped)
make test-live               # RUN_LIVE_TESTS=1, public Wallex API test

# Validation without real orders
make preflight               # REST/WS/MCP/DeepSeek/account checks; writes artifact
make self-test               # deterministic shadow OMS/risk round trip
make agent-test              # DeepSeek decision on a synthetic candidate
make shadow                  # live-data shadow mode, never sends an order
make dashboard               # http://127.0.0.1:8080/test-results

# Research
make download                # 1m history 2026-05-01 → 2026-10-21
make backtest                # event-driven backtest + report
make study                   # parameter sensitivity grid
make ablation                # deterministic vs cached-AI comparison
DECISIONS=path AI_CACHE=path make ai-review

# Operations
make live                    # only with explicit confirmation env (see §15.5)
make flatten                 # shadow by default; live requires confirmation
make kill                    # file kill switch
make replay                  # verify latest trading run
make up / make down          # Docker stack
make logs
make proxy                   # optional Caddy profile
```

Direct CLI equivalents:

```bash
python -m wallex_trader <command>
```

Valid commands: `run`, `shadow`, `live`, `preflight`, `download`, `backtest`,
`study`, `ablation`, `ai-review`, `replay`, `flatten`, `kill`, `dashboard`,
`health`, `self-test`, `agent-test`, `config`.

---

## 3. Current state (do not hallucinate older states)

- Mode in `.env`: `shadow`. Live is started with shell environment overrides.
- **Deployment:** live runs on VPS `78.141.219.4` under `argus-live.service`
  (Docker Compose + systemd, enabled at boot, restart-on-failure). Dashboard:
  `http://78.141.219.4:8080/test-results`. See §15.9 for access and operations.
- Strategy: `STRATEGY_MODE=swing` (slow trend; `SWING_TIMEFRAME_HOURS=4` default), universe **11 symbols**:
  BTCTMN, ETHTMN, SOLTMN, USDTTMN, XRPTMN, DOGETMN, PEPETMN, XAUTTMN,
  BNBTMN, TRXTMN, LTCTMN. ZECTMN was removed because ZEC/TMN is not tradable in the
  credit account; see `configs/blocked_markets.txt`.
- Prop sub-account: **5,000,000 TMN**, 0 open orders.
- `.env` now contains the prop-account API key and the prop sub-account client ID.
- Live preflight: **PASS** (`api_key_matches_sub_account=true`, balance 5M).
- **Real live run:** `live_20261023_062625_nvuw` (06:26–07:17) filled PEPE 149,712 and
  BNB 0.0003 (net fee-adjusted: 149,262.864 PEPE / 0.0002991 BNB). Base-asset fees and a
  live `entry_meta` overwrite caused a reconciliation kill and rejected exits. The
  positions were sold at 07:32 by a follow-up run; the 07:34 flatten command found only
  unsellable dust and sent zero orders. Do not trade the dust.
- **Live execution test:** `live_20261023_120849_ogmm` (12:08–12:50 UTC) bought BNBTMN
  **186k TMN** at 12:09 (1h swing, 100k floor respected). Pre-existing BNB dust then
  caused a false reconciliation mismatch, a kill, and a flatten that left a
  sub-step remainder; the engine retried the unsellable remainder until stopped.
  Round-trip loss ~4.4k TMN. Current cash **4,990,623 TMN**, BNB dust 0.00002346
  (~4.8k TMN), no positions or open orders. Do not trade the dust. Fixed by the
  dust-aware reconciler, sub-step remainder drop, and single-live-run lock (§18.16).
- Earlier: first live attempt (`live_20261022_131200_0w87`) used the main-account key and
  was rejected; the second (`live_20261022_152628_52xl`) ran 24 minutes with **0 decisions**
  because no completed 4h bar produced a candidate.
- **Live canary:** `live_20261023_095238_krcf` (09:52–11:18 UTC) ran in live mode with
  **0 decisions, 0 orders, 0 fills**, no kill, 6 WS reconnects, 0 REST errors, flat
  equity at 4,996,524 TMN, and `replay` PASS. It ended before the 12:00 UTC 4h close, so
  it validated live startup/reconciliation only; the execution path is still unproven.
- **WS-kill idle (2026-10-24 15:30 → 2026-10-25):** run `live_20261024_113452_7iq6`
  bought USDTTMN at 11:35 and TRXTMN at 12:00. Wallex WS dropped (3012 no-pong) at
  15:29:40, the 30s reconnect hit HTTP 503 at 15:30:10, and the 30s `ws_down` kill fired
  at 15:30:11 because the reconnect backoff had ratcheted to 30s and never reset. Both
  positions were force-flattened; the process stayed healthy but sticky-killed for
  ~19h with a frozen scan, which is why no trades appeared. Cash ~4,961,355 TMN, flat.
  Fixed by the backoff reset + `WS_DOWN_CONFIRMATIONS` and the dashboard kill banner
  (§18.18); recovery required a deliberate restart.
- **Recovery (2026-10-25 13:12 UTC):** the fix plus a 35s wall-clock backstop was
  deployed and the service restarted. PEPE and USDTTMN were force-flattened on stop
  (realized ~−2.8k TMN, fees ~2.3k); the new run `live_20261025_131209_ncrt` opened a
  fresh BNBTMN position (~171k TMN notional). Kill inactive, WS healthy, 0 REST errors.
- Universe is now **11 symbols** (ZECTMN removed); `MIN_NOTIONAL_TMN=100000` is a
  validator-enforced floor. Live/shadow refuse to start if the allowlist contains any
  symbol from `configs/blocked_markets.txt`.
- Dashboard: LIVE badge has a blinking green dot while running; overview shows last
  scan, candidate count, regime and next signal-timeframe decision. A red banner shows
  an active kill switch (reason, trigger, activation time) so a sticky kill is visible.
- Tests: **72 passed, 1 skipped**; ruff clean; mypy clean (67 files); Docker build OK.
- Results snapshot: `results/`; full artifacts: `artifacts/` (gitignored).
- Canonical full report: `report.md`; detailed docs in `docs/`.

Headline research results:

| Run | Net | Max DD | Trades | Notes |
|---|---:|---:|---:|---|
| 8-symbol deterministic swing | +15.79% | 5.39% | 38 | headline baseline |
| AI-gated (cached DeepSeek) | +12.52% | 4.34% | 34 | 39/41 approved, 2 late entries vetoed |
| 12-symbol swing | +16.46% | 4.69% | 39 | current live universe |
| 2× fees | +9.36% | 5.53% | 37 | cost stress |
| 2× slippage (16 bps) | +15.23% | 5.34% | 37 | cost stress |
| 10-day horizon | +11.82% | 4.45% | 52 | more turnover, still positive |
| Original 1m MR/MOM | −3.94% | 4.12% | 48 | rejected after measured fees |

If you update results, update `report.md`, `docs/RESULTS.md`, `README.md` and
`results/README.md` consistently. Do not mix 8-symbol and 12-symbol numbers without
labelling them.

---

## 4. Hackathon priorities (why the design is conservative)

Trading table (180): final profit 90, loss control 30, algorithm activity 15,
execution stability 15.

Technical table (150): strategy logic 40, real AI use 35, risk/capital 30,
reproducibility 25, backtest + shadow 20.

Implications:

- Profit and loss control dominate. A high-activity negative-expectancy scalper is a
  bad trade even if it looks busy.
- AI must have a real role: veto, size, order style, stop/TP/horizon, normal exits,
  reflection. It is not a decorative call.
- A judge must be able to rerun everything from `README.md`, `Makefile`, configs,
  locks, artifacts and `results/`.
- "No trades" is acceptable when there is no edge, but the dashboard/heartbeat must
  explain why.

---

## 5. Project map

```text
src/wallex_trader/
  exchange/
    rest.py          Wallex REST, rate limits, precision, order helpers, balances
    ws.py            Centrifugo v2 WebSocket client, reconnect, app pong
    mcp.py           Wallex MCP JSON-RPC client, read-only allowlist
    auth.py          x-api-key + sub-account-client-id headers
    rate_limiter.py  token bucket + order sliding window
  marketdata/
    feed.py          seeding, WS/REST fallback, 1m/1h stores, health
    candles.py       CandleStore, out-of-order handling
    orderbook.py     BookStore, snapshots, metrics
    tape.py          trade tape, aggressive flow
    features.py      live features, cross-asset, feature hash
  strategy/
    universe.py      allowlist/volume selection
    swing.py         slow trend, configurable timeframe, score, candidate
    signals.py       legacy intraday MR/MOM
    regime.py        range/trend/high-vol/illiquid classification
    exits.py         trailing stop, signal exits, cost-aware exits
    portfolio.py     cash/positions/PnL/snapshots, reservations
  agent/
    client.py        DeepSeek LLM, DecisionAgent, deterministic agent
    schemas.py       strict AgentDecision, ReflectionNote, SessionReview
    prompts.py       system prompt, payload, reflection/session prompts
    memory.py        agent journal, lessons in prompts
    reflection.py    post-trade and session reviews
    ab_test.py       CachedDecisionAgent, RiskOnlyAgent, ablation
  risk/
    kernel.py        pre-check, sizing, approve, approve_exit
    limits.py        RiskCheck/RiskApproval/RiskLimits
    killswitch.py    sticky file/manual kill state
  execution/
    precision.py     tick/step quantization, bps helpers
    state_machine.py order transitions
    shadow_broker.py pessimistic paper fills
    live_broker.py   live spot LIMIT orders, reconciliation
    oms.py           persist-before-send, fills, timeouts, reprices, reservations
    reconciler.py    balances/orders/positions reconciliation
  storage/
    models.py        SQLAlchemy tables
    db.py            async engine, SQLite/Postgres
    repositories.py  all read/write queries, JSON-safe serialization
  backtest/
    download.py      UDF downloader, manifests, gaps, loader
    precompute.py    vectorized features + swing features
    simulator.py     event-driven 1m simulator, pending entries, exits
    fills.py         backtest fill/price helpers
    metrics.py       Sharpe/Sortino/DD/win rate/etc.
    report.py        HTML/JSON/CSV/JSONL artifacts
    study.py         parameter grid
    ai_review.py     offline DeepSeek review of backtest candidates
  dashboard/
    api.py           FastAPI app, auth, pages, APIs, SSE, kill endpoint
    views.py         Jinja filters/globals
    templates/       base/overview/decisions/positions/memory/backtests/shadow/system/audit
    static/          app.css, vendored Chart.js
  engine.py          startup, loops, decisions, risk, OMS, reflection, shutdown
  cli.py             argparse CLI
  preflight.py       read-only live/shadow readiness checks
  replay.py          accounting/hash verification
  selftest.py        deterministic shadow OMS/risk test
configs/
  base.yaml, shadow.yaml, live.yaml, backtest.yaml
docs/
  ARCHITECTURE, STRATEGY, AI_AGENT, RISK, BACKTEST, SHADOW_TESTING,
  OPERATIONS, PARAMETERS, COMPLIANCE, DECISION_LOG, RESULTS
results/             committed lightweight evidence
artifacts/           full backtests, shadow/live runs, preflight, ablations (gitignored)
scripts/             preflight/download/shadow/live/flatten/smoke_test
tests/               unit + live integration
Dockerfile, docker-compose.yml, Caddyfile, Makefile
pyproject.toml, uv.lock, requirements.lock, .env.example
report.md            full human-readable project report
```

---

## 6. Architecture and data flow

1. `TradingEngine.start` loads settings, DB, REST, metadata, universe, fees, balances,
   then seeds 1m/1h candles and depth/trades.
2. `MarketDataFeed` starts the WebSocket, resyncs REST on connect, and runs a REST
   fallback loop.
3. Every 5s the decision loop builds candidates:
   - default `swing`: completed signal-timeframe bars (default 4h), new-bar detection, slow trend candidate;
   - optional `intraday`/`both`: 1m features and MR/MOM candidates.
4. Each candidate goes through risk pre-check, DeepSeek (with portfolio/context/notes),
   risk approve/sizing, then the OMS.
5. The position loop marks positions every second, enforces stops/trailing/time/kill,
   and processes fills/timeouts.
6. Equity, positions, orders, decisions, LLM calls and notes are persisted.
7. Reflection runs after closed trades and in periodic session reviews.
8. The dashboard reads the DB and artifacts; it cannot place orders.

Latency is not HFT. The slow strategy intentionally evaluates completed signal-timeframe
bars (default 4h); the 5s loop is for health and mid-bar re-entry/exit management, not
new signal entries.

---

## 7. Strategy specification

### 7.1 Default: slow trend (`STRATEGY_MODE=swing`)

Build completed bars of `SWING_TIMEFRAME_HOURS` (default 4) from 1h candles. Let:

- `C` = latest signal-timeframe close
- `F` = EMA(`SWING_FAST_EMA`, default 48) on signal bars
- `S` = EMA(`SWING_SLOW_EMA`, default 240) on signal bars
- `R24` = 24h momentum = `C / C[-6] - 1`
- `ATR` = mean true range over 14 signal bars, in bps

Entry:

```text
C > F > S
R24 > 0
```

Score:

```text
0.55
+ 0.15 * min(1, (C/S - 1) / 0.10)
+ 0.15 * min(1, (F/S - 1) / 0.05)
+ 0.15 * min(1, R24 / 0.10)
```

Cap 0.95; require ≥ `SWING_MIN_SCORE` (0.60).

Expected move / cost:

```text
expected_move = max(3.0 * ATR, 1.5 * R24 * 10000, 300 bps), cap 3500
cost = spread + 2 * taker_fee * 10000 + 8 + COST_BUFFER_BPS
net_edge = expected_move - cost
```

Require `net_edge >= ALPHA_THRESHOLD_BPS` and depth ≥ 5× max order.

Stops/TP/horizon:

```text
stop   = clamp(max(1000, 1.2 * ATR), 300, 1200) bps
target = clamp(max(2500, 2.5 * stop), 600, 3000) bps
horizon= 20 days (configurable; risk floor 7 days, max 30 days)
trail  = arm at +800 bps, distance 800 bps
```

Exit order: hard stop → take profit → trailing → option signal exit → time stop →
challenge endgame → final liquidation. Stop is assumed first if both stop and target
are inside a bar. Backtest trailing uses the prior bar's high-water mark (conservative).

### 7.2 Legacy intraday (`STRATEGY_MODE=intraday` or `both`)

The original 1m engine computes returns, EMA9/21/50, RSI14, Bollinger z, ATR, VWAP,
volume z, book imbalance, aggressive flow, BTC/ETH/USDT cross features and a regime.
It emits MR/MOM candidates. It is kept for research and for the optional activity sleeve.
On the 113-day sample it lost **−3.94%** over 48 trades after fees. Do not enable it by
default or claim it is profitable.

If the user asks for “1-minute scalping”, explain that:

- `SWING_TIMEFRAME_HOURS` is intentionally 1–24 hours (the slow strategy); 1m is a
  different engine.
- `STRATEGY_MODE=intraday` (or `both`) is the built-in 1m path; decisions scan every 5s,
  but the candidate horizon is still minutes, not HFT, and the fee hurdle remains.
- With the 25% sleeve, true backtests: 1m **−1.39% / 1.43% DD / 113 trades**, 2m
  **−1.55% / 1.68% / 103**, 5m **−2.77% / 2.86% / 115**. The earlier full-risk 1m
  variant lost −3.94% over 48 trades. A true 1m scalp cannot pay 55–60 bps round-trip
  costs reliably. Configure the bar size with `INTRADAY_TIMEFRAME_MINUTES=1|2|5` and
  keep `INTRADAY_RISK_MULTIPLIER` small.
- If experimenting, use `STRATEGY_MODE=intraday` with `INTRADAY_RISK_MULTIPLIER=0.10`
  in **shadow** first; do not start a full-size live 1m sleeve. Do not lower
  `ALPHA_THRESHOLD_BPS`/`Z_ENTRY` just to force fills; that converts a fee-aware
  candidate into a negative-expectancy one.
- `STRATEGY_MODE=both` combines the slow swing with the small 1m sleeve; it will trade
  more but the sleeve has negative expectancy in the measured sample.


### 7.3 Why faster is not the default

Measured on the same 113-day data after fees (250k notional per trade, 4 positions):

| Variant | Trades | Net | Max DD |
|---|---:|---:|---:|
| 1h trend (best) | 262 | −0.85% | 4.95% |
| 1h trend (tight stop) | 301 | −10.24% | 10.42% |
| 15m maker mean reversion | 103–1,175 | −2.73% to −43.8% | 2.8–43.9% |
| 4h range reversion | 54–133 | −1.12% to −6.71% | 1.8–7.3% |
| 4h breakout | 116–139 | −1.28% to −1.79% | 2.8–3.4% |

The fee load dominates. The correct way to increase opportunity is more symbols and,
if acceptable, a smaller horizon or a small activity sleeve — not 1m scalping.

### 7.4 How to get more trades (approved levers)

- **Signal timeframe**: `SWING_TIMEFRAME_HOURS=1` (2d/10d EMAs) was validated at +15.34% / 3.77% DD / 35 trades; `2` at +9.60% / 5.19% / 42; default `4` at +15.79% / 5.39% / 38. A 24h bar needs ~300 days of history and is not validated on the current 150-day sample.
- **Universe**: 11 tradable symbols in `.env`. The historical 12-symbol run (+16.46% / 4.69% DD / 39 trades) included ZECTMN, which is not tradable in the credit account and is now excluded.
- **10-day horizon**: `SWING_HORIZON_SEC=864000`; +11.82% / 4.45% DD / 52 trades.
- **Activity sleeve**: `STRATEGY_MODE=both` and `INTRADAY_RISK_MULTIPLIER=0.25`; it
  trades more but the intraday variants are negative after fees. Not recommended for
  real money; never enable as a silent default.
- **Never** lower `ALPHA_THRESHOLD_BPS`, `Z_ENTRY`, or stop size merely to force trades.

### 7.5 Why a live session may have no trades

The default evaluates **completed signal-timeframe bars** (`SWING_TIMEFRAME_HOURS=4` by
default; set 1 for hourly decision points). A 20–30 minute session may span only one
decision point (or none), and the market may be in a downtrend/range where no symbol
passes `C > F > S`. The 15:26 live run had 0 decisions for exactly this reason; the next
4h close was after the operator stopped. The dashboard now shows the last scan, candidate
count, regime and next decision time. Never “fix” this by inserting forced orders.

### 7.6 Universe

Default `.env` allowlist has 11 liquid TMN spot markets. Selection checks:

- not present in `configs/blocked_markets.txt` (account-specific hard blocklist);
- listed, spot, TMN-quoted;
- not flagged risky, not new (<14 days);
- 24h quote volume ≥ 5B TMN;
- valid price/precision.

Live and shadow modes refuse to start if `UNIVERSE_ALLOWLIST` contains a blocked
market. A blocked market is also excluded by `UniverseSelector` with reason
`not tradable in this account`. Do not re-add a blocked market, and do not bypass the
blocklist for convenience.

Do not permanently exclude a symbol for a transiently wide book; the risk kernel
checks spread/depth at order time.

---

## 8. AI agent specification

### 8.1 Models and roles

| Role | Model (default) | Effort |
|---|---|---|
| Entry/exit decision | `DEEPSEEK_MODEL` (`deepseek-flash`) | low |
| Regime memo | `DEEPSEEK_DEEP_MODEL` (`deepseek-v4-pro`) | low |
| Trade reflection | `DEEPSEEK_DEEP_MODEL` | high |
| Session review | `DEEPSEEK_DEEP_MODEL` | high |

All calls are recorded in `llm_calls` with prompt, raw response, latency, tokens,
success/error and kind. Empty content is retried with double tokens and low effort,
then the decision HOLDs. Live defaults to HOLD if DeepSeek is unavailable
(`ALLOW_AGENT_FALLBACK=false`).

### 8.2 Decision schema

`AgentDecision` is strict (`extra="forbid"`):

```json
{
  "action": "BUY | SELL | HOLD",
  "confidence": 0.0,
  "size_fraction": 0.0,
  "order_type": "LIMIT | MARKETABLE_LIMIT",
  "limit_offset_bps": 0.0,
  "take_profit_bps": 2500,
  "stop_loss_bps": 1000,
  "horizon_sec": 1728000,
  "regime": "trend_up",
  "rationale": "<= 2 sentences",
  "risk_flags": ["..."]
}
```

Rules:

- BUY/SELL require confidence > 0 and size > 0.
- HOLD forces size 0.
- TP ≥ stop.
- TREND horizon **7–30 days**; TREND size ≥ `AGENT_MIN_SIZE_FRACTION` (0.70).
- Unknown fields/JSON invalid → one correction retry → HOLD.
- The risk kernel can veto or shrink; it never initiates.

### 8.3 Prompt contents and real AI role

The prompt includes candidate features/edge/costs, portfolio, risk limits, regime memo,
recent decisions, journal lessons, and `market_context.other_candidates` at the same
signal-timeframe close. The AI:

- can veto (it vetoed 2/41 extreme late entries in the final review);
- chooses 0.7–1.0 size for TREND;
- chooses MARKETABLE_LIMIT vs LIMIT;
- sets stop/TP/horizon within bounds;
- can request normal exits (TREND exit review is disabled by default because rules own
  the slow trend);
- writes lessons.

It cannot change hard limits, allowlist, leverage, funding, or call state-changing MCP
tools. Prompt/response hashes and the full LLM call are stored. The prompt cache key is
`sha256(model + temperature + prompt_version + full_payload)`.

### 8.4 Learning loop

After a closed trade, `ReflectionAgent` asks v4-pro for a structured review: note,
lesson, tags, importance, confidence calibration, advisory adjustment, avoid patterns.
The note is stored in `agent_notes` and appended to `artifacts/agent_journal.md`, then
injected into future prompts. Periodic session reviews summarize the session. Lessons
are advisory only.

### 8.5 AI ablation

`ai-review` asks DeepSeek to review historical candidates without outcome knowledge,
writes a cache, then `backtest --agent-mode cached` replays the approved subset. Final
review: 39 BUY / 2 HOLD; AI-gated +12.52% / 4.34% DD / 34 trades vs deterministic
+15.79% / 5.39% / 38. This is the honest AI effect: lower return, lower drawdown, a
different trade set. Do not cherry-pick only the higher number.

---

## 9. Risk kernel

### 9.1 Sizing

```text
risk_amount   = equity * RISK_PER_TRADE
stop_distance = entry * stop_bps / 10000
qty_risk      = risk_amount / stop_distance
qty_position  = equity * MAX_POSITION_PCT / entry
qty_liquidity = top5_depth * MAX_DEPTH_FRACTION / entry
qty_order     = MAX_ORDER_NOTIONAL_TMN / entry
qty_cash      = available_cash / (entry * (1 + fee))
qty           = floor_to_step(min(...) * ai_size_fraction)
```

Then apply soft de-risk (×0.5 at 3% DD) and, for non-TREND signals, the intraday
multiplier (default 0.25). Checks include min notional, gross exposure, portfolio heat,
correlated exposure, cash buffer, spread/depth/age, slippage, price sanity, self-trade,
open order/position caps, daily loss, drawdown, volatility, WS/API health, clock skew,
and endgame. Orders below **100,000 TMN** are vetoed; the kernel never upsizes a
counter to clear the floor. The score gate follows the candidate's generation floor:
`TREND` uses `SWING_MIN_SCORE` (0.60), `MR`/`MOM` use `CANDIDATE_THRESHOLD` (0.65).
Keep these aligned; otherwise candidates are generated and then always vetoed
(`signal score below threshold`).

### 9.2 Defaults (shadow/backtest)

| Limit | Value |
|---|---:|
| Risk/trade | 0.75% |
| Max position | 20% |
| Max gross exposure | 60% |
| Max portfolio heat | 3% |
| Max open positions | 4 |
| Max correlated exposure | 35% |
| Min cash buffer | 25% |
| Daily loss limit | 2.5% |
| Peak drawdown kill | 6% |
| Soft de-risk | 3% |
| Hard de-risk | 4.5% |
| Max order | 750,000 TMN |
| Min order | 100,000 TMN |
| Max spread | 50 bps |
| Max slippage | 25 bps |
| Stop / TP | 10% / 25% |
| Trailing | +8% arm / 8% distance |
| Horizon | 20 days |

Live canary is enforced by `Settings._validate` and `configs/live.yaml`:
**250,000 TMN max order, 100,000 TMN min order, 10% max position, 0.5% risk**. Do not
bypass these. Environment variables were intentionally removed for the order/position/risk
caps so the live YAML applies; the 100,000 TMN floor is validator-enforced in every mode.

### 9.3 Kill switch

Activates on file/manual kill, peak drawdown 6%, daily loss 2.5%, WS down 30s for
3 consecutive health checks (or 35s on any check), API
error rate >10% after 50 calls, clock skew >5s for 3 consecutive checks (single
sample can be stale), live reconciliation mismatch
(3 consecutive checks), or manual dashboard/file request. Actions: no new entries,
cancel all orders, flatten if configured, critical risk/system events. Kill is sticky;
restart is deliberate. `FLATTEN_ON_STOP=true` flattens on a normal Ctrl-C stop.

### 9.4 Reconciliation

Live compares open orders and balances/positions. It requires **3 consecutive
mismatches** before killing (a transient one-check race after a rejected order caused a
false kill in the first live attempt). It tracks **known dust** per base asset — ignored
at startup and created by sub-step exit remainders — and subtracts it before comparing
the exchange balance to the local position. A remainder below the market step or the
exchange minimum notional is removed from the portfolio and recorded as dust instead of
being retried forever. Always include full details in the risk event.

Only **one live process** may run at a time. `data/runs/live.lock` records the PID and
run ID; a second live start refuses to run while that PID is alive, and stale locks are
reclaimed. Never run two live processes against the same account.

---

## 10. Execution

- Spot LIMIT only; MARKETABLE_LIMIT is a LIMIT with an aggressive price capped by
  `MAX_SLIPPAGE_BPS`. No unbounded market orders.
- Payload fields: `client_id`, `symbol`, `side`, `type`, `price`, `quantity` (strings).
  `client_id` is unique and uses letters/digits/underscore only.
- Persist the order **before** sending. On ambiguous POST timeout, query by client ID
  before deciding it failed. Never blind-retry order creation.
- Prices quantized to tick; quantities floored to step. Wallex `/v1/markets`
  `tickSize`/`stepSize` are decimal-place counts on some markets; derive tick/step from
  `price_precision`/`amount_precision`. Do not reintroduce the raw tick fields.
- Self-trade prevention blocks an opposite open order on the same symbol.
- Passive orders are cancelled/replaced up to `MAX_REPRICES` after timeout; live reprice
  is intentionally not automatic (avoid chasing); shadow may reprice.
- Fees are TMN; base-asset fees are converted at fill price.
- Orders are linked to `decision_id`; every fill has a decision ID.
- `LiveBroker.reconcile` must not be removed.

### Sub-account / key

The prop account has its own API key. The master/main key can read the sub-account with
`sub-account-client-id` header but order routing uses the key's own account. The current
`.env` has the prop key; live preflight verifies:
`api_key_matches_sub_account=true` (main TMN == sub TMN). If these differ, live must
refuse to start. Never trade the main account by accident.

---

## 11. Data feed

- REST paths: `/hector/web/v1/markets`, `/v1/markets`, `/v1/depth`, `/v1/trades`,
  `/v1/udf/history`, `/v1/account/*`.
- UDF 1m history is fetched in **day-aligned UTC windows**; a 7-day 1m request returns
  `no_data`. The downloader checkpoints daily Parquet + raw gzip JSON + manifest/gaps.
- 1h candles feed the strategy-timeframe swing (default 4h); UDF `240` falls back to 1h on Wallex, so ARGUS
  resamples locally. Fetch 60 days at startup and refresh the last 2 days periodically.
- WS uses Centrifugo v2. Depth channels replace local sides; candles upsert. Reconnect
  with backoff and REST resync. Do not send a proactive `{}` ping; only reply `{}` to a
  server app ping (proactive pings caused 3501 bad-request disconnects).
- `CandleStore.upsert` ignores older out-of-order candles. Never insert into a full
  bounded `deque` (raises `IndexError: deque already at its maximum size`; this caused
  the WS/REST storm on 2026-10-22). The hot path only needs newest bars; history is seeded.
- Clock skew uses HTTP `Date` headers (1s granularity); threshold is 5s, not 1s.
- Feed health drives entries: WS connected, fresh books, max candle age.

---

## 12. Storage and audit

Core tables: `runs`, `market_snapshots`, `trades`, `features`, `decisions`, `orders`,
`fills`, `positions`, `equity`, `risk_events`, `llm_calls`, `agent_notes`,
`system_events`, `backtest_runs`, `shadow_runs`, `llm_cache`, `heartbeats`.

- Decimal values are stored as strings; JSON is sanitized with `json_safe` (including
  dataclasses, Decimal, datetime, Path, Enum).
- `decisions` stores candidate/features/agent/risk/execution and prompt/response hashes.
- `llm_calls` stores the full prompt and raw response.
- `replay` verifies decision IDs, order/fill linkage, agent response hashes and
  reconstructs realized PnL/fees from fills. It is not a re-call of DeepSeek.
- Do not hand-edit DB rows to “fix” a run; use `replay`/documented migrations.

---

## 13. Dashboard

- URL: `/test-results` (root redirects). Health: `/healthz` (no auth).
- HTTP basic auth from `.env`; if password is empty, auth is disabled (local only).
- Tabs: Overview, Decisions, Positions, Agent memory, Backtests, Shadow, System, Audit.
- **LIVE badge**: a blinking green dot appears inside the badge while `run.mode=live`
  and `run.status=running`; JS polls `/api/overview` every 8s and removes the dot when
  stopped. CSS is `.live-dot` with `@keyframes livePulse`.
- Overview shows last scan, candidate count, regime and next decision time from heartbeat
  details. Do not remove this; it explains no-trade periods.
- No order-entry controls. The only mutation is `POST /api/kill` with
  `x-kill-token` and `{"confirm":"KILL"}` (shadow) / `{"confirm":"KILL_LIVE"}` (live).
- Logs, decisions, config and notes are redacted for secrets; never add key display.

---

## 14. Configuration and environment

`.env` is the secret/runtime file; `.env.example` is the committed template.
Do not source `.env` in a shell: the Wallex key may contain `|`/`$` and break the shell.
The app and Compose use dotenv parsing.

Key variables (not exhaustive):

- `MODE=shadow|live|backtest`, `RUN_ID`, `LIVE_CONFIRM`, `CHALLENGE_START/END`.
- `WALLEX_API_KEY`, `WALLEX_SUB_ACCOUNT_CLIENT_ID`, `WALLEX_API_BASE/WS/MCP`.
- `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_DEEP_MODEL`, `DEEPSEEK_BASE_URL`,
  `DEEPSEEK_REASONING_EFFORT`, `DEEPSEEK_DEEP_REASONING_EFFORT`.
- `POSTGRES_DSN`, PostgreSQL Compose credentials.
- `DASHBOARD_HOST/PORT/BASIC_AUTH_USER/BASIC_AUTH_PASSWORD/KILL_TOKEN`.
- `UNIVERSE_ALLOWLIST`, `UNIVERSE_MAX_SYMBOLS`.
- `STRATEGY_MODE`, `INTRADAY_TIMEFRAME_MINUTES`, `INTRADAY_RISK_MULTIPLIER`, `SWING_TIMEFRAME_HOURS`, `SWING_FAST_EMA`, `SWING_SLOW_EMA`, `SWING_*`, `Z_ENTRY`,
  `ALPHA_THRESHOLD_BPS`, `CANDIDATE_THRESHOLD`.
- `RISK_PER_TRADE`, `MAX_POSITION_PCT`, `MAX_ORDER_NOTIONAL_TMN` are **mode YAML**
  values for live (do not re-add them to `.env` unless intentionally changing a mode).
- `MAX_OPEN_POSITIONS`, `MAX_PORTFOLIO_HEAT`, `DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN`,
  `CLOCK_SKEW_KILL_SEC`, `WS_DOWN_KILL_SEC`, `FLATTEN_ON_STOP`, `FLATTEN_ON_KILL`.
- `AGENT_*`, `BACKTEST_*`.

Priority: real environment > `.env` > mode YAML > field defaults. `load_settings(mode)`
forces the selected mode for that process.

Run live only with explicit environment and confirmation:

```bash
MODE=live LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY FLATTEN_ON_STOP=true make live
```

Do not set `LIVE_CONFIRM` in `.env` unless you intend every `run` to be live-eligible.
Never run live in a loop, background process, or without a human watching.

### Locks

- `uv.lock` via `uv lock`.
- `requirements.lock` via:
  `uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.lock`
- Docker installs from `requirements.lock`; update both when dependencies change.

---

## 15. Workflows

### 15.1 Add or change a symbol

1. Confirm it is a liquid TMN spot market and not risky/new.
2. Download history: `python -m wallex_trader download --symbols SYM --start 2026-05-01 --end 2026-10-21`.
3. Add to `UNIVERSE_ALLOWLIST` (`.env`/example) and, if intended as default,
   `configs/base.yaml`.
4. Run a backtest with explicit `--symbols` including the new symbol; compare return,
   DD, trade count, per-symbol contribution; run 2× cost stress.
5. Update `docs/RESULTS.md`, `report.md`, `README.md` if the default changes.
6. Do not add a symbol with no historical data to the live default.

### 15.2 Change risk

- Change code + `configs/base.yaml`/`live.yaml` + `.env.example` + docs.
- Add/extend unit tests (`tests/unit/test_risk.py`).
- Re-run backtest and stress; if drawdown/return worsens materially, revert.
- Never lower `MIN_NOTIONAL`, precision checks, self-trade prevention, kill conditions.
- Live caps are validator-enforced; do not remove the guard.

### 15.3 Change the strategy

- Implement both live (`marketdata/features.py`/`strategy/*`) and backtest
  (`backtest/precompute.py`/`simulator.py`) paths; they must stay consistent.
- Add a no-lookahead test (`tests/unit/test_precompute.py`).
- Run 113-day backtest, 2× fees, 2× slippage, and an AI-gated ablation.
- Update `docs/STRATEGY.md`, `docs/PARAMETERS.md`, `docs/BACKTEST.md`, `report.md`.
- Do not switch the default to a faster strategy without evidence; see §7.3.

### 15.4 Backtest and artifacts

```bash
python -m wallex_trader backtest --symbols ... --start ... --end ...
```

Artifacts: `artifacts/backtests/<run_id>/report.html`, `metrics.json`, `equity.csv`,
`trades.csv`, `decisions.jsonl`, config/data manifest. The CLI saves a DB row; the
dashboard Backtests tab reads it. Reports downsample charts to ~2,000 points; keep it.

### 15.5 Live run (human-driven)

1. Confirm the user explicitly wants live; never start it on your own.
2. `MODE=live LIVE_CONFIRM=... .venv/bin/python -m wallex_trader preflight` must be
   `ok:true`, `api_key_matches_sub_account:true`, balance ≥ minimum, 0 open orders.
3. Start foreground with `FLATTEN_ON_STOP=true` (recommended) and a bounded duration;
   watch dashboard/heartbeat/reconciliation.
4. Ctrl-C once; wait for `engine stopped`; inspect `artifacts/live/<run_id>/`.
5. Run `python -m wallex_trader replay --run-id <id>`; verify PASS.
6. Do not run live in Docker/background unattended. The only approved exception is the
   operator-requested VPS deployment in §15.9 (systemd unit, canary caps, preflight,
   `FLATTEN_ON_STOP=true`). Do not create a second live process or tmux copy.

### 15.6 Analyze a run

- DB: `runs`, `decisions`, `orders`, `fills`, `positions`, `equity`, `risk_events`,
  `llm_calls`, `agent_notes`.
- Files: `artifacts/live|shadow/<run_id>/metrics.json`, decisions, orders, fills,
  positions, equity.csv, journal.
- `python -m wallex_trader replay --run-id <id>`.
- Compare fills/fees to exchange only via read-only endpoints; never place a test order.

### 15.7 Debug “no trades”

Check in order:

1. Is the process alive? heartbeat age <90s, status running.
2. Is the kill switch active? `risk_events`, `data/KILL_SWITCH`.
3. Is the data healthy? WS connected, books fresh, candles.
4. What does the scan say? last scan/candidate count/regime/next strategy-timeframe decision.
5. Is it a decision time? The default only opens positions at completed signal-timeframe bars.
6. Is the market in a downtrend/range? `C > F > S` may be false; HOLD is correct.
7. If `STRATEGY_MODE=both`, are intraday thresholds/edge/spread/depth satisfied?
8. Never insert a forced order or lower risk to make activity.

### 15.8 Dependencies

- Add to `pyproject.toml` with bounds; run `make lock`.
- Regenerate `requirements.lock`; rebuild Docker; run tests/lint/mypy.
- Do not add a package without need; keep the image lean.

### 15.9 VPS deployment and operations (live)

The live system runs on VPS `78.141.219.4`. Access is **SSH-key only**; the server
password must never be written to the repo, docs, logs, or shell history.

```bash
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4
```

| Item | Value |
|---|---|
| Project | `/opt/argus` |
| Remote `.env` | `/opt/argus/.env`, root-only mode 600, authoritative |
| Database | Docker `db` (Postgres, volume `argus_pgdata`) |
| Dashboard | Docker `dashboard`, `DASHBOARD_BIND=0.0.0.0:8080`, HTTP basic auth |
| Live trader | systemd `argus-live.service` → container `argus-live-trader` |
| Unit | `/etc/systemd/system/argus-live.service` |
| Restart | `Restart=on-failure`, `RestartSec=30`, max 5 starts / 300s; enabled at boot |
| Shutdown | `FLATTEN_ON_STOP=true`; `systemctl stop` **and `restart`** sell open positions |

The remote `.env` must set `DASHBOARD_BIND=0.0.0.0` for the public dashboard; the
Compose default is `127.0.0.1`. Never run `make up` on the VPS while `argus-live` is
active: it starts the Compose `trader` service as a second (shadow) engine against the
same DB, API key and WebSocket.

Operations:

```bash
systemctl status argus-live
journalctl -u argus-live -f
systemctl stop argus-live      # flattens positions (FLATTEN_ON_STOP)
systemctl start argus-live
systemctl restart argus-live   # also flattens: stop + start
cd /opt/argus && docker compose ps
docker exec argus-live-trader python -m wallex_trader health --max-age 120 --mode live
```

`--mode live` requires the current image; a container started before 2026-10-24 does
not have it. Use the same command without `--mode`, or run it through
`docker compose run --rm --no-deps trader ...`, until that container is restarted.

Deploy a code change (run tests locally first; never copy `.env`). **Flatten first**:
`systemctl restart` closes every open position because the unit sets
`FLATTEN_ON_STOP=true`. Verify the `positions` table has no open quantity before
deploying, or accept a forced market exit and its fees.

```bash
rsync -az --delete -e "ssh -i ~/.ssh/ahmad_mobaraki_ed25519" \
  --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude 'artifacts/' \
  --exclude 'logs/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.mypy_cache/' --exclude '.ruff_cache/' --exclude '.env' \
  ./ root@78.141.219.4:/opt/argus/
ssh -i ~/.ssh/ahmad_mobaraki_ed25519 root@78.141.219.4 \
  'cd /opt/argus && docker compose build trader && systemctl restart argus-live'
```

Rules:

- Only one live engine per account. The fixed container name `argus-live-trader` is the
  primary guard; `data/runs/live.lock` is an OS advisory `flock` on the shared data
  volume and also excludes a differently named live container. Never start a second
  live process, `docker compose run ... live`, or a tmux copy while it is active.
- A code change is not deployed until it passes ruff, mypy and pytest locally; after
  deploy, run a live preflight (`ok:true`, `api_key_matches_sub_account:true`,
  0 open orders) before trusting the new process.
- Do not `rsync` `.env`; edit it on the server and keep mode 600.
- The dashboard is plain HTTP with basic auth. Prefer an SSH tunnel
  (`-L 8080:127.0.0.1:8080`) or Caddy/TLS. Never disable basic auth.
- systemd `Restart=on-failure` can clear an in-memory kill after a crash. This is an
  operator-approved trade-off for the VPS; a kill that does not crash the process
  stays sticky until a deliberate restart.

---

## 16. Testing and validation

```bash
make lint
make typecheck
make test
make self-test
make agent-test
make preflight
make shadow -- --duration 120     # bounded
docker build -t argus-local .
```

- Current: 72 passed, 1 skipped (live integration).
- Add unit tests for any new behavior; keep tests fast and offline by default.
- `RUN_LIVE_TESTS=1 pytest -m live` exercises public Wallex endpoints only.
- `self-test` and `agent-test` must never send a network order.
- Before claiming done: ruff, mypy, pytest, Docker build, and a bounded shadow run if
  engine/feed/strategy changed.
- Do not mark tests passed unless actually run.

---

## 17. Docker and operations

- `Dockerfile` runs as UID/GID 1000; host-mounted `data/`, `artifacts/`, `logs/` must be
  writable by that UID. Compose mounts them; `docker compose down` keeps volumes.
- Dashboard binds `${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-8080}` by default;
  set `DASHBOARD_BIND=0.0.0.0` explicitly (the VPS does) to expose it publicly. Basic
  auth is disabled when `DASHBOARD_BASIC_AUTH_PASSWORD` is empty, so never expose a
  password-less dashboard. Trader has no exposed port; Caddy profile optional.
- Trader healthcheck uses `python -m wallex_trader health --max-age 120`.
- `docker compose --profile tools run --rm backtest` seeds PostgreSQL backtest rows.
- Logs rotate; `logs/trader.jsonl` is the aggregate JSON log and `logs/runs/<run_id>.jsonl` is the per-run log. Every run line includes `run_id` and `mode`; the run ID is printed at startup.
- Do not expose the dashboard publicly without basic auth/Caddy/TLS.

---

## 18. Known incidents and pitfalls (read before changing related code)

1. **Precision**: Wallex v1 `tickSize`/`stepSize` can be decimal-place counts (PEPE
   tick 4, BTC step 8). Use `10^-price_precision` / `10^-amount_precision`. Raw fields
   caused “price 2% from mid” and zero quantities.
2. **Main key vs prop key**: the main key can read the prop sub-account via header but
   orders use the key’s account. Use the prop key; preflight now enforces
   `api_key_matches_sub_account`. First live order was rejected 422/1006 (0.39665 TMN).
3. **Deque storm**: out-of-order candle insert into a full `deque` raised repeatedly,
   killing WS. `CandleStore.upsert` now ignores older candles. Never reintroduce
   middle insertion on a bounded deque.
4. **False reconciliation kill**: a single transient mismatch right after the rejected
   order killed the engine. Now 3 consecutive checks; keep that hysteresis.
5. **Clock skew**: 1s threshold false-fired due HTTP Date granularity. Now 5s, and a
   single sample no longer kills: it must exceed 5s on 3 consecutive health checks
   (2026-10-24, a one-off bad REST `Date` sample froze a live run for 4h). The risk
   event now records the measured skew. Do not remove the confirmation hysteresis.
6. **WS proactive ping**: sending `{}` on a timer caused 3501 and reconnects. Only reply
   to server app pings.
7. **Universe too strict**: a transiently wide book removed a symbol for an hour.
   Selection is metadata/volume; order-time checks handle spread/depth.
8. **DeepSeek empty content**: retried with double tokens/low effort; regime memo uses
   low effort; failure → HOLD/fallback memo. Do not treat an empty response as a trade.
9. **Portfolio equity double count**: reserved cash was added to equity; fixed. Reserved
   cash is part of cash but unavailable for orders, not a second asset.
10. **Run ID collision**: simultaneous backtests used same timestamp/hash; now UUID/pid.
11. **Shadow/live methods**: ShadowBroker methods are sync; OMS uses `_await_if_needed`.
    Keep it.
12. **Live artifacts**: stop writes `artifacts/live|shadow/<run_id>/` including metrics,
    decisions, orders, fills, positions, equity, journal. Do not remove.
13. **Live restore**: live restores positions only from prior **live** runs, never
    shadow. Keep the mode filter.
14. **Flatten on stop**: `FLATTEN_ON_STOP=true` closes positions on normal stop; live
    artifacts are written after flatten. Keep this option for bounded tests.
15. **Base-asset fees + live entry metadata (2026-10-23)**: Wallex charged PEPE/BNB
    buy fees in the base asset. The old code booked gross quantity and a TMN fee, so
    portfolio quantities/cash diverged; reconciliation killed and sell orders for the
    gross quantity were rejected (`code 1006`, insufficient quantity). `LiveBroker` also
    overwrote OMS `raw_request`, losing `entry_meta` and silently using the 20-minute
    default horizon/stop. Fixes: `Fill.base_fee_quantity`, net position quantity,
    all-in average entry, preserve `entry_meta` under `exchange_payload`, exit-rejection
    backoff, entries disabled on kill, startup position reconciliation. Run a shadow
    test after any fee/quantity change.
16. **Dust reconciliation + sub-step remainder (2026-10-23 12:08)**: pre-existing BNB
    dust (0.00001619) was ignored at startup, but the reconciler compared the full
    exchange balance against the new BNBTMN position, killed after 3 checks, and the
    flatten left 0.00000727 — below the 0.00001 step. Retries floored to zero and
    spammed forever. Fixes: `Engine.dust` ledger, dust-aware `Reconciler.check`,
    `_drop_dust_remainder` after exits/at flatten, and a `data/runs/live.lock`
    single-live-run lock. Two live processes had also overlapped for ~1 minute; never
    start a second live process before the first logs `engine stopped`. `replay` now
    recovers base-asset fees from the persisted order payload, so the killed run
    replays PASS. Keep the reconciler, replay and lock unit tests green.
17. **Phantom position resurrection after shutdown flatten (2026-10-23 14:21)**:
    `FLATTEN_ON_STOP` sold the two open positions (fills and cash recorded), but
    `save_position_snapshot` only writes *open* positions, so the newest
    `positions` rows per symbol stayed at the last open quantity. The next live
    start restored two phantom positions the exchange no longer held; a large
    shortfall (dust balance vs stored quantity) passed the ≤2% fee adjustment
    branch untouched, survived three reconciliation checks, and killed the
    engine — its own recovery flatten attempts were then rejected (nothing to
    sell). Fixes: (a) `save_position_snapshot(snapshot, portfolio,
    closed_symbols=...)` writes zero-quantity tombstone rows; the equity loop
    diffs vanished positions and flushes tombstones every cycle, the stop path
    tombstones symbols held at stop, and startup dust drops tombstone too;
    (b) startup reconciliation clamps any stored position to the exchange
    balance after a >2% shortfall (keeps the critical `startup_position_mismatch`
    risk event as the audit signal) and lets the existing unsellable-dust logic
    drop the remainder; (c) restore skips zero rows and the dashboard position
    list filters them. `restore_positions` already ignored `quantity <= 0`.
    After a **reconciliation-killed start, wait for `engine stopped` before
    restarting** — the first restart legitimately killed because the DB state
    was stale, not because the exchange was wrong.
18. **WS backoff ratchet + sticky ws_down kill (2026-10-24 15:30)**:
    Wallex WS disconnected (3012 no-pong) at 15:29:40 and the reconnect attempt
    at 15:30:10 returned HTTP 503. The client backoff had already ratcheted to
    30s and was **never reset after successful connections**, so every later
    disconnect also waited ~30s. With the kill threshold exactly 30s and no
    confirmation window, the outage tripped the sticky `ws_down` kill at
    15:30:11; positions were force-flattened and the engine then sat idle for
    ~19h while the process stayed healthy (last scan frozen, no decisions).
    Fixes: `backoff_before_retry` resets to 1s after a connection stays up
    ≥10s, `WS_DOWN_CONFIRMATIONS=3` requires the threshold to be exceeded on
    3 consecutive 1s health checks with a `WS_DOWN_KILL_GRACE_SEC=5s` wall-clock
    backstop so a blocked position loop cannot delay the kill, and the dashboard
    shows a red kill banner. The kill itself is still sticky and still requires a
    deliberate restart.

---

## 19. Hard “do not” list

- Do not commit `.env`, keys, tokens, account IDs, or dashboard passwords.
- Do not `source .env`; use the application loader.
- Do not place live orders without explicit human instruction and preflight.
- Do not bypass or weaken the risk kernel, live canary, kill switch, reconciliation,
  precision, min notional, or self-trade prevention.
- Do not add leverage, shorting, margin, withdrawals, or manual order endpoints.
- Do not force trades, lower fees, ignore spread/slippage, or assume fills.
- Do not expose secrets in logs, dashboards, reports, or test output.
- Do not edit `.kilo/agent-manager.json` or other persisted UI state.
- Do not hand-edit production DB rows or artifacts to make results look better.
- Do not claim a live fill/profit unless it exists in `fills`/`equity`.
- Do not change the default from swing to intraday without a full cost backtest.
- Do not run live in background/Docker unattended.
- Do not commit or push unless explicitly asked.

---

## 20. Definition of done for a change

A change is done only when:

- [ ] Code and config are consistent (live + backtest paths).
- [ ] Unit tests added/updated; `pytest` passes.
- [ ] `ruff` and `mypy` pass.
- [ ] Backtest and 2× cost stress run for strategy/risk changes; report updated.
- [ ] Shadow smoke run for engine/feed/execution changes.
- [ ] Docs (`docs/`, `report.md`, `README.md`, `results/` if results) updated.
- [ ] No secrets, no forced trades, no weakened limits.
- [ ] Docker build still succeeds if packaging changed.
- [ ] Live behavior was not exercised without explicit user consent.

---

## 21. Documentation index

- `AGENTS.md` — this file.
- `README.md` — quick start, commands, status.
- `report.md` — full human-readable project report, scorecard mapping, results, incidents.
- `docs/ARCHITECTURE.md` — components, flows, storage, artifacts.
- `docs/STRATEGY.md` — signals, formulas, exits, activity trade-off.
- `docs/AI_AGENT.md` — DeepSeek role, schema, prompts, memory, ablation.
- `docs/RISK.md` — sizing, caps, kill conditions, execution safety.
- `docs/BACKTEST.md` — data, simulator, results, stress, limitations.
- `docs/SHADOW_TESTING.md` — semantics, evidence, acceptance.
- `docs/OPERATIONS.md` — preflight, live canary, dashboard, troubleshooting.
- `docs/PARAMETERS.md` — parameter provenance, plateaus, study.
- `docs/COMPLIANCE.md` — spot-only, no manual/self-trade, AI boundaries.
- `docs/DECISION_LOG.md` — schema, hashes, replay, queries.
- `docs/RESULTS.md` — consolidated measured results.
- `results/README.md` — lightweight committed reports/evidence.
- `configs/*.yaml` — mode defaults; `.env.example` — environment reference.

---

## 22. Final instruction to the agent

Be conservative with real money. Prefer an honest “no trade” over a forced trade, an
honest negative result over a cherry-picked number, and a documented safety guard over a
convenient bypass. If you are unsure whether an action is live-money or changes risk,
stop and ask the human. Keep this file and the docs in sync with the code.
