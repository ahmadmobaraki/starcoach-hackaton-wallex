from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from wallex_trader.models import Side


def to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_step(value: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def floor_quantity(quantity: Decimal, step: Decimal, min_qty: Decimal = Decimal("0")) -> Decimal:
    qty = quantize_step(quantity, step, ROUND_DOWN)
    if qty < min_qty:
        return Decimal("0")
    return qty


def quantize_price(
    price: Decimal,
    tick: Decimal,
    side: Side,
    *,
    aggressive: bool,
) -> Decimal:
    if price <= 0 or tick <= 0:
        return price
    if side == Side.BUY:
        rounding = ROUND_UP if aggressive else ROUND_DOWN
    else:
        rounding = ROUND_DOWN if aggressive else ROUND_UP
    return quantize_step(price, tick, rounding)


def bps_to_fraction(bps: float) -> Decimal:
    return Decimal(str(bps)) / Decimal(10_000)


def apply_bps(price: Decimal, bps: float, *, up: bool) -> Decimal:
    factor = Decimal(1) + (bps_to_fraction(bps) if up else -bps_to_fraction(bps))
    return price * factor


def pct_change(new: Decimal, old: Decimal) -> float:
    if old <= 0:
        return 0.0
    return float((new - old) / old)


def fmt_decimal(value: Decimal, precision: int) -> str:
    quantum = Decimal(1).scaleb(-precision)
    return format(value.quantize(quantum), "f")
