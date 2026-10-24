from __future__ import annotations

from wallex_trader.config import Settings
from wallex_trader.engine import CLOCK_SKEW_CONFIRMATIONS, TradingEngine


def test_clock_skew_needs_consecutive_samples(settings: Settings) -> None:
    engine = TradingEngine(settings)
    threshold = settings.clock_skew_kill_sec

    for _ in range(CLOCK_SKEW_CONFIRMATIONS - 1):
        assert not engine._clock_skew_confirmed(threshold + 1.0)
    assert engine._clock_skew_confirmed(threshold + 1.0)


def test_clock_skew_low_sample_resets_streak(settings: Settings) -> None:
    engine = TradingEngine(settings)
    threshold = settings.clock_skew_kill_sec

    for _ in range(CLOCK_SKEW_CONFIRMATIONS - 1):
        assert not engine._clock_skew_confirmed(-(threshold + 2.0))
    assert not engine._clock_skew_confirmed(threshold)
    for _ in range(CLOCK_SKEW_CONFIRMATIONS - 1):
        assert not engine._clock_skew_confirmed(threshold + 0.5)
    assert engine._clock_skew_confirmed(threshold + 0.5)


def test_clock_skew_missing_sample_resets_streak(settings: Settings) -> None:
    engine = TradingEngine(settings)
    assert not engine._clock_skew_confirmed(settings.clock_skew_kill_sec + 5.0)
    assert not engine._clock_skew_confirmed(None)
    assert engine._clock_skew_streak == 0
