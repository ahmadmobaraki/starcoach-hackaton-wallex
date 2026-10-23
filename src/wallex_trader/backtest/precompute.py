from __future__ import annotations

import numpy as np
import pandas as pd

from wallex_trader.config import Settings

FEATURE_COLUMNS = [
    "price",
    "ret_1m",
    "ret_3m",
    "ret_5m",
    "ret_15m",
    "ret_30m",
    "ret_1h",
    "ret_4h",
    "ret_5m_bps",
    "ret_15m_bps",
    "ret_1h_bps",
    "ret_4h_bps",
    "ema9_dist_bps",
    "ema21_dist_bps",
    "ema50_dist_bps",
    "ema9_slope_bps",
    "ema50_slope_bps",
    "rsi_14",
    "bb_z_20",
    "atr_bps",
    "rv_1m_bps",
    "rv_5m_bps",
    "rv_15m_bps",
    "rv_percentile",
    "price_vs_vwap_bps",
    "volume_z_60",
    "range_compression",
    "spread_bps",
    "microprice_dev_bps",
    "book_imbalance_1",
    "book_imbalance_5",
    "book_imbalance_10",
    "top5_depth_tmn",
    "depth_25bps_tmn",
    "depth_50bps_tmn",
    "depth_100bps_tmn",
    "book_age_ms",
    "aggressive_buy_ratio",
    "trade_count_60s",
    "trade_notional_60s",
    "large_trade_ratio",
    "trade_intensity",
    "btc_ret_5m",
    "btc_ret_15m",
    "btc_ret_1h",
    "symbol_vs_btc_15m_bps",
    "eth_ret_5m",
    "usdtmn_ret_5m",
    "beta_btc",
    "corr_btc",
    "hour_utc",
    "weekday",
    "time_to_end_sec",
]

REGIME_COLUMNS = [
    "regime_label",
    "regime_memo",
    "volatility_percentile",
    "btc_ret_5m_bps",
    "btc_ret_15m_bps",
    "btc_ret_1h_bps",
    "btc_trend",
]


