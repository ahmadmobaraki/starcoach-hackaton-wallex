# Results

All numbers below were produced in this workspace on 2026-10-22/23 against the real
Wallex and DeepSeek APIs where applicable. A lightweight committed snapshot is in `results/`; full artifacts are under `artifacts/`.

## 1. Live connectivity and account preflight

Source: `artifacts/preflight/*.json`.

| Check | Result |
|---|---|
| Wallex markets | 469 markets; OK |
| BTCTMN depth | bid/ask OK; spread ~2–13 bps |
| BTCTMN trades | 50 recent trades returned |
| 1m UDF history | 1,441 candles/day; OK |
| Private balances | key valid; prop sub-account TMN = 5,000,000 |
| Account fees | BTCTMN 0.25% maker / 0.30% taker |
| Open orders | 0 |
| Clock skew | ~0.019 s |
| WebSocket | connected; depth/candle pushes; transient 503 reconnects handled |
| Wallex MCP | initialized; 52 tools; tools/list OK |
| DeepSeek | JSON reply from `deepseek-flash`; ~1.9 s |

The prop sub-account is funded with 5,000,000 TMN; after switching to its API key,
live preflight passes (`api_key_matches_sub_account=true`, 0 open orders). The first
attempt used the main key and was rejected (0.39665 TMN); that incident and the 24-minute
second run with no signal are documented below.

## 2. Deterministic swing backtest (headline)

Config: 8 symbols, 2026-07-01 → 2026-10-21, 1m data, 4h swing, 4 positions, 3% heat,
0.75% risk/trade, 10% stop, 25% TP, 20-day horizon, 8% trailing, measured fees,
8 bps slippage. Artifact: `artifacts/backtests/bt_20261022_110405_3425ea/`.

| Metric | Value |
|---|---:|
| Net return | **+15.79%** |
| Max drawdown | 5.39% |
| Trades | 38 |
| Win rate | 68.4% |
| Profit factor | 3.83 |
| Sharpe / Sortino | 3.73 / 3.90 |
| Calmar | 2.93 |
| Fees | 91,749 TMN |
| Turnover | 6.12× |
| Avg holding | ~12.5 days |
| PnL without best trade | +694,165 TMN |
| Positive symbols | 8 / 8 |

Per-symbol net PnL (TMN): ETH +219,323; XAUT +162,051; SOL +153,737; BTC +84,484;
DOGE +77,924; XRP +43,478; PEPE +35,901; USDT +12,829.

## 2b. 12-symbol opportunity surface

12 symbols (adding BNB, TRX, LTC, ZEC): **+16.46%**, 4.69% max drawdown, 39 trades
(artifact `results/backtest_12sym/`). This is the live `.env` universe.

## 2c. Signal timeframe variants

| Timeframe | Net | Max DD | Trades |
|---|---:|---:|---:|
| 1h | +15.34% | 3.77% | 35 |
| 2h | +9.60% | 5.19% | 42 |
| 4h (default) | +15.79% | 5.39% | 38 |

1h and 2h are configurable via `SWING_TIMEFRAME_HOURS`; EMA counts remain 48/240.

## 2d. 1m/2m/5m intraday tests (25% sleeve)

| Bar | Net | Max DD | Trades | Fees |
|---|---:|---:|---:|---:|
| 1m | −1.39% | 1.43% | 113 | 127,253 TMN |
| 2m | −1.55% | 1.68% | 103 | 115,942 TMN |
| 5m | −2.77% | 2.86% | 115 | 129,258 TMN |

These use `STRATEGY_MODE=intraday` and resampled bars. They are not viable after fees.

## 3. Failed intraday baseline (honest negative)

Original 1m mean-reversion/momentum engine, 4 symbols, same 113 days:

| Metric | Value |
|---|---:|
| Net return | −3.94% |
| Max drawdown | 4.12% |
| Trades | 48 |
| Win rate | 18.8% |
| Profit factor | 0.278 |
| Fees | 213,930 TMN |

The intraday edge was smaller than the 55–90 bps round-trip cost. It is kept in the
code and documentation but disabled by default.

## 4. Robustness stress

### 2× fees

| Metric | Value |
|---|---:|
| Net return | +9.36% |
| Max drawdown | 5.53% |
| Trades | 37 |
| Win rate | 64.9% |
| Profit factor | 2.57 |
| Sharpe | 2.37 |
| Fees | 163,081 TMN |

### 2× slippage (16 bps)

| Metric | Value |
|---|---:|
| Net return | +15.23% |
| Max drawdown | 5.34% |
| Trades | 37 |
| Win rate | 62.2% |
| Profit factor | 3.15 |
| Sharpe | 3.53 |

