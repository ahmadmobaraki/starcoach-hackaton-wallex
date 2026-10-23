from __future__ import annotations

import numpy as np
import pandas as pd

from wallex_trader.backtest.precompute import build_feature_frames
from wallex_trader.strategy.regime import RegimeState
from wallex_trader.strategy.signals import SignalEngine


def make_frames(rows: int = 600) -> dict[str, pd.DataFrame]:
    index = pd.date_range("2026-01-01", periods=rows, freq="1min", tz="UTC")
    base = np.linspace(100, 120, rows) + np.sin(np.arange(rows) / 20) * 0.5
    frame = pd.DataFrame(
        {
            "ts": (index.view("int64") // 1_000_000_000),
            "symbol": "BTCTMN",
            "open": base - 0.1,
            "high": base + 0.2,
            "low": base - 0.2,
            "close": base,
            "volume": np.linspace(10, 20, rows),
        },
        index=index,
    )
    eth = frame.copy()
    eth["symbol"] = "ETHTMN"
    eth[["open", "high", "low", "close"]] *= 0.5
    usdt = frame.copy()
    usdt["symbol"] = "USDTTMN"
    usdt[["open", "high", "low", "close"]] = 270000.0
    return {"BTCTMN": frame, "ETHTMN": eth, "USDTTMN": usdt}


def test_features_do_not_use_future_rows(settings) -> None:
    frames = make_frames()
    features_before, regime_before = build_feature_frames(frames, settings)
    mutated = {symbol: frame.copy() for symbol, frame in frames.items()}
    mutated["BTCTMN"].loc[mutated["BTCTMN"].index[400:], "close"] *= 10
    mutated["BTCTMN"].loc[mutated["BTCTMN"].index[400:], "high"] *= 10
    mutated["BTCTMN"].loc[mutated["BTCTMN"].index[400:], "low"] *= 10
    features_after, regime_after = build_feature_frames(mutated, settings)
    before = features_before["BTCTMN"].loc[: mutated["BTCTMN"].index[399]]
    after = features_after["BTCTMN"].loc[: mutated["BTCTMN"].index[399]]
    pd.testing.assert_frame_equal(before, after, check_exact=False, rtol=1e-9, atol=1e-12)
    pd.testing.assert_frame_equal(
        regime_before.loc[: mutated["BTCTMN"].index[399]],
        regime_after.loc[: mutated["BTCTMN"].index[399]],
        check_exact=False,
        rtol=1e-9,
        atol=1e-12,
    )


def test_signal_engine_emits_mean_reversion_candidate(settings) -> None:
    regime = RegimeState(
        label="range",
        memo="test",
        btc_ret_5m_bps=0,
        btc_ret_15m_bps=0,
        btc_ret_1h_bps=0,
        volatility_percentile=50,
        average_spread_bps=5,
        btc_trend="up",
        created_at=pd.Timestamp.now(tz="UTC").to_pydatetime(),
    )
    features = {
        "price": 100000.0,
        "spread_bps": 5.0,
        "top5_depth_tmn": 100_000_000.0,
        "bb_z_20": -3.0,
        "rsi_14": 18.0,
        "ret_5m": -0.02,
        "ret_15m": -0.03,
        "ret_1h": -0.04,
        "volume_z_60": 2.0,
        "book_imbalance_5": 0.6,
        "aggressive_buy_ratio": 0.7,
        "btc_ret_15m": 0.001,
        "atr_bps": 150.0,
        "rv_1m_bps": 50.0,
        "rv_percentile": 80.0,
        "ema21_dist_bps": -100.0,
        "price_vs_vwap_bps": -80.0,
    }
    candidate = SignalEngine(settings).evaluate(
        "BTCTMN", features, regime, fee_taker=0.003, fee_maker=0.0025
    )
    assert candidate is not None
    assert candidate.signal == "MR"
    assert candidate.expected_edge_bps >= settings.alpha_threshold_bps


def test_candle_store_ignores_out_of_order_without_raising() -> None:
    from datetime import UTC, datetime, timedelta

    from wallex_trader.marketdata.candles import CandleStore
    from wallex_trader.models import Candle

    store = CandleStore(maxlen=5)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(5):
        store.upsert(
            Candle(
                symbol="BTCTMN",
                ts=start + timedelta(minutes=index),
                open=100,
                high=101,
                low=99,
                close=100,
                volume=1,
            )
        )
    store.upsert(
        Candle(
            symbol="BTCTMN",
            ts=start + timedelta(minutes=2),
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=1,
        )
    )
    store.upsert(
        Candle(
            symbol="BTCTMN",
            ts=start + timedelta(seconds=30),
            open=100,
            high=101,
            low=99,
            close=99.5,
            volume=1,
        )
    )
    assert len(store.candles("BTCTMN")) == 5
    assert store.candles("BTCTMN")[-1].close == 100
