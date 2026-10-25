from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from wallex_trader.config import Settings
from wallex_trader.models import Fill, Order, PortfolioSnapshot
from wallex_trader.storage.db import Database
from wallex_trader.storage.models import (
    AgentNoteRow,
    BacktestRunRow,
    DecisionRow,
    EquityRow,
    FeatureRow,
    FillRow,
    HeartbeatRow,
    LLMCacheRow,
    LLMCallRow,
    MarketSnapshotRow,
    OrderRow,
    PositionSnapshotRow,
    RiskEventRow,
    RunRow,
    ShadowRunRow,
    SystemEventRow,
    TradeRow,
)


def json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class Repository:
    def __init__(self, db: Database, run_id: str) -> None:
        self.db = db
        self.run_id = run_id

    # ---- run lifecycle -------------------------------------------------
    async def start_run(self, settings: Settings, run_id: str | None = None) -> None:
        if run_id:
            self.run_id = run_id
        async with self.db.session() as session:
            stale = (
                await session.scalars(
                    select(RunRow).where(
                        RunRow.status == "running",
                        RunRow.run_id != self.run_id,
                        RunRow.mode == settings.mode.value,
                    )
                )
            ).all()
            for row in stale:
                row.status = "interrupted"
                row.ended_at = datetime.now(UTC)
            existing = await session.get(RunRow, self.run_id)
            if existing is None:
                session.add(
                    RunRow(
                        run_id=self.run_id,
                        mode=settings.mode.value,
                        git_sha=settings.git_sha or "no-git",
                        config_hash=settings.config_hash(),
                        config_json=json_safe(settings.safe_dump(redact=True)),
                        status="running",
                        started_at=datetime.now(UTC),
                        metadata_json={},
                    )
                )
            else:
                existing.status = "running"
                existing.ended_at = None

    async def finish_run(self, status: str = "finished") -> None:
        async with self.db.session() as session:
            row = await session.get(RunRow, self.run_id)
            if row:
                row.status = status
                row.ended_at = datetime.now(UTC)

    async def heartbeat(self, component: str, details: dict[str, Any] | None = None) -> None:
        async with self.db.session() as session:
            session.add(
                HeartbeatRow(
                    ts=datetime.now(UTC),
                    run_id=self.run_id,
                    component=component,
                    details_json=json_safe(details or {}),
                )
            )

    async def system_event(
        self,
        level: str,
        component: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                SystemEventRow(
                    run_id=self.run_id,
                    ts=datetime.now(UTC),
                    level=level,
                    component=component,
                    message=message,
                    context_json=json_safe(context or {}),
                )
            )

    async def risk_event(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                RiskEventRow(
                    run_id=self.run_id,
                    ts=datetime.now(UTC),
                    severity=severity,
                    code=code,
                    message=message,
                    evidence_json=json_safe(evidence or {}),
                )
            )

    # ---- market / features --------------------------------------------
    async def save_market_snapshot(
        self,
        ts: datetime,
        symbol: str,
        bid: float,
        ask: float,
        spread_bps: float,
        microprice: float,
        depth: dict[str, Any],
    ) -> None:
        async with self.db.session() as session:
            session.add(
                MarketSnapshotRow(
                    run_id=self.run_id,
                    ts=ts,
                    symbol=symbol,
                    bid=bid,
                    ask=ask,
                    spread_bps=spread_bps,
                    microprice=microprice,
                    depth_json=json_safe(depth),
                )
            )

    async def save_features(
        self,
        ts: datetime,
        symbol: str,
        features: dict[str, Any],
        feature_hash: str,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                FeatureRow(
                    run_id=self.run_id,
                    ts=ts,
                    symbol=symbol,
                    feature_json=json_safe(features),
                    feature_hash=feature_hash,
                )
            )

    async def save_trade(self, run_id: str, trade: dict[str, Any]) -> None:
        async with self.db.session() as session:
            if trade.get("trade_id"):
                existing = await session.scalar(
                    select(TradeRow).where(
                        TradeRow.symbol == trade["symbol"],
                        TradeRow.trade_id == str(trade["trade_id"]),
                    )
                )
                if existing:
                    return
            session.add(
                TradeRow(
                    run_id=run_id,
                    ts=datetime.fromisoformat(str(trade["ts"])),
                    symbol=str(trade["symbol"]),
                    price=float(trade["price"]),
                    quantity=float(trade["quantity"]),
                    is_buy_order=bool(trade.get("is_buy_order", False)),
                    taker_side=str(trade.get("taker_side", "BUY")),
                    trade_id=str(trade["trade_id"]) if trade.get("trade_id") else None,
                )
            )

    # ---- decisions -----------------------------------------------------
    async def save_decision(self, decision: dict[str, Any]) -> None:
        async with self.db.session() as session:
            existing = await session.scalar(
                select(DecisionRow).where(DecisionRow.decision_id == decision["decision_id"])
            )
            if existing:
                return
            session.add(
                DecisionRow(
                    decision_id=str(decision["decision_id"]),
                    run_id=str(decision.get("run_id", self.run_id)),
                    ts=decision["ts"],
                    symbol=str(decision["symbol"]),
                    action=str(decision["action"]),
                    confidence=float(decision.get("confidence", 0)),
                    signal=decision.get("signal"),
                    score=float(decision["score"]) if decision.get("score") is not None else None,
                    mode=str(decision.get("mode", "shadow")),
                    candidate_json=json_safe(decision.get("candidate", {})),
                    features_json=json_safe(decision.get("features", {})),
                    agent_json=json_safe(decision.get("agent", {})),
                    risk_json=json_safe(decision.get("risk", {})),
                    execution_json=json_safe(decision.get("execution", {})),
                    rationale=str(decision.get("rationale", "")),
                    prompt_hash=str(decision.get("prompt_hash", "")),
                    response_hash=str(decision.get("response_hash", "")),
                    latency_ms=float(decision.get("latency_ms", 0)),
                )
            )

    async def recent_decisions(
        self,
        run_id: str | None = None,
        *,
        symbol: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            stmt = select(DecisionRow).order_by(DecisionRow.ts.desc()).limit(limit)
            if run_id:
                stmt = stmt.where(DecisionRow.run_id == run_id)
            if symbol:
                stmt = stmt.where(DecisionRow.symbol == symbol)
            rows = (await session.scalars(stmt)).all()
        return [_decision_dict(row) for row in rows]

    async def recent_decisions_for_symbol(
        self, symbol: str, limit: int = 10, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.recent_decisions(run_id=run_id, symbol=symbol, limit=limit)

    # ---- orders / fills ------------------------------------------------
    async def save_order(self, order: Order) -> None:
        async with self.db.session() as session:
            row = await session.scalar(
                select(OrderRow).where(OrderRow.client_order_id == order.client_order_id)
            )
            if row is None:
                row = OrderRow(
                    client_order_id=order.client_order_id,
                    exchange_order_id=order.exchange_order_id,
                    run_id=self.run_id,
                    ts=order.created_at,
                    updated_at=order.updated_at,
                    symbol=order.symbol,
                    side=order.side.value,
                    order_type=order.order_type.value,
                    price=str(order.price),
                    quantity=str(order.quantity),
                    status=order.status.value,
                    filled_quantity=str(order.filled_quantity),
                    avg_fill_price=str(order.avg_fill_price),
                    fee_tmn=str(order.fee_tmn),
                    decision_id=order.decision_id,
                    reason=order.reason,
                    raw_json=json_safe(order.raw_response),
                )
                session.add(row)
            else:
                row.exchange_order_id = order.exchange_order_id
                row.updated_at = order.updated_at
                row.status = order.status.value
                row.filled_quantity = str(order.filled_quantity)
                row.avg_fill_price = str(order.avg_fill_price)
                row.fee_tmn = str(order.fee_tmn)
                row.reason = order.reason
                row.raw_json = json_safe(order.raw_response)

    async def save_fill(self, fill: Fill) -> None:
        async with self.db.session() as session:
            existing = await session.get(FillRow, fill.fill_id)
            if existing:
                return
            session.add(
                FillRow(
                    fill_id=fill.fill_id,
                    order_id=fill.order_id,
                    run_id=self.run_id,
                    ts=fill.ts,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    price=str(fill.price),
                    quantity=str(fill.quantity),
                    fee=str(fill.fee),
                    fee_asset=fill.fee_asset,
                    decision_id=fill.decision_id,
                    liquidity=fill.liquidity,
                )
            )

    # ---- portfolio -----------------------------------------------------
    async def save_position_snapshot(
        self,
        snapshot: PortfolioSnapshot,
        portfolio: Any,
        closed_symbols: Sequence[str] = (),
    ) -> None:
        # Closed positions must leave a zero-quantity tombstone row, otherwise
        # the newest row per symbol stays at the last open quantity and the
        # next live start resurrects a phantom position the exchange no longer
        # holds (incident 2026-10-23 14:21 flatten → restore → reconciliation
        # kill).
        tombstones = [
            symbol for symbol in closed_symbols if symbol not in portfolio.positions
        ]
        async with self.db.session() as session:
            for symbol, position in portfolio.positions.items():
                mark = portfolio.marks.get(symbol, position.avg_entry)
                session.add(
                    PositionSnapshotRow(
                        run_id=self.run_id,
                        ts=snapshot.ts,
                        symbol=symbol,
                        quantity=str(position.quantity),
                        avg_entry=str(position.avg_entry),
                        stop_price=str(position.stop_price),
                        take_profit_price=str(position.take_profit_price),
                        unrealized_pnl=str((mark - position.avg_entry) * position.quantity),
                        realized_pnl=str(position.realized_pnl_tmn),
                        metadata_json=json_safe(position.to_dict()),
                    )
                )
            for symbol in tombstones:
                session.add(
                    PositionSnapshotRow(
                        run_id=self.run_id,
                        ts=snapshot.ts,
                        symbol=symbol,
                        quantity="0",
                        avg_entry="0",
                        stop_price="0",
                        take_profit_price="0",
                        unrealized_pnl="0",
                        realized_pnl="0",
                        metadata_json={
                            "closed": True,
                            "opened_at": snapshot.ts.isoformat(),
                        },
                    )
                )

    async def save_equity(self, snapshot: PortfolioSnapshot) -> None:
        async with self.db.session() as session:
            session.add(
                EquityRow(
                    run_id=self.run_id,
                    ts=snapshot.ts,
                    cash_tmn=str(snapshot.cash_tmn),
                    positions_value_tmn=str(snapshot.positions_value_tmn),
                    equity_tmn=str(snapshot.equity_tmn),
                    realized_pnl_tmn=str(snapshot.realized_pnl_tmn),
                    unrealized_pnl_tmn=str(snapshot.unrealized_pnl_tmn),
                    fees_tmn=str(snapshot.fees_tmn),
                    gross_exposure_pct=snapshot.gross_exposure_pct,
                    drawdown_pct=snapshot.drawdown_pct,
                    daily_pnl_pct=snapshot.daily_pnl_pct,
                    net_return_pct=snapshot.net_return_pct,
                    snapshot_json=json_safe(snapshot.to_dict()),
                )
            )

    # ---- LLM -----------------------------------------------------------
    async def save_llm_call(
        self,
        *,
        model: str,
        prompt_hash: str,
        response: dict[str, Any],
        latency_ms: float,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        success: bool,
        kind: str = "decision",
        decision_id: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                LLMCallRow(
                    run_id=self.run_id,
                    ts=datetime.now(UTC),
                    decision_id=decision_id,
                    kind=kind,
                    model=model,
                    prompt_hash=prompt_hash,
                    response_json=json_safe(response),
                    latency_ms=latency_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    success=success,
                    error=error,
                )
            )

    async def llm_cache_get(self, cache_key: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(LLMCacheRow, cache_key)
            if row:
                return dict(row.response_json)
        return None

    async def llm_cache_put(
        self,
        cache_key: str,
        model: str,
        prompt_hash: str,
        response: dict[str, Any],
    ) -> None:
        async with self.db.session() as session:
            existing = await session.get(LLMCacheRow, cache_key)
            if existing:
                return
            session.add(
                LLMCacheRow(
                    cache_key=cache_key,
                    model=model,
                    prompt_hash=prompt_hash,
                    response_json=json_safe(response),
                    created_at=datetime.now(UTC),
                )
            )

    async def count_llm_calls_today(self) -> int:
        since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.db.session() as session:
            value = await session.scalar(
                select(func.count(LLMCallRow.id)).where(
                    LLMCallRow.run_id == self.run_id,
                    LLMCallRow.ts >= since,
                    LLMCallRow.kind == "decision",
                )
            )
        return int(value or 0)

    # ---- agent memory --------------------------------------------------
    async def add_agent_note(
        self,
        *,
        note: str,
        lesson: str = "",
        symbol: str | None = None,
        kind: str = "trade",
        outcome: str = "neutral",
        tags: list[str] | None = None,
        importance: float = 0.5,
        pnl_tmn: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                AgentNoteRow(
                    run_id=self.run_id,
                    ts=datetime.now(UTC),
                    symbol=symbol,
                    kind=kind,
                    outcome=outcome,
                    note=note,
                    lesson=lesson,
                    tags_json=tags or [],
                    importance=importance,
                    pnl_tmn=pnl_tmn,
                    metadata_json=json_safe(metadata or {}),
                )
            )

    async def get_agent_notes(
        self,
        *,
        symbol: str | None = None,
        limit: int = 6,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            stmt = select(AgentNoteRow).order_by(AgentNoteRow.ts.desc()).limit(max(limit * 4, 20))
            if run_id:
                stmt = stmt.where(AgentNoteRow.run_id == run_id)
            if symbol:
                stmt = stmt.where(
                    (AgentNoteRow.symbol == symbol) | (AgentNoteRow.symbol.is_(None))
                )
            rows = (await session.scalars(stmt)).all()
        notes = [_note_dict(row) for row in rows]
        notes.sort(key=lambda item: (item["importance"], item["ts"]), reverse=True)
        return notes[:limit]

    # ---- dashboard reads ----------------------------------------------
    async def latest_trading_run(self) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.scalar(
                select(RunRow)
                .where(RunRow.mode.in_(["shadow", "live"]))
                .order_by(RunRow.started_at.desc())
                .limit(1)
            )
        if row is None:
            return await self.latest_run()
        return _run_dict(row)

    async def latest_run(self, mode: str | None = None) -> dict[str, Any] | None:
        async with self.db.session() as session:
            stmt = select(RunRow).order_by(RunRow.started_at.desc()).limit(1)
            if mode:
                stmt = stmt.where(RunRow.mode == mode)
            row = await session.scalar(stmt)
        return _run_dict(row) if row else None

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(RunRow, run_id)
        return _run_dict(row) if row else None

    async def latest_equity(self, run_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.scalar(
                select(EquityRow)
                .where(EquityRow.run_id == run_id)
                .order_by(EquityRow.ts.desc())
                .limit(1)
            )
        return _equity_dict(row) if row else None

    async def equity_series(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(EquityRow)
                    .where(EquityRow.run_id == run_id)
                    .order_by(EquityRow.ts.desc())
                    .limit(limit)
                )
            ).all()
        return [_equity_dict(row) for row in reversed(rows)]

    async def latest_positions(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(PositionSnapshotRow)
                    .where(PositionSnapshotRow.run_id == run_id)
                    .order_by(PositionSnapshotRow.ts.desc())
                    .limit(limit)
                )
            ).all()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.symbol not in latest and Decimal(row.quantity) > 0:
                latest[row.symbol] = _position_dict(row)
        return list(latest.values())

    async def latest_positions_for_mode(self, mode: str) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(PositionSnapshotRow)
                    .join(RunRow, RunRow.run_id == PositionSnapshotRow.run_id)
                    .where(RunRow.mode == mode)
                    .order_by(PositionSnapshotRow.ts.desc())
                    .limit(500)
                )
            ).all()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.symbol not in latest:
                latest[row.symbol] = _position_dict(row)
        return list(latest.values())

    async def latest_positions_global(self) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(PositionSnapshotRow).order_by(PositionSnapshotRow.ts.desc()).limit(500)
                )
            ).all()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.symbol not in latest:
                latest[row.symbol] = _position_dict(row)
        return list(latest.values())

    async def list_orders(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(OrderRow)
                    .where(OrderRow.run_id == run_id)
                    .order_by(OrderRow.ts.desc())
                    .limit(limit)
                )
            ).all()
        return [_order_dict(row) for row in rows]

    async def list_fills(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(FillRow)
                    .where(FillRow.run_id == run_id)
                    .order_by(FillRow.ts.desc())
                    .limit(limit)
                )
            ).all()
        return [_fill_dict(row) for row in rows]

    async def list_risk_events(self, run_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(RiskEventRow)
                    .where(RiskEventRow.run_id == run_id)
                    .order_by(RiskEventRow.ts.desc())
                    .limit(limit)
                )
            ).all()
        return [_risk_dict(row) for row in rows]

    async def list_system_events(
        self, run_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            stmt = select(SystemEventRow).order_by(SystemEventRow.ts.desc()).limit(limit)
            if run_id:
                stmt = stmt.where(SystemEventRow.run_id == run_id)
            rows = (await session.scalars(stmt)).all()
        return [_system_dict(row) for row in rows]

    async def list_agent_notes(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            stmt = select(AgentNoteRow).order_by(AgentNoteRow.ts.desc()).limit(limit)
            if run_id:
                stmt = stmt.where(AgentNoteRow.run_id == run_id)
            rows = (await session.scalars(stmt)).all()
        return [_note_dict(row) for row in rows]

    async def latest_heartbeat(self, mode: str | None = None) -> dict[str, Any] | None:
        async with self.db.session() as session:
            stmt = select(HeartbeatRow)
            if mode:
                stmt = stmt.join(
                    RunRow, RunRow.run_id == HeartbeatRow.run_id
                ).where(RunRow.mode == mode)
            row = await session.scalar(stmt.order_by(HeartbeatRow.ts.desc()).limit(1))
        return _heartbeat_dict(row) if row else None

    async def overview(self, run_id: str | None = None) -> dict[str, Any]:
        run = await (self.get_run(run_id) if run_id else self.latest_trading_run())
        if not run:
            return {"run": None, "empty": True}
        active_run = run["run_id"]
        equity = await self.latest_equity(active_run)
        decisions = await self.recent_decisions(active_run, limit=20)
        notes = await self.list_agent_notes(active_run, limit=10)
        risk_events = await self.list_risk_events(active_run, limit=10)
        system_events = await self.list_system_events(active_run, limit=10)
        positions = await self.latest_positions(active_run)
        async with self.db.session() as session:
            decisions_count = await session.scalar(
                select(func.count(DecisionRow.id)).where(DecisionRow.run_id == active_run)
            )
            fills_count = await session.scalar(
                select(func.count(FillRow.fill_id)).where(FillRow.run_id == active_run)
            )
            llm_count = await session.scalar(
                select(func.count(LLMCallRow.id)).where(LLMCallRow.run_id == active_run)
            )
            llm_errors = await session.scalar(
                select(func.count(LLMCallRow.id)).where(
                    LLMCallRow.run_id == active_run, LLMCallRow.success.is_(False)
                )
            )
            note_count = await session.scalar(
                select(func.count(AgentNoteRow.id)).where(AgentNoteRow.run_id == active_run)
            )
        return {
            "run": run,
            "equity": equity,
            "positions": positions,
            "decisions": decisions,
            "notes": notes,
            "risk_events": risk_events,
            "system_events": system_events,
            "counts": {
                "decisions": int(decisions_count or 0),
                "fills": int(fills_count or 0),
                "llm_calls": int(llm_count or 0),
                "llm_errors": int(llm_errors or 0),
                "notes": int(note_count or 0),
                "open_positions": len(
                    [p for p in positions if float(p.get("quantity", 0)) > 0]
                ),
            },
            "heartbeat": await self.latest_heartbeat(),
        }

    # ---- backtests / shadow reports -----------------------------------
    async def save_backtest_run(
        self,
        *,
        run_id: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        artifact_path: str,
        data_manifest: dict[str, Any],
        git_sha: str,
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(BacktestRunRow, run_id)
            if row is None:
                row = BacktestRunRow(run_id=run_id, ts=datetime.now(UTC))
                session.add(row)
            row.config_json = json_safe(config)
            row.metrics_json = json_safe(metrics)
            row.artifact_path = artifact_path
            row.data_manifest_json = json_safe(data_manifest)
            row.git_sha = git_sha

    async def save_shadow_run(
        self,
        *,
        run_id: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        artifact_path: str,
    ) -> None:
        async with self.db.session() as session:
            row = await session.get(ShadowRunRow, run_id)
            if row is None:
                row = ShadowRunRow(run_id=run_id, ts=datetime.now(UTC))
                session.add(row)
            row.config_json = json_safe(config)
            row.metrics_json = json_safe(metrics)
            row.artifact_path = artifact_path

    async def list_backtests(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(BacktestRunRow).order_by(BacktestRunRow.ts.desc()).limit(limit)
                )
            ).all()
        return [_backtest_dict(row) for row in rows]

    async def get_backtest(self, run_id: str) -> dict[str, Any] | None:
        async with self.db.session() as session:
            row = await session.get(BacktestRunRow, run_id)
        return _backtest_dict(row) if row else None

    async def list_shadow_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.scalars(
                    select(ShadowRunRow).order_by(ShadowRunRow.ts.desc()).limit(limit)
                )
            ).all()
        return [_shadow_dict(row) for row in rows]

    # ---- export --------------------------------------------------------
    async def export_decisions_jsonl(self, run_id: str) -> str:
        rows = await self.recent_decisions(run_id, limit=10_000)
        return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)


