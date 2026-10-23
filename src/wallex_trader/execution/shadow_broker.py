from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from wallex_trader.config import Settings
from wallex_trader.models import (
    ZERO,
    FeeRate,
    Fill,
    MarketMeta,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Side,
    TradeTick,
)

logger = logging.getLogger("trader.shadow_broker")


class ShadowBroker:
    """Pessimistic paper broker. It never talks to the Wallex order API."""

    is_live = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self._fill_seq = 0
        self.last_activity = datetime.now(UTC)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            order
            for order in self.orders.values()
            if order.status in (OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING_SEND, OrderStatus.SENT)
            and (symbol is None or order.symbol == symbol)
        ]

    def place(
        self,
        order: Order,
        *,
        book: OrderBook,
        meta: MarketMeta,
        fee_rate: FeeRate,
        now: datetime | None = None,
    ) -> list[Fill]:
        current = now or datetime.now(UTC)
        self.orders[order.client_order_id] = order
        order.status = OrderStatus.SENT
        order.updated_at = current
        if self._would_self_trade(order):
            order.status = OrderStatus.REJECTED
            order.reason = "self-trade prevention"
            return []
        if order.order_type == OrderType.MARKETABLE_LIMIT:
            fills = self._fill_marketable(order, book, meta, fee_rate, current)
        else:
            if self._crosses(order, book):
                fills = self._fill_marketable(order, book, meta, fee_rate, current)
            else:
                order.status = OrderStatus.OPEN
                fills = []
        self.last_activity = current
        return fills

    def process_open_orders(
        self,
        books: dict[str, OrderBook],
        trades: dict[str, list[TradeTick]],
        *,
        now: datetime | None = None,
    ) -> list[Fill]:
        current = now or datetime.now(UTC)
        fills: list[Fill] = []
        for order in list(self.open_orders()):
            book = books.get(order.symbol)
            if book is None:
                continue
            if order.status in (OrderStatus.PENDING_SEND, OrderStatus.SENT):
                order.status = OrderStatus.OPEN
            if self._crosses(order, book):
                fills.extend(self._fill_marketable(order, book, None, None, current, passive_price=order.price))
                continue
            for trade in trades.get(order.symbol, []):
                if trade.timestamp < order.updated_at:
                    continue
                if self._trade_fills(order, trade):
                    fill = self._make_fill(
                        order,
                        price=order.price,
                        quantity=min(order.remaining, trade.quantity * Decimal("0.35")),
                        fee_rate=self.settings.maker_fee,
                        liquidity="maker",
                        ts=current,
                    )
                    if fill:
                        fills.append(fill)
                    break
        self.last_activity = current
        return fills

    def cancel(self, client_order_id: str, reason: str = "canceled") -> bool:
        order = self.orders.get(client_order_id)
        if order is None or order.status in (OrderStatus.FILLED, OrderStatus.CANCELED):
            return False
        order.status = OrderStatus.CANCELED
        order.reason = reason
        order.updated_at = datetime.now(UTC)
        return True

    def cancel_all(self, symbol: str | None = None) -> int:
        count = 0
        for order in self.open_orders(symbol):
            if self.cancel(order.client_order_id, "cancel_all"):
                count += 1
        return count

    def _fill_marketable(
        self,
        order: Order,
        book: OrderBook,
        meta: MarketMeta | None,
        fee_rate: FeeRate | None,
        now: datetime,
        *,
        passive_price: Decimal | None = None,
    ) -> list[Fill]:
        levels = book.asks if order.side == Side.BUY else book.bids
        if not levels:
            return []
        remaining = order.remaining
        filled_notional = ZERO
        filled_qty = ZERO
        for level in levels:
            if remaining <= 0:
                break
            if order.side == Side.BUY and level.price > order.price:
                break
            if order.side == Side.SELL and level.price < order.price:
                break
            take = min(remaining, level.quantity)
            if take <= 0:
                continue
            filled_qty += take
            filled_notional += take * level.price
            remaining -= take
        if filled_qty <= 0:
            order.status = OrderStatus.OPEN
            return []
        avg_price = filled_notional / filled_qty
        impact_bps = self._impact_bps(filled_qty, book, order.side)
        slip = Decimal(str(impact_bps)) / Decimal(10_000)
        avg_price = avg_price * (Decimal(1) + slip if order.side == Side.BUY else Decimal(1) - slip)
        if order.side == Side.BUY:
            avg_price = min(avg_price, order.price)
        else:
            avg_price = max(avg_price, order.price)
        rate = (fee_rate.taker if fee_rate else self.settings.taker_fee)
        fill = self._make_fill(
            order,
            price=avg_price,
            quantity=filled_qty,
            fee_rate=rate,
            liquidity="taker",
            ts=now,
        )
        if fill:
            if order.remaining > 0:
                order.status = OrderStatus.PARTIAL
                order.reason = "partial marketable fill; remainder left open"
            return [fill]
        return []

    def _make_fill(
        self,
        order: Order,
        *,
        price: Decimal,
        quantity: Decimal,
        fee_rate: float,
        liquidity: str,
        ts: datetime,
    ) -> Fill | None:
        quantity = min(quantity, order.remaining)
        if quantity <= 0:
            return None
        notional = price * quantity
        fee = notional * Decimal(str(fee_rate))
        self._fill_seq += 1
        fill = Fill(
            fill_id=f"{order.client_order_id}_{self._fill_seq:04d}",
            order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            price=price,
            quantity=quantity,
            fee=fee,
            fee_asset="TMN",
            ts=ts,
            liquidity=liquidity,
            decision_id=order.decision_id,
        )
        order.fills.append(fill)
        order.filled_quantity += quantity
        total_notional = sum((f.price * f.quantity for f in order.fills), ZERO)
        order.avg_fill_price = total_notional / order.filled_quantity if order.filled_quantity else ZERO
        order.fee_tmn += fee
        order.updated_at = ts
        if order.filled_quantity >= order.quantity - Decimal("1e-12"):
            order.status = OrderStatus.FILLED
        self.fills.append(fill)
        return fill

    @staticmethod
    def _crosses(order: Order, book: OrderBook) -> bool:
        if order.side == Side.BUY and book.best_ask is not None:
            return order.price >= book.best_ask
        if order.side == Side.SELL and book.best_bid is not None:
            return order.price <= book.best_bid
        return False

    @staticmethod
    def _trade_fills(order: Order, trade: TradeTick) -> bool:
        # Passive maker fills require the market to trade through the limit.
        if order.side == Side.BUY:
            return trade.price < order.price and trade.taker_side == Side.SELL
        return trade.price > order.price and trade.taker_side == Side.BUY

    def _would_self_trade(self, order: Order) -> bool:
        return any(
            other.symbol == order.symbol
            and other.side != order.side
            and other.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)
            and other.remaining > 0
            for other in self.orders.values()
            if other.client_order_id != order.client_order_id
        )

    @staticmethod
    def _impact_bps(quantity: Decimal, book: OrderBook, side: Side) -> float:
        depth = book.depth_quantity(Side.SELL if side == Side.BUY else Side.BUY, 5)
        if depth <= 0:
            return 100.0
        ratio = float(quantity / depth)
        return min(60.0, 2.0 + 40.0 * ratio)
