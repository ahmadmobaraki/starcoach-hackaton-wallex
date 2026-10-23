from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wallex_trader.config import Settings
from wallex_trader.models import Candle, MarketMeta
from wallex_trader.strategy.regime import RegimeState
from wallex_trader.strategy.swing import (
    SwingSignalEngine,
    resample_candles,
    swing_feature_row,
)


def make_trending_candles(count: int = 300) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = []
    price = 100.0
    for index in range(count):
        close = price * (1.001**index)
        candles.append(
            Candle(
                symbol="BTCTMN",
                ts=start + timedelta(hours=4 * index),
                open=close * 0.999,
                high=close * 1.002,
                low=close * 0.998,
                close=close,
                volume=1000.0,
            )
        )
    return candles


def make_regime() -> RegimeState:
    return RegimeState(
        label="trend_up",
        memo="test",
        btc_ret_5m_bps=10,
        btc_ret_15m_bps=20,
        btc_ret_1h_bps=30,
        volatility_percentile=50,
        average_spread_bps=5,
        btc_trend="up",
        created_at=datetime.now(UTC),
    )


def test_market_meta_uses_precision_not_raw_tick_fields() -> None:
    meta = MarketMeta(
        symbol="PEPETMN",
        base_asset="PEPE",
        quote_asset="TMN",
        price_precision=4,
        amount_precision=0,
        tick_size=4.0,
        step_size=1.0,
    )
    assert meta.tick == Decimal("0.0001")
    assert meta.step == Decimal("1")
    btc = MarketMeta(
        symbol="BTCTMN",
        base_asset="BTC",
        quote_asset="TMN",
        price_precision=0,
        amount_precision=8,
        tick_size=1.0,
        step_size=8.0,
    )
    assert btc.tick == Decimal("1")
    assert btc.step == Decimal("0.00000001")


def test_rising_trend_emits_candidate(settings: Settings) -> None:
    candles = make_trending_candles()
    values = swing_feature_row(candles, settings)
    assert values is not None
    candidate = SwingSignalEngine(settings).evaluate_values(
        "BTCTMN", values, make_regime(), fee_taker=0.003, fee_maker=0.0025
    )
    assert candidate is not None
    assert candidate.signal == "TREND"
    assert candidate.recommended_stop_bps >= 300
    assert candidate.recommended_take_profit_bps >= candidate.recommended_stop_bps
    assert candidate.recommended_horizon_sec == settings.swing_horizon_sec
    assert candidate.expected_edge_bps >= settings.alpha_threshold_bps


def test_flat_market_has_no_trend(settings: Settings) -> None:
    candles = [
        Candle(
            symbol="BTCTMN",
            ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=4 * index),
            open=100,
            high=100.1,
            low=99.9,
            close=100,
            volume=1000,
        )
        for index in range(300)
    ]
    values = swing_feature_row(candles, settings)
    assert values is not None
    assert (
        SwingSignalEngine(settings).evaluate_values(
            "BTCTMN", values, make_regime(), fee_taker=0.003, fee_maker=0.0025
        )
        is None
    )


def test_resample_hourly_to_completed_four_hour() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    hourly = [
        Candle(
            symbol="BTCTMN",
            ts=start + timedelta(hours=index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100.5 + index,
            volume=10,
        )
        for index in range(8)
    ]
    bars = resample_candles(hourly, hours=4, now=start + timedelta(hours=8))
    assert len(bars) == 2
    assert bars[0].open == 100
    assert bars[0].close == 103.5
    assert bars[0].high == 104
    assert bars[0].ts == start + timedelta(hours=4)
