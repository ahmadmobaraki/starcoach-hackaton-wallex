from __future__ import annotations

from decimal import Decimal

from wallex_trader.execution.precision import (
    apply_bps,
    floor_quantity,
    fmt_decimal,
    quantize_price,
)
from wallex_trader.models import Side


def test_floor_quantity_never_rounds_up() -> None:
    assert floor_quantity(Decimal("1.23456789"), Decimal("0.0001")) == Decimal("1.2345")
    assert floor_quantity(Decimal("0.00001"), Decimal("0.0001")) == Decimal("0")


def test_floor_quantity_respects_minimum() -> None:
    assert floor_quantity(Decimal("0.5"), Decimal("1"), Decimal("1")) == Decimal("0")
    assert floor_quantity(Decimal("1.5"), Decimal("1"), Decimal("1")) == Decimal("1")


def test_aggressive_buy_price_rounds_up() -> None:
    price = quantize_price(Decimal("100.001"), Decimal("0.01"), Side.BUY, aggressive=True)
    assert price == Decimal("100.01")


def test_passive_buy_price_rounds_down() -> None:
    price = quantize_price(Decimal("100.009"), Decimal("0.01"), Side.BUY, aggressive=False)
    assert price == Decimal("100.00")


def test_aggressive_sell_price_rounds_down() -> None:
    price = quantize_price(Decimal("100.009"), Decimal("0.01"), Side.SELL, aggressive=True)
    assert price == Decimal("100.00")


def test_apply_bps() -> None:
    assert apply_bps(Decimal("100"), 25, up=True) == Decimal("100.25")
    assert apply_bps(Decimal("100"), 25, up=False) == Decimal("99.75")


def test_fmt_decimal() -> None:
    assert fmt_decimal(Decimal("22550000000"), 0) == "22550000000"
    assert fmt_decimal(Decimal("0.00002"), 8) == "0.00002000"