def build_feature_frames(
    frames: dict[str, pd.DataFrame], settings: Settings
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    if "BTCTMN" not in frames:
        raise ValueError("backtest requires BTCTMN candles for the regime filter")
    features: dict[str, pd.DataFrame] = {}
    btc = frames["BTCTMN"]
    btc_ret_5 = btc["close"].pct_change(5)
    btc_ret_15 = btc["close"].pct_change(15)
    btc_ret_60 = btc["close"].pct_change(60)
    eth_ret_5 = frames["ETHTMN"]["close"].pct_change(5) if "ETHTMN" in frames else None
    usdt_ret_5 = (
        frames["USDTTMN"]["close"].pct_change(5) if "USDTTMN" in frames else None
    )
    for symbol, frame in frames.items():
        features[symbol] = _symbol_features(
            frame,
            settings,
            btc_ret_5=btc_ret_5,
            btc_ret_15=btc_ret_15,
            btc_ret_60=btc_ret_60,
            eth_ret_5=eth_ret_5,
            usdt_ret_5=usdt_ret_5,
            btc_close=btc["close"],
        )
    regime = _regime_frame(btc, settings)
    return features, regime


def _symbol_features(
    frame: pd.DataFrame,
    settings: Settings,
    *,
    btc_ret_5: pd.Series,
    btc_ret_15: pd.Series,
    btc_ret_60: pd.Series,
    eth_ret_5: pd.Series | None,
    usdt_ret_5: pd.Series | None,
    btc_close: pd.Series,
) -> pd.DataFrame:
    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"]
    out = pd.DataFrame(index=frame.index)
    out["price"] = close
    ret_1 = close.pct_change(1)
    out["ret_1m"] = ret_1
    out["ret_3m"] = close.pct_change(3)
    out["ret_5m"] = close.pct_change(5)
    out["ret_15m"] = close.pct_change(15)
    out["ret_30m"] = close.pct_change(30)
    out["ret_1h"] = close.pct_change(60)
    out["ret_4h"] = close.pct_change(240)
    out["ret_5m_bps"] = out["ret_5m"] * 10_000
    out["ret_15m_bps"] = out["ret_15m"] * 10_000
    out["ret_1h_bps"] = out["ret_1h"] * 10_000
    out["ret_4h_bps"] = out["ret_4h"] * 10_000

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    out["ema9_dist_bps"] = (close / ema9 - 1) * 10_000
    out["ema21_dist_bps"] = (close / ema21 - 1) * 10_000
    out["ema50_dist_bps"] = (close / ema50 - 1) * 10_000
    out["ema9_slope_bps"] = ema9.diff() / close * 10_000
    out["ema50_slope_bps"] = ema50.diff() / close * 10_000

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi_14"] = (100 - 100 / (1 + rs)).fillna(50)

    mean20 = close.rolling(20).mean()
    std20 = close.rolling(20).std(ddof=0)
    out["bb_z_20"] = ((close - mean20) / std20.replace(0, np.nan)).fillna(0)

    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False).mean()
    out["atr_bps"] = atr / close * 10_000
    out["rv_1m_bps"] = ret_1.rolling(30).std(ddof=0) * 10_000
    out["rv_5m_bps"] = ret_1.rolling(5).std(ddof=0) * 10_000
    out["rv_15m_bps"] = ret_1.rolling(15).std(ddof=0) * 10_000
    out["rv_percentile"] = (
        out["rv_1m_bps"].rolling(240, min_periods=60).rank(pct=True) * 100
    ).fillna(50)

    typical = (high + low + close) / 3
    volume_sum = volume.rolling(60).sum()
    vwap = (typical * volume).rolling(60).sum() / volume_sum.replace(0, np.nan)
    out["price_vs_vwap_bps"] = (close / vwap - 1) * 10_000

    vol_mean = volume.rolling(60).mean()
    vol_std = volume.rolling(60).std(ddof=0)
    out["volume_z_60"] = ((volume - vol_mean) / vol_std.replace(0, np.nan)).fillna(0)
    out["range_compression"] = (high - low).rolling(5).mean() / (high - low).rolling(60).mean()

    spread = max(2.0, settings.backtest_slippage_bps)
    out["spread_bps"] = spread
    out["microprice_dev_bps"] = 0.0
    out["book_imbalance_1"] = 0.0
    out["book_imbalance_5"] = 0.0
    out["book_imbalance_10"] = 0.0
    # L2 history is unavailable on Wallex; use a volume-based liquidity proxy.
    proxy_depth = (volume.rolling(60).sum() * close * 30).clip(lower=50_000_000)
    out["top5_depth_tmn"] = proxy_depth
    out["depth_25bps_tmn"] = proxy_depth * 0.5
    out["depth_50bps_tmn"] = proxy_depth * 0.8
    out["depth_100bps_tmn"] = proxy_depth
    out["book_age_ms"] = 0.0
    out["aggressive_buy_ratio"] = (0.5 + (ret_1 * 100).clip(-0.25, 0.25)).fillna(0.5)
    out["trade_count_60s"] = (volume > 0).rolling(60).sum()
    out["trade_notional_60s"] = (volume * close).rolling(60).sum()
    out["large_trade_ratio"] = 0.0
    out["trade_intensity"] = (volume > 0).rolling(60).sum() / 60.0

    out["btc_ret_5m"] = btc_ret_5.reindex(frame.index)
    out["btc_ret_15m"] = btc_ret_15.reindex(frame.index)
    out["btc_ret_1h"] = btc_ret_60.reindex(frame.index)
    out["symbol_vs_btc_15m_bps"] = (
        (out["ret_15m"] - out["btc_ret_15m"]) * 10_000
    )
    out["eth_ret_5m"] = eth_ret_5.reindex(frame.index) if eth_ret_5 is not None else np.nan
    out["usdtmn_ret_5m"] = usdt_ret_5.reindex(frame.index) if usdt_ret_5 is not None else np.nan

    btc_aligned = btc_close.reindex(frame.index)
    if btc_aligned.notna().sum() > 20:
        sym_ret = ret_1
        btc_ret = btc_aligned.pct_change(1)
        cov = sym_ret.rolling(120).cov(btc_ret)
        var = btc_ret.rolling(120).var()
        out["beta_btc"] = (cov / var.replace(0, np.nan)).fillna(1.0)
        out["corr_btc"] = sym_ret.rolling(120).corr(btc_ret).fillna(0.0)
    else:
        out["beta_btc"] = 1.0
        out["corr_btc"] = 0.0

    out["hour_utc"] = frame.index.hour
    out["weekday"] = frame.index.weekday
    if settings.challenge_end:
        out["time_to_end_sec"] = (settings.challenge_end - frame.index).total_seconds()
    else:
        out["time_to_end_sec"] = np.nan
    return out[FEATURE_COLUMNS]


