from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.exchange.rest import (
    WallexAPIError,
    WallexRESTClient,
    parse_order_response,
)
from wallex_trader.execution.precision import fmt_decimal
from wallex_trader.models import (
    ZERO,
    FeeRate,
    Fill,
    MarketMeta,
    Order,
    OrderStatus,
)

logger = logging.getLogger("trader.live_broker")


class LiveBroker:
    is_live = True

    def __init__(self, settings: Settings, rest: WallexRESTClient) -> None:
        self.settings = settings
        self.rest = rest
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            order
            for order in self.orders.values()
            if order.status in (OrderStatus.PENDING_SEND, OrderStatus.SENT, OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.UNKNOWN)
            and (symbol is None or order.symbol == symbol)
        ]

    async def place(
        self,
        order: Order,
        *,
        book: Any,
        meta: MarketMeta,
        fee_rate: FeeRate,
        now: datetime | None = None,
    ) -> list[Fill]:
        if not self.settings.is_live:
            raise RuntimeError("LiveBroker refused: mode is not live")
        payload = {
            "client_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "type": "LIMIT",
            "price": fmt_decimal(order.price, meta.price_precision),
            "quantity": fmt_decimal(order.quantity, meta.amount_precision),
        }
        # Keep OMS metadata (entry_meta/exit_reason) when attaching the exchange
        # payload. Overwriting it used to reset the position to the default
        # 20-minute time stop after a live fill.
        order.raw_request = {
            **dict(order.raw_request or {}),
            "exchange_payload": payload,
        }
        self.orders[order.client_order_id] = order
        order.status = OrderStatus.PENDING_SEND
        order.updated_at = now or datetime.now(UTC)
        try:
            response = await self.rest.create_order(payload)
        except WallexAPIError as exc:
            # Ambiguous POST: query by client ID before deciding it failed.
            if exc.retryable or exc.status in (None, 408, 502, 503, 504):
                existing = await self._safe_get_order(order.client_order_id)
                if existing is not None:
                    return self._apply_order_payload(order, existing, fee_rate)
            order.status = OrderStatus.REJECTED
            order.reason = str(exc)[:500]
            order.raw_response = {"error": str(exc), "payload": exc.payload}
            order.updated_at = datetime.now(UTC)
            return []
        order.raw_response = response
        fills = self._apply_order_payload(order, response, fee_rate)
        if order.status in (OrderStatus.SENT, OrderStatus.PENDING_SEND):
            refreshed = await self._safe_get_order(order.client_order_id)
            if refreshed is not None:
                fills.extend(self._apply_order_payload(order, refreshed, fee_rate))
        return _dedupe_fills(fills)

    async def refresh(self, order: Order, fee_rate: FeeRate | None = None) -> list[Fill]:
        payload = await self._safe_get_order(order.client_order_id)
        if payload is None:
            return []
        if order.status == OrderStatus.UNKNOWN:
            order.status = OrderStatus.SENT
        return self._apply_order_payload(order, payload, fee_rate)

    async def process_open_orders(
        self, fee_rates: dict[str, FeeRate] | None = None
    ) -> list[Fill]:
        fills: list[Fill] = []
        for order in list(self.open_orders()):
            fee_rate = (fee_rates or {}).get(order.symbol)
            fills.extend(await self.refresh(order, fee_rate))
        return _dedupe_fills(fills)

    async def cancel(self, client_order_id: str, reason: str = "canceled") -> bool:
        order = self.orders.get(client_order_id)
        try:
            await self.rest.cancel_order(client_order_id)
        except WallexAPIError as exc:
            logger.warning("cancel failed for %s: %s", client_order_id, exc)
            return False
        if order:
            order.status = OrderStatus.CANCELED
            order.reason = reason
            order.updated_at = datetime.now(UTC)
        return True

    async def cancel_all(self, symbol: str | None = None) -> int:
        count = 0
        for order in self.open_orders(symbol):
            if await self.cancel(order.client_order_id, "cancel_all"):
                count += 1
        try:
            count += await self.rest.cancel_all(symbol)
        except WallexAPIError as exc:
            logger.warning("exchange cancel_all failed: %s", exc)
        return count

    async def reconcile(self) -> dict[str, Any]:
        exchange_orders = await self.rest.open_orders()
        exchange_ids = {
            str(item.get("clientOrderId") or item.get("client_id") or "")
            for item in exchange_orders
        }
        local_ids = {order.client_order_id for order in self.open_orders()}
        missing_local = sorted(exchange_ids - local_ids)
        missing_exchange = sorted(local_ids - exchange_ids)
        return {
            "exchange_open_orders": len(exchange_orders),
            "local_open_orders": len(local_ids),
            "missing_local": missing_local,
            "missing_exchange": missing_exchange,
            "mismatch": bool(missing_local or missing_exchange),
        }

    async def balances(self) -> dict[str, dict[str, Any]]:
        return await self.rest.balances()

    async def _safe_get_order(self, client_order_id: str) -> dict[str, Any] | None:
        try:
            return await self.rest.get_order(client_order_id)
        except WallexAPIError as exc:
            logger.warning("order lookup failed for %s: %s", client_order_id, exc)
            return None

    def _apply_order_payload(
        self,
        order: Order,
        payload: dict[str, Any],
        fee_rate: FeeRate | None,
    ) -> list[Fill]:
        parsed = parse_order_response(payload)
        order.exchange_order_id = str(parsed.get("exchange_order_id") or order.exchange_order_id or "")
        order.status = _map_status(str(parsed.get("status", "UNKNOWN")))
        order.raw_response = payload
        order.updated_at = datetime.now(UTC)
        fills: list[Fill] = []
        raw_fills = parsed.get("fills") or []
        if isinstance(raw_fills, list):
            for index, item in enumerate(raw_fills):
                if not isinstance(item, dict):
                    continue
                try:
                    price = Decimal(str(item.get("price", parsed.get("avg_price", 0))))
                    qty = Decimal(
                        str(item.get("quantity", item.get("qty", item.get("executedQuantity", 0))))
                    )
                    fee = Decimal(str(item.get("fee", 0)))
                    fee_asset = str(item.get("feeAsset") or item.get("fee_asset") or "TMN")
                    if price <= 0 or qty <= 0:
                        continue
                    fill_id = str(
                        item.get("id")
                        or item.get("fillId")
                        or f"{order.client_order_id}_ex_{index}"
                    )
                    if any(existing.fill_id == fill_id for existing in order.fills):
                        continue
                    fill = Fill(
                        fill_id=fill_id,
                        order_id=order.client_order_id,
                        symbol=order.symbol,
                        side=order.side,
                        price=price,
                        quantity=qty,
                        fee=fee,
                        fee_asset=fee_asset,
                        ts=datetime.now(UTC),
                        liquidity=str(item.get("liquidity") or "taker"),
                        decision_id=order.decision_id,
                    )
                    order.fills.append(fill)
                    order.filled_quantity += qty
                    order.fee_tmn += fee
                    fills.append(fill)
                except (TypeError, ValueError):
                    continue
        if not fills and order.status == OrderStatus.FILLED and order.filled_quantity < order.quantity:
            price = Decimal(str(parsed.get("avg_price") or order.price))
            qty = Decimal(str(parsed.get("filled_quantity") or order.quantity))
            qty = min(qty, order.quantity)
            if qty > 0 and price > 0:
                rate = fee_rate.taker if fee_rate else self.settings.taker_fee
                fee = qty * price * Decimal(str(rate))
                fill = Fill(
                    fill_id=f"{order.client_order_id}_synthetic",
                    order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    price=price,
                    quantity=qty,
                    fee=fee,
                    fee_asset="TMN",
                    ts=datetime.now(UTC),
                    liquidity="taker",
                    decision_id=order.decision_id,
                )
                order.fills.append(fill)
                order.filled_quantity += qty
                order.fee_tmn += fee
                fills.append(fill)
        if order.filled_quantity > 0:
            total = sum((fill.price * fill.quantity for fill in order.fills), ZERO)
            order.avg_fill_price = total / order.filled_quantity
            if order.status in (OrderStatus.SENT, OrderStatus.OPEN):
                order.status = OrderStatus.PARTIAL
            if order.filled_quantity >= order.quantity - Decimal("1e-12"):
                order.status = OrderStatus.FILLED
        self.fills.extend(fills)
        return fills


def _map_status(status: str) -> OrderStatus:
    value = status.upper().replace(" ", "_")
    return {
        "NEW": OrderStatus.OPEN,
        "OPEN": OrderStatus.OPEN,
        "PENDING": OrderStatus.PENDING_SEND,
        "SENT": OrderStatus.SENT,
        "PARTIAL": OrderStatus.PARTIAL,
        "PARTIALLY_FILLED": OrderStatus.PARTIAL,
        "FILLED": OrderStatus.FILLED,
        "DONE": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "CANCELLED": OrderStatus.CANCELED,
        "REJECTED": OrderStatus.REJECTED,
        "EXPIRED": OrderStatus.EXPIRED,
        "UNKNOWN": OrderStatus.UNKNOWN,
    }.get(value, OrderStatus.UNKNOWN)


def _dedupe_fills(fills: list[Fill]) -> list[Fill]:
    seen: set[str] = set()
    result: list[Fill] = []
    for fill in fills:
        if fill.fill_id in seen:
            continue
        seen.add(fill.fill_id)
        result.append(fill)
    return result