# ---- serializers ------------------------------------------------------
def _run_dict(row: RunRow) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "mode": row.mode,
        "git_sha": row.git_sha,
        "config_hash": row.config_hash,
        "status": row.status,
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
        "config": row.config_json,
    }


def _equity_dict(row: EquityRow) -> dict[str, Any]:
    return {
        "ts": _iso(row.ts),
        "cash_tmn": row.cash_tmn,
        "positions_value_tmn": row.positions_value_tmn,
        "equity_tmn": row.equity_tmn,
        "realized_pnl_tmn": row.realized_pnl_tmn,
        "unrealized_pnl_tmn": row.unrealized_pnl_tmn,
        "fees_tmn": row.fees_tmn,
        "gross_exposure_pct": row.gross_exposure_pct,
        "drawdown_pct": row.drawdown_pct,
        "daily_pnl_pct": row.daily_pnl_pct,
        "net_return_pct": row.net_return_pct,
        "snapshot": row.snapshot_json,
    }


def _decision_dict(row: DecisionRow) -> dict[str, Any]:
    return {
        "decision_id": row.decision_id,
        "run_id": row.run_id,
        "ts": _iso(row.ts),
        "symbol": row.symbol,
        "action": row.action,
        "confidence": row.confidence,
        "signal": row.signal,
        "score": row.score,
        "mode": row.mode,
        "candidate": row.candidate_json,
        "features": row.features_json,
        "agent": row.agent_json,
        "risk": row.risk_json,
        "execution": row.execution_json,
        "rationale": row.rationale,
        "prompt_hash": row.prompt_hash,
        "response_hash": row.response_hash,
        "latency_ms": row.latency_ms,
    }


