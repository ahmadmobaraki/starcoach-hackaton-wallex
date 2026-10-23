from __future__ import annotations

from decimal import Decimal

from wallex_trader.execution.precision import floor_quantity

ZERO = Decimal("0")


def base_asset(symbol: str) -> str:
    return symbol[:-3] if symbol.endswith("TMN") else symbol


def is_sub_step(quantity: Decimal, step: Decimal) -> bool:
    """True when the quantity cannot be represented by the market step."""
    return quantity > ZERO and step > ZERO and floor_quantity(quantity, step) <= ZERO


def is_unsellable(quantity: Decimal, step: Decimal, mark: Decimal, min_notional: Decimal) -> bool:
    """True when an exchange remainder is below the step or exchange minimum notional."""
    if quantity <= ZERO:
        return True
    if is_sub_step(quantity, step):
        return True
    if mark > ZERO and min_notional > ZERO and quantity * mark < min_notional:
        return True
    return False


def add_dust(dust: dict[str, Decimal], asset: str, quantity: Decimal) -> Decimal:
    if quantity <= ZERO:
        return dust.get(asset, ZERO)
    total = dust.get(asset, ZERO) + quantity
    dust[asset] = total
    return total


def set_dust(dust: dict[str, Decimal], asset: str, quantity: Decimal) -> None:
    if quantity > ZERO:
        dust[asset] = quantity
    else:
        dust.pop(asset, None)
