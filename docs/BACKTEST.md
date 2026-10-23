# Backtesting

## Data

- Source: Wallex UDF `/v1/udf/history` at 1-minute resolution.
- Window: 2026-05-24 → 2026-10-21 for indicator warm-up; evaluation 2026-07-01 →
  2026-10-21 (113 days).
- Symbols: BTCTMN, ETHTMN, SOLTMN, USDTTMN, XRPTMN, DOGETMN, PEPETMN, XAUTTMN.
- Volume: ~1.7M 1-minute rows. Raw gzipped JSON and curated Parquet are kept under
  `data/raw` and `data/curated`; the downloader writes a manifest with checksums,
  row counts, gap counts and fetch timestamps.
- Validation: duplicate timestamps, monotonicity, OHLC bounds, negative volume,
  1-minute gaps. Missing minutes are reported, not silently filled.

UDF 1m data is returned in day-aligned UTC windows, so the downloader fetches one UTC
day at a time with checkpointing and resume. A 7-day 1m request returns `no_data` on
Wallex; this is why the downloader uses daily windows.

## Simulator

The simulator is event-driven on 1-minute bars:

1. A completed 4h bar produces a signal at its close.
2. The entry is executed at the **next 1m open** (no same-bar lookahead).
3. Hard stops, trailing stops, targets and signal exits are checked against the
   current bar's high/low after entry.
4. Stop and target in the same bar → stop is assumed first.
5. Trailing stop updates use the prior bar's high-water mark, not the current high.
6. Fees: measured maker 25 bps / taker 30 bps. MARKETABLE_LIMIT entries and exits pay
   taker; passive limits pay maker.
7. Slippage: 8 bps on both sides by default, applied to the executed price.
8. L2 history is unavailable; a synthetic book uses a conservative spread and a
   liquidity proxy. The report states this limitation.
9. At the end, open positions are liquidated at the last mark with taker fees and
   slippage.

No future data is used in the 4h signal: the bar must be closed, and the entry is next
bar. The precompute test `test_features_do_not_use_future_rows` verifies that mutating
future rows does not change earlier features.

## Signal timeframe variants

Same 8 symbols and costs, changing only `SWING_TIMEFRAME_HOURS` (EMA counts stay
48/240):

| Timeframe | Net | Max DD | Trades | Note |
|---|---:|---:|---:|---|
| 1h | +15.34% | 3.77% | 35 | 2d/10d trend, hourly decisions |
| 2h | +9.60% | 5.19% | 42 | 4d/20d trend |
| 4h (default) | +15.79% | 5.39% | 38 | 8d/40d trend |
| 24h | — | — | — | needs ~300 days of history; not validated |

## Transaction costs

Measured on the challenge key: `makerFeeRate = 0.0025`, `takerFeeRate = 0.0030` for
TMN markets. A taker round trip is 60 bps; the strategy's cost hurdle includes 60 bps
fees, spread, 8 bps slippage and a 12 bps buffer. This is why the slow strategy needs
several-hundred-bps expected moves.

## Baseline result (default config)

Deterministic agent (no AI veto), 4 positions, 3% heat, 0.75% risk, 10% stop, 25% TP,
20-day horizon, 8% trailing.

| Metric | Value |
|---|---:|
| Net return | +15.79% |
| Max drawdown | 5.39% |
| Trades | 38 |
| Win rate | 68.4% |
| Profit factor | 3.83 |
| Sharpe | 3.73 |
| Sortino | 3.90 |
| Calmar | 2.93 |
| Fees | 91,749 TMN |
| Turnover | 6.12× |
| Avg holding | 12.5 days |
| PnL without best trade | +694,165 TMN |
| Symbols profitable | 8 / 8 |

Per symbol (TMN): ETH +219,323; SOL +153,737; XAUT +162,051; BTC +84,484; DOGE +77,924;
XRP +43,478; PEPE +35,901; USDT +12,829.

The earlier 4-symbol variant with 3 positions returned +16.44% with 2.41% max drawdown
and 21 trades. The 8-symbol set has more activity and diversification at a slightly
higher drawdown. A 12-symbol run (adding BNB, TRX, LTC, ZEC) returns **+16.46%** with
4.69% max drawdown and 39 trades; the live `.env` uses 12 symbols, while the headline
table remains the 8-symbol like-for-like baseline.

## Failed intraday baseline

The original 1m MR/MOM engine on 4 symbols:

- net −3.94%, max drawdown 4.12%, 48 trades, 18.8% win rate, fees 213,930 TMN;
- most exits were time stops after ~49 minutes with gross moves under 50 bps.

This is kept in `docs/STRATEGY.md` and in the repository, demonstrating that the team
tested the plan's hypothesis and changed course when costs destroyed the edge.

## Robustness checks

- **Without the best trade:** +694,165 TMN, still strongly positive.
- **Per-symbol:** all eight symbols positive.
- **Parameter plateau:** fast/slow EMA, stop, trail and horizon variants cluster around
  positive results; see `docs/PARAMETERS.md`. The chosen point is not a single spike.
- **2× fees:** +9.36% net, 5.53% max drawdown, 37 trades, PF 2.57, Sharpe 2.37 (still positive).
- **2× slippage (16 bps):** +15.23% net, 5.34% max drawdown, 37 trades, PF 3.15, Sharpe 3.53.
- **AI-gated ablation:** +12.52% net, 4.34% max drawdown, 34 trades, PF 4.14, Sharpe
  4.04. DeepSeek vetoed 2/41 late extended entries and chose 0.7–1.0 sizing; see
  `docs/RESULTS.md`.

## Reproduce

```bash
make download
make backtest
```

Artifacts:

```
artifacts/backtests/<run_id>/
  report.html       self-contained chart + metrics + trades
  metrics.json      machine-readable metrics
  equity.csv        per-minute equity/drawdown
  trades.csv        entry/exit, PnL, fees, reason
  decisions.jsonl   every candidate with AI/risk/execution context
  config.json       redacted config
  data_manifest.json
  git_sha.txt
```

Open the report with `file://` or via the dashboard's Backtests tab. The run is also
saved in PostgreSQL and appears at `/test-results/backtests`.

## Parameter study

```bash
python -m wallex_trader study \
  --symbols BTCTMN,ETHTMN,SOLTMN,USDTTMN \
  --start 2026-07-01 --end 2026-10-21 \
  --alpha 60,80,100 --stop 800,1000 --tp 2000,2500 --z 2.0,2.4
```

`study.json`, `study.md` and `study.html` are written under
`artifacts/parameter_study/`. The selection rule is a plateau with positive
out-of-sample folds, max drawdown ≤ 5%, profit factor ≥ 1.3 after 2× costs, not the
single best cell.

## Limitations

- 113 days is one regime (a strong crypto bull market). The trend strategy will
  underperform in chop/bear markets; trailing stops and de-risking are the protection.
- No historical L2/tape; the synthetic book cannot model queue position or a real
  depth shock. The 2× slippage stress is the mitigation.
- TMN is the quote currency; USD/TMN devaluation and exchange-specific liquidity can
  change the results.
- Fees are assumed constant. A fee change invalidates the cost hurdle; rerun.
- Long-only spot cannot profit from a sustained downtrend; the correct action is HOLD
  TMN, which the regime filter and AI enforce.
