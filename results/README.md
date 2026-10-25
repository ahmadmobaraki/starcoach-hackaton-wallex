# Results snapshot

Lightweight, committed evidence from the 2026-10-22 workspace. Full run artifacts
remain under `artifacts/` (large, gitignored).

| Folder | What it is | Net | Max DD | Trades |
|---|---|---:|---:|---:|
| `backtest_deterministic/` | 8-symbol swing baseline, 113 days | +15.79% | 5.39% | 38 |
| `backtest_ai/` | cached DeepSeek-gated variant | +12.52% | 4.34% | 34 |
| `backtest_12sym/` | 12-symbol opportunity expansion | +16.46% | 4.69% | 39 |
| `backtest_1h_timeframe/` | `SWING_TIMEFRAME_HOURS=1`, 2d/10d EMA | +15.34% | 3.77% | 35 |
| `backtest_2h_timeframe/` | `SWING_TIMEFRAME_HOURS=2`, 4d/20d EMA | +9.60% | 5.19% | 42 |
| `backtest_1m_intraday/` | `STRATEGY_MODE=intraday`, 1m bars | −1.39% | 1.43% | 113 |
| `backtest_2m_intraday/` | 2m bars | −1.55% | 1.68% | 103 |
| `backtest_5m_intraday/` | 5m bars | −2.77% | 2.86% | 115 |
| `stress_2x_fees/` | 2× fee stress | +9.36% | 5.53% | 37 |
| `stress_2x_slippage/` | 16 bps slippage stress | +15.23% | 5.34% | 37 |
| `shadow/` | final 110s shadow run (live data, no real orders) | — | — | 1 paper fill |
| `intraday_shadow/` | 1m/2m/5m live-data shadow samples | — | — | 0 (no candidates) |
| `live_incident_20261023.md` | real live run: two buys, base-fee/exit bug, sold; dust remains | — | — | 2 buys, 0 sells |
| `preflight.json` / `preflight_readonly_5m.json` / `preflight_main_key_mismatch.json` / `preflight_shadow.json` | current passing live preflight, read-only 5M proof, historical main-key rejection, shadow preflight | — | — | — |
| `selftest.json` | deterministic shadow OMS/risk test | — | — | 1 paper round trip |
| `ai_review.json` / `ai_cache.jsonl` | DeepSeek review of 41 historical candidates (39 BUY, 2 HOLD) | — | — | — |

The committed backtest rows were generated with the historical kernel gate
`CANDIDATE_THRESHOLD=0.65`. On 2026-10-24 the TREND gate was aligned with
`SWING_MIN_SCORE=0.60`; the current live shape (1h, 11 symbols) measures +20.21% /
5.40% DD / 43 trades base, +17.26% / 6.04% / 37 under 2× fees, and +20.53% / 5.43% /
42 under 2× slippage. The 2× fee drawdown is at the live 6% kill, so the older
0.65-gate snapshot remains the conservative headline. Full artifacts:
`docs/RESULTS.md` §2e and `artifacts/backtests/bt_20261024_*`.

Each backtest folder has `metrics.json`, `trades.csv`, `report.html` and the redacted
config. Open `backtest_deterministic/report.html` in a browser for the full equity
curve, drawdown, per-symbol PnL and trade list. The report is standalone (Chart.js is
copied next to it).

Key commands to reproduce:

```bash
make install
make download
make backtest
make shadow
make dashboard
```

The live account had a 0 TMN balance at test time, so only public/read-only private
checks and shadow trading were possible. No live order was sent.
