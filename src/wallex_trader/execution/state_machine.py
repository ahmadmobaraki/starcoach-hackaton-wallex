from __future__ import annotations

from wallex_trader.models import Order, OrderStatus

ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.PENDING_SEND, OrderStatus.REJECTED, OrderStatus.CANCELED},
    OrderStatus.PENDING_SEND: {OrderStatus.SENT, OrderStatus.REJECTED, OrderStatus.UNKNOWN},
    OrderStatus.SENT: {
        OrderStatus.OPEN,
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.OPEN: {
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.PARTIAL: {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
    OrderStatus.UNKNOWN: {
        OrderStatus.OPEN,
        OrderStatus.PARTIAL,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    },
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition(order: Order, target: OrderStatus, *, reason: str = "") -> bool:
    if order.status == target:
        return True
    if not can_transition(order.status, target):
        return False
    order.status = target
    if reason:
        order.reason = reason
    from datetime import UTC, datetime

    order.updated_at = datetime.now(UTC)
    return True
