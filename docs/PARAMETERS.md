# Parameters and robustness

## Where the numbers come from

- **Fees:** measured from `GET /v1/account/fee` with the challenge key. TMN markets:
  0.25% maker, 0.30% taker. Used as hard execution costs, not assumptions.
- **Spread/slippage:** live BTC spread is 2–13 bps; other TMN markets 0.5–25 bps. The
  backtest charges 8 bps slippage and a synthetic spread; the report states this.
- **Risk per trade:** 0.75% of equity. With a 10% stop this sizes a trade near 7.5%
  of equity, leaving room for four positions under 3% portfolio heat.
- **Alpha threshold:** 100 bps net edge after fees, spread, 8 bps slippage and a 12 bps
  buffer. With 55–60 bps round-trip fees, anything below this is noise.
- **Stop/TP/horizon:** 1000/2500 bps and 20 days, selected on a parameter plateau and
  consistent with the multi-day trend thesis. A 10% stop survives 4h noise; a 25%
  target is 2.5R; 8% trailing protects gains after +8%.
- **Positions/heat:** four positions at 3% heat. Tested variants below.
- **TREND size floor:** 70% of the risk-approved maximum. The AI can choose 0.7–1.0;
  token sizes are rejected because they cannot clear the exchange minimum or fees.

## Signal timeframe variants (8 symbols, 113 days)

EMA counts stay 48/240; the timeframe changes what a bar means.

| `SWING_TIMEFRAME_HOURS` | Net | Max DD | Trades |
|---:|---:|---:|---:|
| 1 | +15.34% | 3.77% | 35 |
| 2 | +9.60% | 5.19% | 42 |
| 4 (default) | +15.79% | 5.39% | 38 |

`24` is accepted but requires ~300 days of history; the current sample is 150 days, so it is not validated. Do not change the timeframe and the EMA counts at the same time without a new backtest.

## Tested portfolio variants (8 symbols, 113 days, final data)

All runs use the same signals, fees, slippage and conservative trailing assumption.

| Positions | Heat | Risk/trade | Net return | Max DD | Trades | Win rate | PF | Sharpe |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 2.5% | 0.75% | +9.61% | 2.91% | 27 | 77.8% | 16.4 | 3.50 |
| **4** | **3.0%** | **0.75%** | **+15.79%** | **5.39%** | **38** | **68.4%** | **3.83** | **3.73** |
| 5 | 3.75% | 0.75% | +25.23% | 6.36% | 47 | 74.5% | 5.54 | 5.68 |
| 4 | 4.0% | 1.00% | +25.57% | 7.65% | 42 | 69.0% | 4.56 | 4.50 |

The 5-position variant has the highest return but 6.36% drawdown exceeds the 6% kill
threshold; the 1% risk variant reaches 7.65%. The chosen 4-position/3% point is the
best return that stays under the hard drawdown cap with a margin. The 3-position
variant is the conservative fallback if loss control is weighted more heavily.

## 4h swing parameter plateau (portfolio prototype, before AI)

Representative results on the same data (fixed 750k notional, no AI):

| Fast/slow EMA | Trail | Stop | Horizon | Positions | Net | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| 48/240 | 8% | 10% | 20d | 3 | +29.1% | 4.34% |
| 120/240 | 8% | 10% | 20d | 3 | +26.8% | 4.75% |
| 48/480 | 8% | 10% | 20d | 3 | +26.7% | 4.47% |
| 48/240 | 8% | 10% | 10d | 3 | +25.2% | 4.60% |
| 48/240 | 8% | 6% | 20d | 3 | +26.3% | 4.88% |
| 48/240 | 8% | 10% | 20d | 2 (ETH/SOL) | +19.2% | 3.13% |

The neighborhood is positive across EMA, stop, horizon and symbol subsets, which is
why the point is not considered a single-cell overfit. A 12-symbol run adds BNB, TRX,
LTC and ZEC and returns **+16.46% / 4.69% DD / 39 trades**; it is the live `.env`
universe for more opportunity surface. EMA-cross exits were tested and
removed: they produced 76 trades and −13% due to whipsaw. Trailing-only exits were
materially better.

## Why not a faster trend/pullback?

A 4h pullback-in-uptrend prototype (RSI < 40–60, target 3–5%, stop 3–4%) produced
146–238 trades and −9% to −14% after fees. The fee drag is too large. The slow trend
needs large winners to pay for many small losses; a faster version does not have them.

## AI-gated variant

DeepSeek reviews historical candidates with the same features, no outcome knowledge.
The final review approved 39/41 and vetoed two extreme late entries (+4673 and +4896
bps above the 40d EMA); it also chose 0.7–1.0 size and MARKETABLE_LIMIT. The AI-gated
backtest returns +12.52% with 4.34% max drawdown vs. +15.79%/5.39% for the deterministic
baseline. The AI is not allowed to weaken limits.

The AI is not allowed to weaken limits. Its choices are bounded by the risk kernel.
The ablation answers a concrete question: does the agent's veto/size add value relative
to trading every risk-approved candidate?

## How to run the study

```bash
python -m wallex_trader study \
  --symbols BTCTMN,ETHTMN,SOLTMN,USDTTMN \
  --start 2026-07-01 --end 2026-10-21 \
  --alpha 60,80,100 --stop 800,1000 --tp 2000,2500 --z 2.0,2.4
```

The grid is deliberately small. The objective is a stable plateau with positive
out-of-sample folds, max drawdown ≤ 5%, profit factor ≥ 1.3 after 2× costs, not the
highest single number. `study.md` and `study.html` are written under
`artifacts/parameter_study/`.

## Values that must not be tuned casually

- `WALLEX_API_KEY`, `LIVE_CONFIRM`, `MODE`, `FLATTEN_ON_KILL`.
- `MIN_NOTIONAL_TMN`, precision/tick handling, self-trade prevention.
- `MAX_ORDER_NOTIONAL_TMN`, `MAX_GROSS_EXPOSURE`, `MAX_PORTFOLIO_HEAT`,
  `DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN`.
- The kill conditions and the absence of shorting/leverage.

If a parameter change improves the backtest by only a few bps but increases drawdown or
turnover, reject it. The fee and slippage stress must be re-run for any change to the
signal or exit logic.