def _position_dict(row: PositionSnapshotRow) -> dict[str, Any]:
    data = dict(row.metadata_json or {})
    data.update(
        {
            "ts": _iso(row.ts),
            "symbol": row.symbol,
            "quantity": row.quantity,
            "avg_entry": row.avg_entry,
            "stop_price": row.stop_price,
            "take_profit_price": row.take_profit_price,
            "unrealized_pnl_tmn": row.unrealized_pnl,
            "realized_pnl_tmn": row.realized_pnl,
        }
    )
    return data


def _order_dict(row: OrderRow) -> dict[str, Any]:
    return {
        "client_order_id": row.client_order_id,
        "exchange_order_id": row.exchange_order_id,
        "ts": _iso(row.ts),
        "updated_at": _iso(row.updated_at),
        "symbol": row.symbol,
        "side": row.side,
        "order_type": row.order_type,
        "price": row.price,
        "quantity": row.quantity,
        "status": row.status,
        "filled_quantity": row.filled_quantity,
        "avg_fill_price": row.avg_fill_price,
        "fee_tmn": row.fee_tmn,
        "decision_id": row.decision_id,
        "reason": row.reason,
        "raw": row.raw_json,
    }


def _fill_dict(row: FillRow) -> dict[str, Any]:
    return {
        "fill_id": row.fill_id,
        "order_id": row.order_id,
        "ts": _iso(row.ts),
        "symbol": row.symbol,
        "side": row.side,
        "price": row.price,
        "quantity": row.quantity,
        "fee": row.fee,
        "fee_asset": row.fee_asset,
        "decision_id": row.decision_id,
        "liquidity": row.liquidity,
    }


