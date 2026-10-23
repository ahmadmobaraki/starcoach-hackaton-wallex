from __future__ import annotations

import inspect
import logging
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.execution.live_broker import LiveBroker
from wallex_trader.execution.shadow_broker import ShadowBroker
from wallex_trader.execution.state_machine import transition
from wallex_trader.models import (
    ZERO,
    Candidate,
    Fill,
    MarketMeta,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    Position,
    Side,
    TradeTick,
)
from wallex_trader.risk.limits import RiskApproval
from wallex_trader.storage.repositories import Repository, json_safe
from wallex_trader.strategy.portfolio import Portfolio

logger = logging.getLogger("trader.oms")


class OMS:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        portfolio: Portfolio,
        broker: ShadowBroker | LiveBroker,
        fee_rates: dict[str, Any] | None = None,
    ) -> None:
        if settings.is_live and not broker.is_live:
            raise RuntimeError("Live mode cannot use ShadowBroker")
        if not settings.is_live and broker.is_live:
            raise RuntimeError("Shadow mode cannot use LiveBroker")
        self.settings = settings
        self.repo = repo
        self.portfolio = portfolio
        self.broker = broker
        self.fee_rates = fee_rates or {}
        self.orders: dict[str, Order] = {}
        self.applied_fills: set[str] = set()
        self.entries_enabled = True
        self.reserved: dict[str, Decimal] = {}
        self._seq = 0
        self.run_token = re.sub(r"[^A-Za-z0-9]", "", repo.run_id)[-8:] or "run"

    @property
    def is_live(self) -> bool:
        return self.broker.is_live

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return self.broker.open_orders(symbol)

    def register_order(self, order: Order) -> None:
        self.orders[order.client_order_id] = order

    async def execute_entry(
        self,
        *,
        decision: Any,
        candidate: Candidate,
        approval: RiskApproval,
        meta: MarketMeta,
        book: OrderBook,
        decision_id: str,
        fee_rate: Any,
        agent_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        if not self.entries_enabled:
            return {
                "client_order_id": "",
                "status": "REJECTED",
                "error": "kill switch active; entry not sent",
                "fills": [],
            }
        client_id = self._client_id(candidate.symbol)
        order = Order(
            client_order_id=client_id,
            exchange_order_id=None,
            symbol=candidate.symbol,
            side=Side.BUY,
            order_type=OrderType(approval.order_type),
            price=approval.price,
            quantity=approval.quantity,
            status=OrderStatus.CREATED,
            decision_id=decision_id,
            raw_request={
                "entry_meta": {
                    "decision_id": decision_id,
                    "signal": candidate.signal,
                    "confidence": decision.confidence,
                    "rationale": decision.rationale,
                    "regime": decision.regime,
                    "stop_bps": decision.stop_loss_bps,
                    "take_profit_bps": decision.take_profit_bps,
                    "horizon_sec": decision.horizon_sec,
                    "features": candidate.features,
                    "agent": agent_payload or {},
                }
            },
        )
        self.register_order(order)
        await self.repo.save_order(order)
        transition(order, OrderStatus.PENDING_SEND, reason="entry approved")
        reserve = approval.notional_tmn + approval.fee_tmn
        self.reserved[order.client_order_id] = reserve
        self.portfolio.reserve_cash(reserve)
        try:
            fills = await _await_if_needed(
                self.broker.place(
                    order,
                    book=book or OrderBook(symbol=order.symbol),
                    meta=meta,
                    fee_rate=fee_rate,
                    now=now,
                )
            )
        except Exception as exc:  # noqa: BLE001
            transition(order, OrderStatus.REJECTED, reason=f"place failed: {exc}")
            self._release_reservation(order)
            await self.repo.save_order(order)
            logger.exception("entry order failed: %s", exc)
            return {
                "client_order_id": order.client_order_id,
                "status": order.status.value,
                "error": str(exc)[:500],
                "fills": [],
            }
        trades = await self._apply_and_persist(fills, order)
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
            self._release_reservation(order)
        await self.repo.save_order(order)
        return {
            "client_order_id": order.client_order_id,
            "exchange_order_id": order.exchange_order_id,
            "status": order.status.value,
            "type": order.order_type.value,
            "price": str(order.price),
            "quantity": str(order.quantity),
            "filled_quantity": str(order.filled_quantity),
            "avg_fill_price": str(order.avg_fill_price),
            "fee_tmn": str(order.fee_tmn),
            "fills": [fill.to_dict() for fill in fills],
            "closed_trades": trades,
        }

    async def execute_exit(
        self,
        *,
        position: Position,
        approval: RiskApproval,
        meta: MarketMeta,
        book: OrderBook | None,
        decision_id: str,
        reason: str,
        fee_rate: Any,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        client_id = self._client_id(position.symbol)
        order = Order(
            client_order_id=client_id,
            exchange_order_id=None,
            symbol=position.symbol,
            side=Side.SELL,
            order_type=OrderType(approval.order_type),
            price=approval.price,
            quantity=approval.quantity,
            status=OrderStatus.CREATED,
            decision_id=decision_id,
            reason=reason,
            raw_request={
                "exit_reason": reason,
                "entry_position": position.to_dict(),
            },
        )
        self.register_order(order)
        await self.repo.save_order(order)
        transition(order, OrderStatus.PENDING_SEND, reason=reason)
        try:
            fills = await _await_if_needed(
                self.broker.place(
                    order,
                    book=book or OrderBook(symbol=order.symbol),
                    meta=meta,
                    fee_rate=fee_rate,
                    now=now,
                )
            )
        except Exception as exc:  # noqa: BLE001
            transition(order, OrderStatus.REJECTED, reason=f"exit place failed: {exc}")
            await self.repo.save_order(order)
            logger.exception("exit order failed: %s", exc)
            return {
                "client_order_id": order.client_order_id,
                "status": order.status.value,
                "error": str(exc)[:500],
                "fills": [],
            }
        trades = await self._apply_and_persist(fills, order)
        await self.repo.save_order(order)
        return {
            "client_order_id": order.client_order_id,
            "exchange_order_id": order.exchange_order_id,
            "status": order.status.value,
            "type": order.order_type.value,
            "price": str(order.price),
            "quantity": str(order.quantity),
            "filled_quantity": str(order.filled_quantity),
            "avg_fill_price": str(order.avg_fill_price),
            "fee_tmn": str(order.fee_tmn),
            "fills": [fill.to_dict() for fill in fills],
            "closed_trades": trades,
        }

    async def maintain(
        self,
        books: dict[str, OrderBook],
        trades: dict[str, list[TradeTick]],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = now or datetime.now(UTC)
        closed: list[dict[str, Any]] = []
        if isinstance(self.broker, ShadowBroker):
            fills = self.broker.process_open_orders(books, trades, now=current)
        else:
            fills = await self.broker.process_open_orders(self.fee_rates)
        for fill in fills:
            order = self.orders.get(fill.order_id) or self.broker.orders.get(fill.order_id)
            if order is None:
                continue
            if fill.fill_id in self.applied_fills:
                continue
            self._convert_fee(fill, order)
            await self.repo.save_fill(fill)
            entry_meta = order.raw_request.get("entry_meta") if order.side == Side.BUY else None
            exit_reason = str(order.raw_request.get("exit_reason", order.reason or "exit"))
            try:
                trade = self.portfolio.apply_fill(
                    fill, entry_meta=entry_meta, exit_reason=exit_reason
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("portfolio apply_fill failed: %s", exc)
                continue
            self.applied_fills.add(fill.fill_id)
            if trade:
                closed.append(trade)
        await self._maintain_stale_orders(books, current)
        for order in self.orders.values():
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
                self._release_reservation(order)
                await self.repo.save_order(order)
        return closed

    async def _maintain_stale_orders(
        self, books: dict[str, OrderBook], now: datetime
    ) -> None:
        for order in list(self.broker.open_orders()):
            if order.age_sec < self.settings.order_timeout_sec:
                continue
            if order.filled_quantity > 0:
                await _await_if_needed(
                    self.broker.cancel(order.client_order_id, "timeout after partial fill")
                )
                self._release_reservation(order)
                await self.repo.save_order(order)
                continue
            book = books.get(order.symbol)
            best_bid = book.best_bid if book is not None else None
            can_reprice = (
                isinstance(self.broker, ShadowBroker)
                and order.side == Side.BUY
                and order.reprices < self.settings.max_reprices
                and best_bid is not None
            )
            await _await_if_needed(self.broker.cancel(order.client_order_id, "timeout"))
            self._release_reservation(order)
            await self.repo.save_order(order)
            if not can_reprice:
                continue
            assert best_bid is not None
            entry_meta = order.raw_request.get("entry_meta")
            if not entry_meta:
                continue
            new_id = self._client_id(order.symbol)
            replacement = Order(
                client_order_id=new_id,
                exchange_order_id=None,
                symbol=order.symbol,
                side=order.side,
                order_type=OrderType.LIMIT,
                price=best_bid,
                quantity=order.remaining,
                status=OrderStatus.OPEN,
                decision_id=order.decision_id,
                reason="reprice after timeout",
                reprices=order.reprices + 1,
                raw_request={"entry_meta": entry_meta, "repriced_from": order.client_order_id},
            )
            self.register_order(replacement)
            self.broker.orders[new_id] = replacement
            reserve = replacement.remaining * replacement.price * Decimal("1.01")
            self.reserved[new_id] = reserve
            self.portfolio.reserve_cash(reserve)
            await self.repo.save_order(replacement)
            logger.info("repriced %s -> %s", order.client_order_id, new_id)

    async def cancel_all(self, symbol: str | None = None) -> int:
        count = 0
        for order in self.open_orders(symbol):
            if await _await_if_needed(self.broker.cancel(order.client_order_id, "cancel_all")):
                self._release_reservation(order)
                await self.repo.save_order(order)
                count += 1
        return count

    async def cancel(self, client_order_id: str, reason: str) -> bool:
        order = self.orders.get(client_order_id) or self.broker.orders.get(client_order_id)
        if order is None:
            return False
        ok = await _await_if_needed(self.broker.cancel(client_order_id, reason))
        if ok:
            self._release_reservation(order)
            await self.repo.save_order(order)
        return ok

    async def _apply_and_persist(self, fills: list[Fill], order: Order) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        for fill in fills:
            if fill.fill_id in self.applied_fills:
                continue
            self._convert_fee(fill, order)
            await self.repo.save_fill(fill)
            entry_meta = order.raw_request.get("entry_meta") if order.side == Side.BUY else None
            exit_reason = str(order.raw_request.get("exit_reason", order.reason or "exit"))
            try:
                trade = self.portfolio.apply_fill(
                    fill, entry_meta=entry_meta, exit_reason=exit_reason
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("portfolio apply_fill failed: %s", exc)
                continue
            self.applied_fills.add(fill.fill_id)
            if trade:
                closed.append(trade)
        return closed

    def _convert_fee(self, fill: Fill, order: Order) -> None:
        if fill.fee_asset == "TMN":
            return
        # Wallex may charge the fee in the base asset (observed for PEPE/BNB
        # buys). Keep the base fee so the portfolio can credit the net quantity,
        # and also record its TMN value for reporting.
        base_asset = order.symbol[:-3] if order.symbol.endswith("TMN") else ""
        if fill.fee_asset == base_asset:
            fill.base_fee_quantity = fill.fee
            fill.fee = fill.fee * fill.price
        elif fill.fee_asset == "USDT":
            # TMN-quoted markets may report a stablecoin fee; treat it as TMN
            # one-to-one for accounting and leave an audit flag in the fill.
            fill.base_fee_quantity = ZERO
        fill.fee_asset = "TMN"

    def _release_reservation(self, order: Order) -> None:
        amount = self.reserved.pop(order.client_order_id, ZERO)
        if amount > 0:
            self.portfolio.release_cash(amount)

    def _client_id(self, symbol: str) -> str:
        self._seq += 1
        stamp = int(time.time()) % 100000
        raw = f"argus_{self.run_token[:6]}_{symbol}_{stamp:05d}_{self._seq:04d}"
        return re.sub(r"[^A-Za-z0-9_]", "", raw)[:48]


def order_to_snapshot(order: Order) -> dict[str, Any]:
    return json_safe(order.to_dict())


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def portfolio_execution_context(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    return {
        "equity_tmn": str(snapshot.equity_tmn),
        "cash_tmn": str(snapshot.cash_tmn),
        "open_positions": snapshot.open_positions,
        "drawdown_pct": snapshot.drawdown_pct,
    }
