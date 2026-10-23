from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from wallex_trader.agent.client import DecisionAgent
from wallex_trader.agent.memory import AgentMemory
from wallex_trader.agent.reflection import ReflectionAgent
from wallex_trader.config import Settings
from wallex_trader.execution.oms import OMS
from wallex_trader.execution.shadow_broker import ShadowBroker
from wallex_trader.models import (
    BookLevel,
    Candidate,
    FeeRate,
    MarketMeta,
    OrderBook,
    Side,
)
from wallex_trader.risk.kernel import RiskKernel
from wallex_trader.risk.killswitch import KillSwitch
from wallex_trader.storage.db import create_database
from wallex_trader.storage.repositories import Repository, json_safe
from wallex_trader.strategy.portfolio import Portfolio
from wallex_trader.strategy.regime import RegimeState

logger = logging.getLogger("trader.selftest")


def _synthetic_setup(settings: Settings) -> tuple[Portfolio, MarketMeta, OrderBook, Candidate]:
    meta = MarketMeta(
        symbol="BTCTMN",
        base_asset="BTC",
        quote_asset="TMN",
        price_precision=0,
        amount_precision=8,
        price=100_000.0,
        min_notional=50_000.0,
    )
    mid = Decimal("100000")
    bids = [
        BookLevel(price=mid - Decimal(i * 5), quantity=Decimal("100")) for i in range(1, 11)
    ]
    asks = [
        BookLevel(price=mid + Decimal(25 + i * 5), quantity=Decimal("100")) for i in range(10)
    ]
    book = OrderBook(symbol="BTCTMN", bids=bids, asks=asks, source="selftest")
    portfolio = Portfolio(settings, Decimal(str(settings.initial_capital_tmn)))
    candidate = Candidate(
        symbol="BTCTMN",
        side=Side.BUY,
        signal="TREND",
        score=0.82,
        expected_edge_bps=180.0,
        spread_bps=1.0,
        top5_depth_tmn=50_000_000.0,
        features={
            "price": 100_000.0,
            "ret_1m": -0.004,
            "ret_5m": -0.009,
            "ret_15m": -0.012,
            "rsi_14": 28.0,
            "bb_z_20": -2.4,
            "atr_bps": 90.0,
            "rv_1m_bps": 22.0,
            "volume_z_60": 2.1,
            "book_imbalance_5": 0.62,
            "aggressive_buy_ratio": 0.68,
            "btc_ret_15m": 0.001,
            "spread_bps": 1.0,
            "top5_depth_tmn": 50_000_000.0,
        },
        regime="range",
        rationale="selftest: synthetic trend entry with bid support",
        expected_move_bps=250.0,
        recommended_stop_bps=1000.0,
        recommended_take_profit_bps=2500.0,
        recommended_horizon_sec=1_728_000,
    )
    return portfolio, meta, book, candidate


