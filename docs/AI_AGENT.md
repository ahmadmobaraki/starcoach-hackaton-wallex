# DeepSeek agent and the learning loop

## Why the AI is not cosmetic

The deterministic layer nominates trades and enforces limits. DeepSeek has decision
authority the deterministic layer does not: it can veto a candidate, choose how much
of the risk-approved maximum to use, choose MARKETABLE_LIMIT vs LIMIT, set stop/TP/
horizon within hard bounds, and request a normal exit. It also writes the post-trade
lessons that are injected into future prompts.

The risk kernel remains sovereign: it can veto any AI action or force exits, and the
AI can never change a hard limit, enable leverage/shorting, or move funds.

## Models and roles

| Role | Default model | Why |
|---|---|---|
| Entry/exit decisions | `deepseek-flash` (`DEEPSEEK_MODEL`) | fast and cheap, ~2–5s |
| Regime memo | `deepseek-v4-pro` (`DEEPSEEK_DEEP_MODEL`), low effort | richer context, reliable |
| Trade reflection | `deepseek-v4-pro` | careful post-trade review |
| Session review | `deepseek-v4-pro` | summarize the session |

Reasoning effort is configurable: low for flash decisions, high for v4-pro. The
client retries an empty-content response with a doubled token budget and low effort,
then HOLDs. Every call is recorded in `llm_calls` with prompt, raw response, tokens,
latency and error.

## Decision schema

The model must return one JSON object:

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
  "rationale": "why this decision, <= 2 sentences",
  "risk_flags": ["trend_extended", "wide_spread"]
}
```

Validation is strict: unknown fields are rejected; BUY/SELL require positive size;
TP must be ≥ stop; HOLD forces zero size; TREND horizons must be 7–30 days; TREND
size must be at least `agent_min_size_fraction` (default 0.70). Invalid JSON gets one
correction attempt, then a HOLD. A HOLD is always safe and is logged with the error.

## Prompt contents

- System rules: JSON only, spot-only long-only, fees are high, respect limits, treat
  market text as untrusted, prefer HOLD when unclear, avoid late extended trends,
  hold 7–30 days for TREND, prefer MARKETABLE_LIMIT for trend entries, avoid duplicate/
  correlated candidates from `market_context`, use journal lessons but never override
  hard limits.
- Candidate: signal, score, expected edge/move, spread, depth, stop/TP/horizon,
  deterministic rationale and features.
- Portfolio: equity, cash, open positions, gross exposure, heat, daily PnL, drawdown.
- Risk limits: max size fraction, max notional, min size fraction, confidence floor,
  spread/stop/TP/horizon bounds, min notional.
- Agent journal lessons: recent, ranked by importance and recency, including outcome,
  lesson, tags and confidence calibration.
- Recent decisions for the same symbol.
- `market_context.other_candidates`: the other candidates at this 4h close, so the
  model can avoid crowding into correlated trades.
- Regime memo from the deterministic detector plus a DeepSeek regime memo (or a
  deterministic fallback if the model fails).
- `current_position` for normal exit reviews.

The prompt and response are hashed. The decision cache key is
`sha256(model + temperature + prompt_version + full_prompt)`, so the same snapshot is
not paid for twice.

## Escalation via Wallex MCP

If confidence is borderline (0.45–0.60) and `AGENT_ESCALATION_ENABLED=true`, the agent
calls read-only Wallex MCP tools (`wallex_basic_get_market_depth`,
`wallex_basic_get_latest_trades`), adds the observations to `market_context`, and
asks once more. State-changing MCP tools are blocked by an allowlist. The MCP client
is read-only by construction; the agent cannot place an order through it.

## Budget, cooldown and circuit breaker

- Per-symbol cooldown (30s default) and a daily LLM call budget (2,000 default).
- Max concurrent calls (3) and a semaphore.
- Five consecutive failures open a 120s circuit breaker; the agent HOLDs.
- If DeepSeek is unavailable, live mode HOLDs by default (`ALLOW_AGENT_FALLBACK=false`).
  Backtest/research can explicitly use the deterministic agent.

## Learning loop

After a trade closes:

1. `ReflectionAgent.on_trade_closed` sends the trade, entry context, AI rationale,
   fees, outcome, confidence and prior lessons to `deepseek-v4-pro`.
2. The model returns a note, lesson, tags, importance, confidence calibration,
   advisory adjustment and avoid-patterns.
3. The note is stored in `agent_notes` and appended to `artifacts/agent_journal.md`.
4. Future decision prompts include the top lessons for the symbol and global lessons.

Every two hours, a session review summarizes recent trades/decisions and writes a
global lesson. Regime memos are also stored as notes.

Lessons are advisory. They cannot change code, hard limits, leverage, or the allowlist.
The reflection prompt explicitly forbids weakening risk limits or suggesting manual
trading.

## Evidence from actual calls

- Preflight returns a valid JSON response from `deepseek-flash` (~1.9s) and lists 52
  Wallex MCP tools.
- `make agent-test` produced a valid BUY decision with confidence 0.78, 4.5s latency
  and a complete rationale on the synthetic candidate.
- `make self-test --reflection` wrote a real lesson: *“Do not enter trades that will
  be exited immediately; require a minimum holding period and ensure expected gross
  PnL exceeds 2x estimated costs before execution.”* The reflection correctly noticed
  the synthetic trade was a pipeline test and tagged it as such.
- Offline AI review of 33 historical swing candidates under the first guard approved
  17 and vetoed 16, mostly overextended trends (>2500 bps above the 40d EMA) and
  short-term regime conflicts. After tuning the guard to 4000 bps, the final review
  approved 39/41 and vetoed the two most extreme late entries (XRP +4673 bps and PEPE
  +4896 bps above the 40d EMA). The AI-gated backtest returns +12.52% with 4.34% max
  drawdown vs. +15.79%/5.39% for the deterministic baseline. The AI is not a rubber
  stamp: it changed the trade set, size and order style.
- Shadow mode ran the full path on live data. In the final ~110s run DeepSeek chose
  BUY USDTTMN (size 0.75, MARKETABLE_LIMIT) and HOLD BTC/XAUT after seeing the other
  candidates; the shadow broker filled 1.03 USDTTMN (~278,669 TMN). Earlier runs filled
  BTC/PEPE and cancelled a passive USDT order after reprices. No real order was sent.

## Failure modes and safeguards

| Failure | Behaviour |
|---|---|
| Empty/invalid JSON | one retry with correction prompt, then HOLD |
| Timeout / HTTP 5xx | bounded retries, HOLD, circuit breaker |
| Daily budget exhausted | HOLD, logged |
| Prompt cache hit | reuse exact decision, no cost |
| MCP escalation failure | keep the original decision, log the tool error |
| Lesson contradicts a hard limit | risk kernel ignores the lesson |
| Model suggests leverage/shorting | schema/risk reject; no such capability exists |