Both stress runs remain positive, so the edge is not dependent on the exact fee or
slippage assumption.

### Parameter plateau

See `docs/PARAMETERS.md`: positive results across EMA 48/120, slow 240/480, stop
6–10%, horizon 10–20 days, and 2–3 position variants. This is a plateau, not a single
peak.

## 5. AI-gated ablation

Offline DeepSeek review of historical candidates (same features, no outcome knowledge):

- 33-candidate review with the first late-entry guard: 17 BUY / 16 HOLD; vetoes were
  mostly >2500 bps above the 40d EMA and short-term regime conflicts.
- Final 41-candidate review after the guard was tuned to 4000 bps and min size 0.7:
  **39 BUY / 2 HOLD**. The vetoed entries were XRP +4673 bps and PEPE +4896 bps above
  the 40d EMA (extreme late entries). The AI chose 0.7–1.0 size and mostly
  MARKETABLE_LIMIT.
- AI-gated backtest (`bt_20261022_111726_3881d7e6`): **+12.52% net, 4.34% max drawdown,
  34 trades, 67.6% win rate, PF 4.14, Sharpe 4.04, Calmar 2.89**. All symbols positive.
  This is lower return but lower risk than the deterministic baseline, and the trade
  set differs because of the AI vetoes and size choices.

The AI is not cosmetic: it changed the trade set and size in both reviews. Vetoed
late/extended entries were excluded; the AI-gated variant has lower return and much
lower drawdown in the first review, which is the expected risk/return trade-off.

## 6. Shadow mode

Source: `artifacts/shadow/`.

- Preflight passed against real REST/WS/MCP/DeepSeek.
- Final ~110s swing shadow run (`shadow_20261022_120816_2oxj`): 8 symbols; 3 AI
  decisions (USDTTMN, BTCTMN, XAUTTMN); AI chose 0.75 size and MARKETABLE_LIMIT for
  USDTTMN, HOLD for BTC/XAUT; one paper fill of 1.03 USDTTMN (~278,669 TMN); no real
  orders; 10% stop / 25% target. Earlier runs filled PEPE/BTC and cancelled a passive
  USDT order after reprices.
- Deterministic self-test: paper BTC position opened and closed; fees/PnL reconciled;
  equity 4,997,467 TMN; test DB under `data/selftest.db`.
- Reflection self-test: DeepSeek v4-pro wrote a real lesson about instant exits and
  the 2× cost margin; stored in `artifacts/selftest/selftest_journal.md`.
- Agent test: valid BUY decision on a synthetic candidate, confidence 0.78, 4.5s.

See `docs/SHADOW_TESTING.md` for the full transcript summary and acceptance checklist.

A live run on 2026-10-23 (06:26–07:17) filled PEPE and BNB; base-asset fees caused a
reconciliation kill and rejected gross-quantity exits. The positions were sold at 07:32;
the follow-up flatten found only unsellable dust and sent zero orders. Cash is
4,996,524 TMN, no open orders. The fee/quantity/metadata bugs are fixed. Details in
`report.md` §7.5 and `results/live_incident_20261023.md`.

A first live attempt (13:12–14:06) started correctly but the order was rejected because
the first attempt used the main-account key; the prop sub-account held the 5M. The
order endpoint saw the main balance (0.39665 TMN, error code 1006) and rejected it. A
transient reconciliation check then killed the engine; no fill and no position. After
the prop key was installed, live preflight passes; a second 24-minute run had no
candidate because the 4h trend filter was bearish/range. The `deque` WebSocket storm and
the single-check reconciliation kill were also fixed.

## 7. Reproducibility

```bash
make install
make preflight
make self-test
make download
make backtest
make shadow
make dashboard
```

- Backtest artifacts include config, data manifest, code SHA, equity, trades and
  decisions.
- `replay` reconstructs accounting and verifies hashes.
- Docker Compose runs PostgreSQL + trader + dashboard (+ optional Caddy).
- Unit tests: 59 passed, 1 live test skipped by default.

## 8. What these results do and do not prove

Proven: the plumbing works against the real APIs; the risk and audit trail are real;
the swing strategy has a positive, diversified backtest after measured fees; the AI
meaningfully vetoes and sizes trades; shadow mode executes the same path without real
orders.

Not proven: future returns; performance in a bear/chop regime; L2 fill quality; that
a 3-day challenge window will capture a multi-day trend. The system is designed to
stop trading when the evidence or the market health deteriorates, not to force trades.
