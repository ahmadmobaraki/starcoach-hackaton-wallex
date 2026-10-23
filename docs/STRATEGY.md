# Strategy logic

## The cost constraint comes first

The challenge account's `GET /v1/account/fee` returns **0.25% maker / 0.30% taker**
on TMN markets (measured 2026-10-22). A taker round trip costs **60 bps**, plus the
spread and slippage. Any strategy that targets 20–50 bps moves is structurally
unprofitable. ARGUS therefore defaults to a **slow trend-following strategy** with
multi-day targets.

The original plan's intraday mean-reversion/momentum engine is still in the code
(`STRATEGY_MODE=intraday`) but is disabled by default. On 113 days and four symbols
it lost **−3.94%** net over 48 trades after fees; that result is kept in the repo as
evidence rather than hidden. The default `STRATEGY_MODE=swing` was chosen because it
survived the same cost model.

## Universe

Allowlist (default): `BTCTMN, ETHTMN, SOLTMN, USDTTMN, XRPTMN, DOGETMN, PEPETMN,
XAUTTMN`. Additional liquid symbols can be added with `UNIVERSE_ALLOWLIST`.

Selection checks:

- listed on Wallex, spot, TMN-quoted;
- not flagged risky, not a new listing (≥14 days);
- 24h quote volume ≥ 5,000,000,000 TMN;
- valid price/precision.

Book spread and depth are checked per order by the risk kernel, not permanently
excluded, so a temporarily wide market does not remove a symbol for the whole hour.

## Slow trend signal (primary)

Inputs are **completed bars of `SWING_TIMEFRAME_HOURS`** (default 4), built by resampling
1-hour Wallex UDF candles. The timeframe is configurable in `.env`:

```dotenv
SWING_TIMEFRAME_HOURS=4   # 1, 2, 4, ... 24
SWING_FAST_EMA=48
SWING_SLOW_EMA=240
```

The EMA values are **bar counts**. At 4h, 48/240 = 8d/40d. At 1h, 48/240 = 2d/10d,
which gives hourly decision points; backtest +15.34% / 3.77% DD / 35 trades. At 2h,
48/240 = 4d/20d; +9.60% / 5.19% / 42. A daily timeframe needs roughly 300 days of
history and is not validated on the current 150-day sample.

Let:

- `C` = latest 4h close;
- `F` = EMA(48) on 4h closes = **8-day EMA**;
- `S` = EMA(240) on 4h closes = **40-day EMA**;
- `R24` = `C / C[-6] − 1` = 24-hour momentum (6 × 4h);
- `ATR` = mean true range over 14 × 4h bars, in bps.

Entry condition:

```
C > F > S          (established slow uptrend)
R24 > 0            (positive 24h momentum)
```

Score:

```
score = 0.55
      + 0.15 * min(1, (C/S − 1) / 10%)
      + 0.15 * min(1, (F/S − 1) / 5%)
      + 0.15 * min(1, R24 / 10%)
```

The score is capped at 0.95 and must be ≥ 0.60.

Expected move and cost hurdle:

```
expected_move = max(3.0 * ATR, 1.5 * R24 * 10000, 300 bps), capped at 3500 bps
cost          = spread + 2 * taker_fee * 10000 + 8 bps + 12 bps buffer
net_edge      = expected_move − cost
```

A candidate is emitted only if `net_edge ≥ ALPHA_THRESHOLD_BPS` (default 100 bps)
and depth is at least 5× the maximum order notional.

Recommended risk parameters:

```
stop_loss   = clamp(max(1000 bps, 1.2 * ATR), 300, 1200 bps)
take_profit = clamp(max(2500 bps, 2.5 * stop), 600, 3000 bps)
horizon     = 20 days
trailing    = arm after +800 bps, trail 800 bps below the high-water mark
```

Why these values:

- The trend is slow; a 10% stop survives normal 4h noise. The 25% target and 8%
  trailing stop let winners run while protecting gains.
