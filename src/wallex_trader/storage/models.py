from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), index=True)
    git_sha: Mapped[str] = mapped_column(String(40), default="no-git")
    config_hash: Mapped[str] = mapped_column(String(40), default="")
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="starting")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_symbol_ts", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    bid: Mapped[float] = mapped_column(Float, default=0)
    ask: Mapped[float] = mapped_column(Float, default=0)
    spread_bps: Mapped[float] = mapped_column(Float, default=0)
    microprice: Mapped[float] = mapped_column(Float, default=0)
    depth_json: Mapped[dict] = mapped_column(JSON, default=dict)


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trade_symbol_ts", "symbol", "ts"),
        UniqueConstraint("symbol", "trade_id", name="uq_trade_symbol_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    is_buy_order: Mapped[bool] = mapped_column(Boolean, default=False)
    taker_side: Mapped[str] = mapped_column(String(8), default="BUY")
    trade_id: Mapped[str | None] = mapped_column(String(80), nullable=True)


class FeatureRow(Base):
    __tablename__ = "features"
    __table_args__ = (Index("ix_feature_symbol_ts", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    feature_json: Mapped[dict] = mapped_column(JSON, default=dict)
    feature_version: Mapped[str] = mapped_column(String(20), default="v1")
    feature_hash: Mapped[str] = mapped_column(String(80), default="")


class DecisionRow(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decision_run_ts", "run_id", "ts"),
        Index("ix_decision_symbol_ts", "symbol", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(80), unique=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(String(10), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    signal: Mapped[str | None] = mapped_column(String(30), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="shadow")
    candidate_json: Mapped[dict] = mapped_column(JSON, default=dict)
    features_json: Mapped[dict] = mapped_column(JSON, default=dict)
    agent_json: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_json: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_json: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    prompt_hash: Mapped[str] = mapped_column(String(80), default="")
    response_hash: Mapped[str] = mapped_column(String(80), default="")
    latency_ms: Mapped[float] = mapped_column(Float, default=0)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_order_client_id"),
        Index("ix_order_run_ts", "run_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(80))
    exchange_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(24))
    price: Mapped[str] = mapped_column(String(40))
    quantity: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), index=True)
    filled_quantity: Mapped[str] = mapped_column(String(40), default="0")
    avg_fill_price: Mapped[str] = mapped_column(String(40), default="0")
    fee_tmn: Mapped[str] = mapped_column(String(40), default="0")
    decision_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fill_run_ts", "run_id", "ts"),)

    fill_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(80), index=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[str] = mapped_column(String(40))
    quantity: Mapped[str] = mapped_column(String(40))
    fee: Mapped[str] = mapped_column(String(40))
    fee_asset: Mapped[str] = mapped_column(String(16), default="TMN")
    decision_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    liquidity: Mapped[str] = mapped_column(String(16), default="taker")


class PositionSnapshotRow(Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_position_run_symbol_ts", "run_id", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    quantity: Mapped[str] = mapped_column(String(40))
    avg_entry: Mapped[str] = mapped_column(String(40))
    stop_price: Mapped[str] = mapped_column(String(40))
    take_profit_price: Mapped[str] = mapped_column(String(40))
    unrealized_pnl: Mapped[str] = mapped_column(String(40), default="0")
    realized_pnl: Mapped[str] = mapped_column(String(40), default="0")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class EquityRow(Base):
    __tablename__ = "equity"
    __table_args__ = (Index("ix_equity_run_ts", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cash_tmn: Mapped[str] = mapped_column(String(40))
    positions_value_tmn: Mapped[str] = mapped_column(String(40))
    equity_tmn: Mapped[str] = mapped_column(String(40))
    realized_pnl_tmn: Mapped[str] = mapped_column(String(40))
    unrealized_pnl_tmn: Mapped[str] = mapped_column(String(40))
    fees_tmn: Mapped[str] = mapped_column(String(40))
    gross_exposure_pct: Mapped[float] = mapped_column(Float)
    drawdown_pct: Mapped[float] = mapped_column(Float)
    daily_pnl_pct: Mapped[float] = mapped_column(Float)
    net_return_pct: Mapped[float] = mapped_column(Float)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskEventRow(Base):
    __tablename__ = "risk_events"
    __table_args__ = (Index("ix_risk_run_ts", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)


class LLMCallRow(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_run_ts", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), default="decision")
    model: Mapped[str] = mapped_column(String(80))
    prompt_hash: Mapped[str] = mapped_column(String(80), default="")
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentNoteRow(Base):
    __tablename__ = "agent_notes"
    __table_args__ = (Index("ix_note_run_ts", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="trade")
    outcome: Mapped[str] = mapped_column(String(16), default="neutral")
    note: Mapped[str] = mapped_column(Text)
    lesson: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[list] = mapped_column(JSON, default=list)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    pnl_tmn: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class SystemEventRow(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_run_ts", "run_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(12), default="info")
    component: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    artifact_path: Mapped[str] = mapped_column(String(300), default="")
    data_manifest_json: Mapped[dict] = mapped_column(JSON, default=dict)
    git_sha: Mapped[str] = mapped_column(String(40), default="no-git")


class ShadowRunRow(Base):
    __tablename__ = "shadow_runs"

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    artifact_path: Mapped[str] = mapped_column(String(300), default="")


class LLMCacheRow(Base):
    __tablename__ = "llm_cache"

    cache_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    model: Mapped[str] = mapped_column(String(80))
    prompt_hash: Mapped[str] = mapped_column(String(80))
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HeartbeatRow(Base):
    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    component: Mapped[str] = mapped_column(String(32))
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
