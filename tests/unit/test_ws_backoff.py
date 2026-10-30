from __future__ import annotations

from wallex_trader.exchange.ws import (
    MAX_RECONNECT_SEC,
    RECONNECT_BASE_SEC,
    STABLE_CONNECTION_SEC,
    backoff_before_retry,
)


def test_stable_connection_resets_saturated_backoff() -> None:
    assert (
        backoff_before_retry(
            30.0, was_connected=True, connected_sec=STABLE_CONNECTION_SEC
        )
        == RECONNECT_BASE_SEC
    )


def test_short_connection_keeps_backoff() -> None:
    assert backoff_before_retry(16.0, was_connected=True, connected_sec=1.0) == 16.0
    assert backoff_before_retry(16.0, was_connected=False, connected_sec=0.0) == 16.0


def test_backoff_is_capped() -> None:
    assert (
        backoff_before_retry(
            MAX_RECONNECT_SEC + 10.0, was_connected=False, connected_sec=0.0
        )
        == MAX_RECONNECT_SEC
    )
