from __future__ import annotations

from decimal import Decimal

from wallex_trader.models import Side


def taker_price(open_price: float, side: Side, slippage_bps: float) -> Decimal:
    factor = slippage_bps / 10_000
    price = open_price * (1 + factor if side == Side.BUY else 1 - factor)
    return Decimal(str(price))


def limit_fill_price(
    open_price: float,
    high: float,
    low: float,
    limit_price: Decimal,
    side: Side,
) -> Decimal | None:
    limit = float(limit_price)
    if side == Side.BUY:
        if low > limit:
            return None
        return Decimal(str(min(open_price, limit)))
    if high < limit:
        return None
    return Decimal(str(max(open_price, limit)))


def stop_fill_price(
    open_price: float,
    stop_price: Decimal,
    side: Side,
    slippage_bps: float,
) -> Decimal:
    stop = float(stop_price)
    base = min(open_price, stop) if side == Side.SELL else max(open_price, stop)
    factor = slippage_bps / 10_000
    if side == Side.SELL:
        return Decimal(str(base * (1 - factor)))
    return Decimal(str(base * (1 + factor)))


def target_fill_price(
    open_price: float,
    target_price: Decimal,
    side: Side,
    slippage_bps: float,
) -> Decimal:
    target = float(target_price)
    base = max(open_price, target) if side == Side.SELL else min(open_price, target)
    factor = slippage_bps / 10_000
    if side == Side.SELL:
        return Decimal(str(base * (1 - factor)))
    return Decimal(str(base * (1 + factor)))


def fee_tmn(notional: Decimal, rate: float) -> Decimal:
    return notional * Decimal(str(rate))