- The 20-day horizon matches the typical duration of a crypto trend leg and gives the
  edge time to exceed the 55–90 bps round-trip cost.
- The 8% trailing stop converts a large part of an extended trend into realized profit
  without requiring a precise top.
- Position size is risk-based, not all-in; see `docs/RISK.md`.

The AI sees the candidate, the regime memo, open positions, other candidates at the
same 4h close, recent decisions and its own journal. It can approve or veto, choose
size (≥70% of the risk-approved maximum for TREND), choose MARKETABLE_LIMIT vs LIMIT,
and adjust stop/TP/horizon within the hard bounds. A late-entry guard asks it to HOLD
when price is >4000 bps above the 40d EMA or 24h momentum is >40%, unless the trend
is exceptionally strong.

## Exits

Deterministic exits (checked every second in live and every 1m bar in backtest):

1. hard stop at `stop_price` (10% initial);
2. take profit at `take_profit_price` (25%);
3. trailing stop after +8%: stop moves to `high_water − 8%`;
4. time stop after 20 days;
5. challenge endgame: flatten 30 minutes before `CHALLENGE_END`;
6. kill switch: cancel all, flatten if configured;
7. optional AI normal exit (disabled for TREND by default, because the slow trend is
   managed by rules; intraday positions can be reviewed by the AI).

When both stop and target are inside the same bar, the backtest assumes the stop was
hit first. Trailing stops use the prior bar's high-water mark, never the current bar's
high before testing its low. This is intentionally conservative.

## Why not intraday?

The legacy engine computes 1m returns, EMA9/21/50, RSI14, Bollinger z, ATR, VWAP,
volume z, book imbalance, aggressive trade flow, BTC/ETH cross returns and a regime
label. It emits MR when `z ≤ −2.2`, RSI ≤ 38 and price is below VWAP, or MOM when
5m/15m/1h returns align with volume and book confirmation. The logic is sound, but
with 55 bps round-trip fees the edge is too small:

- 48 trades, 18.8% win rate, −3.94% net, 4.12% max drawdown;
- most exits were time stops after 49 minutes with gross moves under 50 bps;
- fees were 214k TMN on 5M capital, larger than the gross PnL.

This is exactly the kind of honest negative result the plan asked for. The code is
kept for research and can be enabled with `STRATEGY_MODE=intraday`; it is not the
default.

## Activity versus quality

The slow strategy trades about 0.3–0.4 times per day on eight symbols (38–42 trades
in 113 days). That is lower than an intraday target of 3–10 trades/day, but each trade
has a multi-day horizon and pays 55–90 bps in costs. Forcing activity with a negative-
expectancy scalper would trade 15 activity points for a high probability of losing
the 90 profit points and the 30 loss-control points. The default is therefore quality
over quantity.

The live `.env` expands the universe to **12** symbols (BNB, TRX, LTC, ZEC added),
tested at **+16.46% / 4.69% DD / 39 trades**. `STRATEGY_MODE=both` with
`INTRADAY_RISK_MULTIPLIER=0.25` is an optional activity sleeve; the 1h/15m/range/breakout
variants were all negative after fees, so it is not the default. A 10-day swing variant
(52 trades, +11.82%, 4.45% DD) is available by lowering `SWING_HORIZON_SEC`, but 20 days
has better return for the same risk.

## Worked example (backtest)

A BTC 4h bar closes with `C` 720 bps above the 40d EMA, `F/S` positive, `R24` +390 bps,
ATR 180 bps. Expected move = max(3×180, 1.5×390, 300) = 585 bps; cost ≈ 87 bps; net
edge ≈ 498 bps. The risk kernel sizes the position to 0.75% equity risk at a 10% stop
(≈375,000 TMN), the AI approves at 0.75 confidence with 0.7–0.8 size and MARKETABLE_LIMIT,
and the position is managed by the 10% hard stop, 25% target and 8% trailing stop.