def _risk_dict(row: RiskEventRow) -> dict[str, Any]:
    return {
        "ts": _iso(row.ts),
        "severity": row.severity,
        "code": row.code,
        "message": row.message,
        "evidence": row.evidence_json,
    }


def _system_dict(row: SystemEventRow) -> dict[str, Any]:
    return {
        "ts": _iso(row.ts),
        "level": row.level,
        "component": row.component,
        "message": row.message,
        "context": row.context_json,
    }


def _note_dict(row: AgentNoteRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": _iso(row.ts),
        "symbol": row.symbol,
        "kind": row.kind,
        "outcome": row.outcome,
        "note": row.note,
        "lesson": row.lesson,
        "tags": row.tags_json,
        "importance": row.importance,
        "pnl_tmn": row.pnl_tmn,
        "metadata": row.metadata_json,
    }


def _heartbeat_dict(row: HeartbeatRow) -> dict[str, Any]:
    return {
        "ts": _iso(row.ts),
        "run_id": row.run_id,
        "component": row.component,
        "details": row.details_json,
    }


def _backtest_dict(row: BacktestRunRow) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "ts": _iso(row.ts),
        "config": row.config_json,
        "metrics": row.metrics_json,
        "artifact_path": row.artifact_path,
        "data_manifest": row.data_manifest_json,
        "git_sha": row.git_sha,
    }


def _shadow_dict(row: ShadowRunRow) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "ts": _iso(row.ts),
        "config": row.config_json,
        "metrics": row.metrics_json,
        "artifact_path": row.artifact_path,
    }