async def run_self_test(
    settings: Settings,
    *,
    include_ai: bool = False,
    include_reflection: bool = False,
) -> dict[str, Any]:
    """Deterministic shadow OMS/risk smoke test. Never sends a real order."""
    from wallex_trader.agent.schemas import AgentAction, AgentDecision

    db_path = settings.data_dir / "selftest.db"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix)
        if path.exists():
            path.unlink()
    selftest_settings = settings.model_copy(
        update={
            "postgres_dsn": f"sqlite+aiosqlite:///{db_path}",
            "journal_path": settings.artifacts_dir / "selftest" / "selftest_journal.md",
            "mode": settings.mode,
        }
    )
    selftest_settings.ensure_dirs()
    db = create_database(selftest_settings)
    await db.init()
    run_id = f"selftest_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    repo = Repository(db, run_id)
    await repo.start_run(selftest_settings, run_id)
    portfolio, meta, book, candidate = _synthetic_setup(selftest_settings)
    kill = KillSwitch(selftest_settings)
    risk = RiskKernel(selftest_settings, kill)
    broker = ShadowBroker(selftest_settings)
    fee = FeeRate(maker=selftest_settings.maker_fee, taker=selftest_settings.taker_fee)
    oms = OMS(selftest_settings, repo, portfolio, broker, {"BTCTMN": fee})
    now = datetime.now(UTC)
    decision = AgentDecision(
        action=AgentAction.BUY,
        confidence=0.78,
        size_fraction=0.8,
        order_type="MARKETABLE_LIMIT",
        limit_offset_bps=5.0,
        take_profit_bps=2500.0,
        stop_loss_bps=1000.0,
        horizon_sec=1_728_000,
        regime="range",
        rationale="selftest synthetic entry",
        risk_flags=[],
    )
    approval = risk.approve(
        decision,
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=fee,
        position=None,
        open_orders=[],
        now=now,
    )
    result: dict[str, Any] = {
        "run_id": run_id,
        "risk_approved": approval.approved,
        "risk_checks": approval.to_dict(),
        "decision": decision.model_dump(mode="json"),
    }
    if not approval.approved:
        await repo.finish_run("failed")
        await db.close()
        result["ok"] = False
        return result
    entry = await oms.execute_entry(
        decision=decision,
        candidate=candidate,
        approval=approval,
        meta=meta,
        book=book,
        decision_id="selftest_entry",
        fee_rate=fee,
        agent_payload={"model": "selftest", "confidence": 0.78},
    )
    result["entry"] = entry
    position = portfolio.positions.get("BTCTMN")
    result["position_after_entry"] = position.to_dict() if position else None
    if position is None:
        await repo.finish_run("failed")
        await db.close()
        result["ok"] = False
        return result
    exit_approval = risk.approve_exit(
        position,
        book,
        meta,
        reason="selftest_exit",
        fee_rate=fee,
        now=now,
        fallback_price=book.best_bid,
    )
    exit_result = await oms.execute_exit(
        position=position,
        approval=exit_approval,
        meta=meta,
        book=book,
        decision_id="selftest_exit",
        reason="selftest_exit",
        fee_rate=fee,
    )
    result["exit"] = exit_result
    snapshot = portfolio.snapshot(now)
    result["snapshot"] = snapshot.to_dict()
    result["trades"] = portfolio.recent_trades(10)
    result["ok"] = (
        entry.get("status") == "FILLED"
        and exit_result.get("status") == "FILLED"
        and len(result["trades"]) == 1
    )
    await repo.save_equity(snapshot)
    if include_ai:
        memory = AgentMemory(selftest_settings, repo)
        agent = DecisionAgent(selftest_settings, repo, memory=memory, mcp=None)
        try:
            agent_result = await agent.decide(
                candidate,
                snapshot,
                RegimeState(
                    label="range",
                    memo="selftest range regime with tight spread",
                    btc_ret_5m_bps=3.0,
                    btc_ret_15m_bps=8.0,
                    btc_ret_1h_bps=12.0,
                    volatility_percentile=55.0,
                    average_spread_bps=1.0,
                    btc_trend="up",
                    created_at=now,
                ),
                position=None,
                decision_id="selftest_ai_decision",
            )
            result["ai_decision"] = agent_result.decision.model_dump(mode="json")
            result["ai_meta"] = {
                "model": agent_result.model,
                "cached": agent_result.cached,
                "latency_ms": agent_result.latency_ms,
                "error": agent_result.error,
                "response_hash": agent_result.response_hash,
            }
        finally:
            await agent.close()
    if include_reflection and result["trades"]:
        memory = AgentMemory(selftest_settings, repo)
        reflection = ReflectionAgent(selftest_settings, repo, memory)
        try:
            note = await reflection.on_trade_closed(
                result["trades"][-1], snapshot, force=True
            )
            result["reflection"] = note
        finally:
            await reflection.close()
    await repo.finish_run("finished" if result["ok"] else "failed")
    await db.close()
    artifact_dir = Path(selftest_settings.artifacts_dir) / "selftest"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "last_selftest.json").write_text(
        json.dumps(json_safe(result), indent=2), encoding="utf-8"
    )
    return json_safe(result)


async def run_agent_test(settings: Settings) -> dict[str, Any]:
    """Call the DeepSeek decision agent on a synthetic candidate; no orders."""
    return await run_self_test(settings, include_ai=True, include_reflection=False)
