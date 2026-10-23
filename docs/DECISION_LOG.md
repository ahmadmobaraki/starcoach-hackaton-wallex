# Decision log and audit

## Principle

Every entry decision is a record, not a log line. A judge or operator can reconstruct
why a trade happened, what the AI saw, what the risk kernel checked, what was sent,
what filled and what it cost.

## Tables

### `decisions`

| Field | Meaning |
|---|---|
| `decision_id` | deterministic unique ID |
| `run_id`, `ts`, `symbol`, `mode` | context |
| `action` | BUY / SELL / HOLD |
| `confidence`, `signal`, `score` | AI and deterministic inputs |
| `candidate_json` | signal, edge, move, spread, depth, stop/TP/horizon, features |
| `features_json` | full feature snapshot used for the prompt |
| `agent_json` | model, action, size, order type, stop/TP/horizon, rationale, hashes, tokens |
| `risk_json` | every risk check with value/limit/reason and approval |
| `execution_json` | order ID, status, price, quantity, fills, trigger |
| `rationale` | human-readable reason |
| `prompt_hash`, `response_hash`, `latency_ms` | reproducibility |

### `llm_calls`

Prompt messages, raw response, model, latency, tokens, success/error, kind
(decision/retry/reflection/regime/session) and decision ID. This is the source of
truth for what DeepSeek actually saw.

### `orders` / `fills`

Client and exchange IDs, side, type, price, quantity, status, filled quantity, average
price, fees, raw response, decision ID, liquidity (maker/taker).

### `agent_notes`

Post-trade and session lessons: outcome, note, lesson, tags, importance, PnL,
confidence calibration, suggested adjustment, model, metadata.

### `risk_events` / `system_events`

Kill switch, vetoes, stale data, reconciliation mismatch, API errors, heartbeats and
health snapshots.

## Hash and replay

- `prompt_hash = sha256(canonical prompt payload)`.
- `response_hash = sha256(canonical AgentDecision JSON)`.
- The decision cache key includes model, temperature, prompt version and full prompt.
- `reply`/`replay` reconstructs portfolio accounting from fills and checks:
  decision IDs unique, orders/fills linked to decisions, agent response hashes match,
  realized PnL and fees reconcile to the latest equity snapshot.
- The replay report is written to `artifacts/replay/<run_id>/`.

## Export

```bash
curl -u admin:$DASHBOARD_PASSWORD \
  http://127.0.0.1:8080/api/export/decisions.jsonl > decisions.jsonl
```

or use the dashboard link. Each line is one decision with nested candidate/agent/risk/
execution objects.

## Example: why did ARGUS buy BTC?

1. A completed 4h bar closed with close > EMA48 > EMA240 and positive 24h momentum.
2. The swing engine scored the trend, computed ATR/expected move and net edge after
   measured fees, and emitted a candidate.
3. DeepSeek saw the candidate, regime memo, portfolio, risk limits, other candidates
   and its journal; it returned BUY, confidence 0.74, size 0.7, MARKETABLE_LIMIT,
   stop 1000 bps, TP 2500 bps, horizon 20 days.
4. The risk kernel approved 0.00000252 BTC at 22,298,020,809 TMN (≈56,197 TMN) with
   the checks recorded.
5. The ShadowBroker filled at 22,280,200,531 with a 168 TMN taker fee.
6. The position is monitored by the 10% stop, 25% target, 8% trailing stop and 20-day
   time stop. At shutdown the position snapshot and equity are stored.

Every number above is in `decisions`, `orders`, `fills`, `positions` and `equity`, and
in the shadow artifact `decisions.jsonl`.

## Queries

```sql
-- decisions by symbol
SELECT ts, action, confidence, signal, rationale
FROM decisions WHERE run_id = :run AND symbol = 'BTCTMN'
ORDER BY ts DESC LIMIT 20;

-- fills and fees
SELECT ts, symbol, side, price, quantity, fee
FROM fills WHERE run_id = :run ORDER BY ts;

-- risk vetoes
SELECT ts, code, message, evidence_json
FROM risk_events WHERE run_id = :run ORDER BY ts DESC;

-- agent lessons
SELECT ts, symbol, outcome, lesson, importance
FROM agent_notes WHERE run_id = :run ORDER BY importance DESC;
```

## Integrity notes

- The log is append-oriented; decisions/orders/fills are not edited by the engine.
- `replay` verifies agent response hashes from the stored fields and reconstructs
  accounting. It does not re-call DeepSeek; that is a separate AI review/ablation.
- If the process is killed, a run may remain `running`; the next start marks it
  `interrupted`.
- The file journal (`artifacts/agent_journal.md`) is append-only and duplicated in the
  shadow artifact at shutdown.
