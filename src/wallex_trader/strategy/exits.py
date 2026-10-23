from __future__ import annotations

from decimal import Decimal
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.models import Position


def update_trailing_stop(
    position: Position,
    mark: Decimal,
    settings: Settings,
) -> bool:
    """Tighten the stop once the trade is meaningfully in profit."""
    if position.avg_entry <= 0 or mark <= 0:
        return False
    trigger_bps = (
        settings.swing_trail_trigger_bps
        if position.signal == "TREND"
        else settings.trail_trigger_bps
    )
    distance_bps = (
        settings.swing_trail_distance_bps
        if position.signal == "TREND"
        else settings.trail_distance_bps
    )
    trigger = position.avg_entry * (
        Decimal(1) + Decimal(str(trigger_bps)) / Decimal(10_000)
    )
    if mark < trigger:
        return False
    candidate = mark * (
        Decimal(1) - Decimal(str(distance_bps)) / Decimal(10_000)
    )
    if candidate > position.stop_price:
        position.stop_price = candidate
        return True
    return False


def round_trip_cost_bps(
    settings: Settings,
    fee_taker: float,
    spread_bps: float,
) -> float:
    return (
        2 * max(fee_taker, settings.taker_fee) * 10_000
        + max(0.0, spread_bps)
        + settings.cost_buffer_bps
        + 8.0
    )


def evaluate_signal_exit(
    features: dict[str, Any],
    position: Position,
    mark: Decimal,
    settings: Settings,
    fee_taker: float,
) -> str | None:
    """Exit when the original thesis has played out. Fees are always considered."""
    if position.avg_entry <= 0 or mark <= 0:
        return None
    if position.signal == "TREND":
        return None
    if position.age_sec() < 300:
        return None
    unrealized_bps = float((mark / position.avg_entry - 1) * Decimal(10_000))
    spread = _num(features.get("spread_bps"), settings.max_spread_bps)
    cost = round_trip_cost_bps(settings, fee_taker, spread)
    z = _num(features.get("bb_z_20"))
    ema21_dist = _num(features.get("ema21_dist_bps"))
    ret_5m = _num(features.get("ret_5m_bps"))
    ret_15m = _num(features.get("ret_15m_bps"))
    ema9_dist = _num(features.get("ema9_dist_bps"))
    if position.signal == "MR":
        if unrealized_bps >= max(80.0, cost + 10.0) and (z >= -0.25 or ema21_dist >= 0):
            return "mean_reversion_exit"
        if unrealized_bps < -max(60.0, cost) and z < -3.5:
            return "thesis_invalidated"
    elif position.signal == "MOM":
        if unrealized_bps >= max(100.0, cost + 10.0) and (
            ret_5m < 0 or ret_15m < 0 or ema9_dist < 0
        ):
            return "momentum_reversal"
        if unrealized_bps < -max(75.0, cost) and ret_5m < 0 and ret_15m < 0:
            return "thesis_invalidated"
    return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
