from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from wallex_trader.agent.client import DecisionAgent
from wallex_trader.agent.memory import AgentMemory
from wallex_trader.agent.schemas import AgentAction
from wallex_trader.config import Settings
from wallex_trader.models import Candidate, PortfolioSnapshot, Side
from wallex_trader.storage.db import create_database
from wallex_trader.storage.repositories import Repository, json_safe
from wallex_trader.strategy.regime import RegimeState

logger = logging.getLogger("trader.ai_review")


async def review_backtest_candidates(
    settings: Settings,
    decisions_path: str | Path,
    out_path: str | Path,
) -> dict[str, Any]:
    """Ask DeepSeek to review historical candidates, then emit a cache for ablation.

    The agent sees only the recorded feature snapshot, never the outcome. This is
    an offline AI-gating experiment on the same candidate stream as the backtest.
    """
    source = Path(decisions_path)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    records = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    buy_records = [record for record in records if record.get("action") == "BUY"]
    if not buy_records:
        raise ValueError("no BUY decisions to review")
    review_db = settings.data_dir / "ai_review.db"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(review_db) + suffix)
        if path.exists():
            path.unlink()
    review_settings = settings.model_copy(
        update={
            "postgres_dsn": f"sqlite+aiosqlite:///{review_db}",
            "agent_symbol_cooldown_sec": 0,
            "agent_max_calls_per_day": max(1000, len(buy_records) * 2),
            "agent_escalation_enabled": False,
            "mode": settings.mode,
        }
    )
    review_settings.ensure_dirs()
    db = create_database(review_settings)
    await db.init()
    run_id = f"aireview_{datetime.now(UTC):%Y%m%d_%H%M%S}"
    repo = Repository(db, run_id)
    await repo.start_run(review_settings, run_id)
    memory = AgentMemory(review_settings, repo)
    agent = DecisionAgent(review_settings, repo, memory=memory, mcp=None)
    cache_records: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    try:
        for index, record in enumerate(buy_records, 1):
            candidate = _candidate_from_record(record)
            ts = datetime.fromisoformat(str(record["ts"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            snapshot = _flat_snapshot(ts, review_settings.initial_capital_tmn)
            regime = _regime_from_record(record, ts)
            decision_id = f"aireview_{index:04d}"
            result = await agent.decide(
                candidate, snapshot, regime, position=None, decision_id=decision_id
            )
            review = {
                "decision_id": decision_id,
                "source_decision_id": record.get("decision_id"),
                "ts": record.get("ts"),
                "symbol": candidate.symbol,
                "signal": candidate.signal,
                "expected_edge_bps": candidate.expected_edge_bps,
                "action": result.decision.action.value,
                "confidence": result.decision.confidence,
                "size_fraction": result.decision.size_fraction,
                "order_type": result.decision.order_type,
                "take_profit_bps": result.decision.take_profit_bps,
                "stop_loss_bps": result.decision.stop_loss_bps,
                "horizon_sec": result.decision.horizon_sec,
                "rationale": result.decision.rationale,
                "risk_flags": result.decision.risk_flags,
                "model": result.model,
                "cached": result.cached,
                "error": result.error,
                "latency_ms": result.latency_ms,
                "response_hash": result.response_hash,
            }
            reviews.append(review)
            if result.decision.action == AgentAction.BUY:
                cache_records.append(
                    {
                        "symbol": candidate.symbol,
                        "features": candidate.features,
                        "action": "BUY",
                        "confidence": result.decision.confidence,
                        "decision_id": decision_id,
                        "response_hash": result.response_hash,
                        "agent": result.decision.model_dump(mode="json"),
                    }
                )
            logger.info(
                "AI review %d/%d %s -> %s (%.2f)",
                index,
                len(buy_records),
                candidate.symbol,
                result.decision.action,
                result.decision.confidence,
            )
    finally:
        await agent.close()
        await repo.finish_run("finished")
        await db.close()
    with target.open("w", encoding="utf-8") as handle:
        for record in cache_records:
            handle.write(json.dumps(json_safe(record), ensure_ascii=False) + "\n")
    review_path = target.with_name(target.stem + "_review.json")
    summary = {
        "source": str(source),
        "cache": str(target),
        "review": str(review_path),
        "candidates": len(buy_records),
        "ai_buy": sum(1 for item in reviews if item["action"] == "BUY"),
        "ai_hold": sum(1 for item in reviews if item["action"] == "HOLD"),
        "reviews": reviews,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    review_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def _candidate_from_record(record: dict[str, Any]) -> Candidate:
    candidate = record.get("candidate") or {}
    features = candidate.get("features") or record.get("features") or {}
    return Candidate(
        symbol=str(record["symbol"]),
        side=Side(str(candidate.get("side", "BUY"))),
        signal=str(candidate.get("signal", record.get("signal", "UNKNOWN"))),
        score=float(candidate.get("score", record.get("score", 0.0)) or 0.0),
        expected_edge_bps=float(candidate.get("expected_edge_bps", 0.0) or 0.0),
        spread_bps=float(candidate.get("spread_bps", 0.0) or 0.0),
        top5_depth_tmn=float(candidate.get("top5_depth_tmn", 0.0) or 0.0),
        features=features,
        regime=str(candidate.get("regime", "range")),
        rationale=str(candidate.get("rationale", "")),
        expected_move_bps=float(candidate.get("expected_move_bps", 0.0) or 0.0),
        recommended_stop_bps=float(candidate.get("recommended_stop_bps", 100.0) or 100.0),
        recommended_take_profit_bps=float(
            candidate.get("recommended_take_profit_bps", 200.0) or 200.0
        ),
        recommended_horizon_sec=int(candidate.get("recommended_horizon_sec", 1200) or 1200),
    )


def _flat_snapshot(ts: datetime, capital: float) -> PortfolioSnapshot:
    value = Decimal(str(capital))
    return PortfolioSnapshot(
        ts=ts,
        cash_tmn=value,
        positions_value_tmn=Decimal("0"),
        equity_tmn=value,
        realized_pnl_tmn=Decimal("0"),
        unrealized_pnl_tmn=Decimal("0"),
        fees_tmn=Decimal("0"),
        gross_exposure_pct=0.0,
        cash_buffer_pct=1.0,
        open_positions=0,
        portfolio_heat_pct=0.0,
        peak_equity_tmn=value,
        drawdown_pct=0.0,
        daily_pnl_pct=0.0,
        net_return_pct=0.0,
    )


def _regime_from_record(record: dict[str, Any], ts: datetime) -> RegimeState:
    candidate = record.get("candidate") or {}
    features = candidate.get("features") or record.get("features") or {}
    label = str(candidate.get("regime", "range"))
    return RegimeState(
        label=label,
        memo=(
            f"Historical replay regime {label}; only the feature snapshot is provided. "
            "No knowledge of the future outcome is available."
        ),
        btc_ret_5m_bps=float(features.get("btc_ret_5m") or 0.0) * 10_000,
        btc_ret_15m_bps=float(features.get("btc_ret_15m") or 0.0) * 10_000,
        btc_ret_1h_bps=float(features.get("btc_ret_1h") or 0.0) * 10_000,
        volatility_percentile=float(features.get("rv_percentile") or 50.0),
        average_spread_bps=float(features.get("spread_bps") or 0.0),
        btc_trend="up" if float(features.get("btc_ret_15m") or 0.0) >= 0 else "down",
        created_at=ts,
        source="historical_replay",
    )
