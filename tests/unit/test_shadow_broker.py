from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wallex_trader.execution.shadow_broker import ShadowBroker
from wallex_trader.models import (
    BookLevel,
    FeeRate,
    MarketMeta,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Side,
    TradeTick,
)


def make_book() -> OrderBook:
    return OrderBook(
        symbol="BTCTMN",
        bids=[
            BookLevel(Decimal("99975"), Decimal("100")),
            BookLevel(Decimal("99950"), Decimal("100")),
        ],
        asks=[
            BookLevel(Decimal("100025"), Decimal("100")),
            BookLevel(Decimal("100050"), Decimal("100")),
        ],
        updated_at=datetime.now(UTC),
    )


def make_meta() -> MarketMeta:
    return MarketMeta(
        symbol="BTCTMN",
        base_asset="BTC",
        quote_asset="TMN",
        price_precision=0,
        amount_precision=8,
        min_notional=50_000,
    )


def make_order(
    order_type: OrderType = OrderType.MARKETABLE_LIMIT,
    side: Side = Side.BUY,
    price: str = "100050",
    quantity: str = "2",
) -> Order:
    return Order(
        client_order_id="test_order_1",
        exchange_order_id=None,
        symbol="BTCTMN",
        side=side,
        order_type=order_type,
        price=Decimal(price),
        quantity=Decimal(quantity),
        status=OrderStatus.CREATED,
    )


def test_marketable_buy_fills_with_taker_fee(settings) -> None:
    broker = ShadowBroker(settings)
    order = make_order()
    fills = broker.place(
        order,
        book=make_book(),
        meta=make_meta(),
        fee_rate=FeeRate(0.0025, 0.003),
    )
    assert len(fills) == 1
    assert order.status == OrderStatus.FILLED
    assert fills[0].liquidity == "taker"
    assert fills[0].price > Decimal("100025")
    assert fills[0].fee > 0


def test_passive_order_waits_for_trade(settings) -> None:
    broker = ShadowBroker(settings)
    order = make_order(order_type=OrderType.LIMIT, price="99900")
    fills = broker.place(
        order,
        book=make_book(),
        meta=make_meta(),
        fee_rate=FeeRate(0.0025, 0.003),
    )
    assert fills == []
    assert order.status == OrderStatus.OPEN
    trade = TradeTick(
        symbol="BTCTMN",
        price=Decimal("99800"),
        quantity=Decimal("10"),
        is_buy_order=True,
        timestamp=datetime.now(UTC) + timedelta(seconds=1),
    )
    fills = broker.process_open_orders({"BTCTMN": make_book()}, {"BTCTMN": [trade]})
    assert len(fills) == 1
    assert fills[0].liquidity == "maker"


def test_self_trade_prevention(settings) -> None:
    broker = ShadowBroker(settings)
    sell = make_order(side=Side.SELL, order_type=OrderType.LIMIT, price="101000")
    broker.place(
        sell,
        book=make_book(),
        meta=make_meta(),
        fee_rate=FeeRate(0.0025, 0.003),
    )
    buy = make_order()
    buy.client_order_id = "test_order_2"
    fills = broker.place(
        buy,
        book=make_book(),
        meta=make_meta(),
        fee_rate=FeeRate(0.0025, 0.003),
    )
    assert fills == []
    assert buy.status == OrderStatus.REJECTED
    assert "self-trade" in buy.reason
