# Risk and capital management

The risk kernel is deterministic and runs after the AI. It can veto an entry, reduce
size, reject an invalid stop, cancel orders, or force an exit. The AI cannot change
these limits.

## Position sizing

Given equity `E` (TMN), entry price `P`, stop distance `s` (bps), taker fee `f`:

```
risk_amount   = E * RISK_PER_TRADE                  (0.75% default)
stop_distance = P * s / 10_000
qty_risk      = risk_amount / stop_distance
qty_position  = E * MAX_POSITION_PCT / P            (20% default)
qty_liquidity = top5_depth_tmn * MAX_DEPTH_FRACTION / P   (10% of top-5 depth)
qty_order     = MAX_ORDER_NOTIONAL_TMN / P          (750,000 TMN default)
qty_cash      = available_cash / (P * (1 + fee))
qty           = floor_to_step(min(qty_risk, qty_position, qty_liquidity,
                                  qty_order, qty_cash) * ai_size_fraction)
```

Then the kernel checks:

- notional ≥ max(50,000 TMN, market minNotional);
- gross exposure after ≤ 60%;
- portfolio heat after ≤ 3%;
- correlated exposure (BTC/ETH/SOL) ≤ 35%;
- cash buffer after ≥ 25%;
- spread ≤ 50 bps and book age ≤ 5s;
- top-5 depth ≥ 5× the max order notional;
- confidence ≥ 0.55, stop/TP/horizon within bounds;
- TREND size ≥ 0.70 of the risk-approved maximum;
- estimated slippage ≤ 25 bps and price within 2% of mid;
- no opposite own order on the symbol (self-trade prevention);
- open orders ≤ 12, open positions ≤ 4;
- daily loss, drawdown, volatility, WS/API health, clock skew and endgame checks.

Because of the 10% trend stop, 0.75% risk sizes a position at roughly 7.5% of equity
(375,000 TMN on 5M). Four such positions use 30% gross exposure and 3% heat, matching
the caps. The order notional cap is also 750,000 TMN, so a single trade can never use
more than 15% of starting equity.

## Hard caps (defaults)

| Limit | Value |
|---|---:|
| Risk per trade | 0.75% of equity |
| Max position | 20% of equity |
| Max gross exposure | 60% |
| Max portfolio heat | 3% |
| Max open positions | 4 |
| Max correlated exposure | 35% |
| Min cash buffer | 25% |
| Daily loss limit | 2.5% |
| Peak drawdown halt | 6% |
| Soft de-risk drawdown | 3% (new size halved) |
| Hard de-risk drawdown | 4.5% (no new entries) |
| Max order notional | 750,000 TMN |
| Min order notional | 100,000 TMN |
| Max spread to trade | 50 bps |
| Max slippage | 25 bps |
| Max stop / TP | 1200 / 3000 bps |
| Min confidence | 0.55 |

Live config is more conservative: max order 250,000 TMN, max position 10%, risk 0.5%.

### Signal score gate

The risk pre-check uses the candidate's own generation floor, so a candidate is never
created only to be rejected by a stricter duplicate gate:

- `TREND` (slow swing): `swing_min_score` (default 0.60).
- `MR` / `MOM` (intraday sleeve): `candidate_threshold` (default 0.65).

Before 2026-10-24 every candidate was compared to `candidate_threshold`, so swing
candidates scoring 0.60–0.65 were generated and then always vetoed (`signal score
below threshold`). Aligning the TREND gate with `swing_min_score` was measured on the
1h/11-symbol sample (2026-07-01 → 2026-10-21, deterministic): +20.21% / 5.40% max DD /
43 trades vs +20.05% / 5.13% / 43 with the old 0.65 gate. Under 2× fees the result is
+17.26% / 6.04% / 37 vs +18.72% / 5.48% / 44, i.e. still strongly positive but more
drawdown-sensitive. The intraday sleeve keeps its 0.65 gate.

## Stop, target, trailing, time

- Hard stop: 1000 bps (10%) or 1.2× 4h ATR, clamped to 300–1200 bps.
- Take profit: 2500 bps (25%) or 2.5× stop, clamped to 600–3000 bps.
- Trailing: arm at +800 bps; trail 800 bps below the high-water mark.
- Time stop: 20 days (`swing_horizon_sec`), so a stalled trend does not tie up capital.
- Challenge endgame: no new entries in the last 2 hours; flatten in the last 30 minutes.

The trailing stop only uses the prior bar's high-water mark in the backtest, never the
current bar's high before testing its low. In live it updates on each mark, which is
tick-by-tick and therefore conservative.

## Kill conditions

The kill switch activates on any of:

- `data/KILL_SWITCH` file (manual, dashboard, `make kill`);
- peak drawdown ≥ 6%;
- daily loss ≤ −2.5%;
- WebSocket down ≥ 30s;
- API error rate > 10% after at least 50 calls;
- clock skew > 5s for 3 consecutive health checks (HTTP Date headers have 1 s
  granularity and can be stale on cached/proxied responses, so one sample is not
  enough); the risk event records the measured skew value;
- live reconciliation mismatch (orders or balances). Known dust below the market step
  or exchange minimum notional is subtracted; a remainder left by a partial exit is
  recorded as dust and dropped instead of retried;
- repeated agent failures / circuit breaker (agent HOLDs; the risk kernel may halt
  if the failure is systemic).

On activation:

1. no new entries;
2. all open orders are cancelled;
3. positions are flattened if `FLATTEN_ON_KILL=true`;
4. a critical `risk_events` row and a system event are written;
5. the process stays alive for inspection; restart is a deliberate act.

The kill switch is sticky. A human must clear the file and restart after inspecting
the cause.

## Execution safety

- Spot only; BUY opens, SELL only reduces an existing position.
- No leverage, no margin, no withdrawals, no manual order endpoint.
- Client order IDs are generated and persisted before sending.
- Ambiguous POST timeouts are queried by client ID before retrying (no blind duplicate).
- Orders are LIMIT at the exchange; MARKETABLE_LIMIT is a limit price capped by
  `max_slippage_bps`.
- Quantities are floored to the market step; prices are quantized to the tick.
- The agent's proposed size/stop/TP/horizon is rejected if outside bounds.
- Book staleness, depth, spread and price sanity are checked before every order.
- Live orders are capped by the canary config; an unfunded account refuses to start.

## Drawdown de-risking

At 3% drawdown, new positions are sized at 50% of the approved maximum. At 4.5%,
new entries are blocked. The 6% peak drawdown still triggers the kill switch. This
produces a layered response instead of waiting for a single cliff.

## Monitoring

The dashboard shows equity, drawdown, daily PnL, gross exposure, heat, open positions,
orders, fills, risk events, agent notes and the kill-switch state. The heartbeat is
written every 15s; Docker health checks fail if it is older than 120s. All risk
decisions include the full list of checks and values, so a veto can be explained
without guesswork.
