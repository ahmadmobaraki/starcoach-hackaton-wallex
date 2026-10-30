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


def test_ws_down_needs_consecutive_checks(settings: Settings) -> None:
    from wallex_trader.engine import WS_DOWN_CONFIRMATIONS

    engine = TradingEngine(settings)
    threshold = float(settings.ws_down_kill_sec)

    for _ in range(WS_DOWN_CONFIRMATIONS - 1):
        assert not engine._ws_down_confirmed(threshold + 1.0)
    assert engine._ws_down_confirmed(threshold + 1.0)


def test_ws_down_recovery_resets_streak(settings: Settings) -> None:
    engine = TradingEngine(settings)
    threshold = float(settings.ws_down_kill_sec)

    assert not engine._ws_down_confirmed(threshold + 1.0)
    assert not engine._ws_down_confirmed(threshold)
    assert engine._ws_down_streak == 0
    for _ in range(2):
        assert not engine._ws_down_confirmed(threshold + 1.0)
    assert engine._ws_down_confirmed(threshold + 1.0)


def test_ws_down_wall_clock_backstop(settings: Settings) -> None:
    from wallex_trader.engine import WS_DOWN_KILL_GRACE_SEC

    engine = TradingEngine(settings)
    threshold = float(settings.ws_down_kill_sec)

    assert not engine._ws_down_confirmed(threshold + 1.0)
    assert engine._ws_down_confirmed(threshold + WS_DOWN_KILL_GRACE_SEC)