def _regime_frame(btc: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    close = btc["close"]
    ret_5 = close.pct_change(5) * 10_000
    ret_15 = close.pct_change(15) * 10_000
    ret_60 = close.pct_change(60) * 10_000
    rv = close.pct_change(1).rolling(30).std(ddof=0)
    vol_pct = (rv.rolling(240, min_periods=60).rank(pct=True) * 100).fillna(50)
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    trend = np.where(ema21 > ema50, "up", "down")
    labels = np.select(
        [
            (vol_pct >= 98) & (ret_15.abs() > 150),
            (ret_60 < -40) & (trend == "down"),
            (ret_60 > 40) & (trend == "up"),
            vol_pct >= 85,
        ],
        ["chaos", "trend_down", "trend_up", "high_vol"],
        default="range",
    )
    out = pd.DataFrame(index=btc.index)
    out["regime_label"] = labels
    out["volatility_percentile"] = vol_pct
    out["btc_ret_5m_bps"] = ret_5
    out["btc_ret_15m_bps"] = ret_15
    out["btc_ret_1h_bps"] = ret_60
    out["btc_trend"] = trend
    out["regime_memo"] = [
        (
            f"Regime {label}: BTC 5m {r5:+.0f} bps, 15m {r15:+.0f} bps, 1h {r60:+.0f} bps, "
            f"trend {tr}, volatility percentile {vp:.0f}. Spot-only, long-only."
        )
        for label, r5, r15, r60, tr, vp in zip(
            labels,
            ret_5.fillna(0),
            ret_15.fillna(0),
            ret_60.fillna(0),
            trend,
            vol_pct,
            strict=False,
        )
    ]
    return out[REGIME_COLUMNS]


def row_to_features(row: pd.Series, symbol: str) -> dict[str, object]:
    features: dict[str, object] = {"symbol": symbol}
    for key, value in row.items():
        if key in ("ema_trend",):
            features[key] = value
            continue
        if isinstance(value, (int, np.integer)):
            features[key] = int(value)
        elif isinstance(value, (float, np.floating)):
            features[key] = None if not np.isfinite(value) else round(float(value), 6)
        else:
            features[key] = value
    return features


def build_swing_features(
    frames: dict[str, pd.DataFrame], settings: Settings
) -> dict[str, pd.DataFrame]:
    """Aggregate 1m candles into completed 4h bars and compute the slow trend signal."""
    result: dict[str, pd.DataFrame] = {}
    all_signals: dict[str, pd.Series] = {}
    for symbol, frame in frames.items():
        bars = frame.resample(
            f"{settings.swing_timeframe_hours}h", label="right", closed="right"
        ).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        ).dropna()
        if len(bars) < settings.swing_slow_ema + 5:
            continue
        close = bars["close"]
        fast = close.ewm(span=settings.swing_fast_ema, adjust=False).mean()
        slow = close.ewm(span=settings.swing_slow_ema, adjust=False).mean()
        ret_mom = close.pct_change(settings.swing_momentum_bars)
        prev_close = close.shift(1)
        true_range = pd.concat(
            [
                bars["high"] - bars["low"],
                (bars["high"] - prev_close).abs(),
                (bars["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.rolling(14).mean()
        atr_bps = atr / close * 10_000
        signal = (close > fast) & (fast > slow) & (ret_mom > 0)
        out = pd.DataFrame(index=bars.index)
        out["close"] = close
        out["ema_fast"] = fast
        out["ema_slow"] = slow
        out["ret_mom"] = ret_mom
        out["atr_bps"] = atr_bps
        out["trend_gap_bps"] = (close / slow - 1) * 10_000
        out["fast_gap_bps"] = (fast / slow - 1) * 10_000
        out["swing_signal"] = signal.astype(bool)
        out = out.dropna()
        result[symbol] = out
        all_signals[symbol] = signal
    if settings.swing_btc_filter and "BTCTMN" in all_signals:
        btc_signal = all_signals["BTCTMN"]
        for frame in result.values():
            btc_aligned = btc_signal.reindex(frame.index).fillna(False).astype(bool)
            frame["swing_signal"] = frame["swing_signal"] & btc_aligned
    return result
