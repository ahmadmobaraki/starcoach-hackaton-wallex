from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from wallex_trader.agent.client import AgentResult
from wallex_trader.agent.schemas import AgentAction, AgentDecision
from wallex_trader.config import Settings
from wallex_trader.marketdata.features import FeatureEngine
from wallex_trader.models import Candidate, PortfolioSnapshot
from wallex_trader.strategy.regime import RegimeState

logger = logging.getLogger("trader.ab_test")


class CachedDecisionAgent:
    """Replays stored AI decisions for a backtest, keyed by symbol + feature hash."""

    def __init__(self, settings: Settings, cache_path: str | Path) -> None:
        self.settings = settings
        self.cache_path = Path(cache_path)
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            logger.warning("decision cache not found: %s", self.cache_path)
            return
        with self.cache_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("action") != "BUY":
                    continue
                symbol = str(record.get("symbol", ""))
                features = record.get("features") or {}
                if not features:
                    continue
                key = (symbol, FeatureEngine.feature_hash(features))
                self.cache[key] = record
        logger.info("loaded %d cached decisions", len(self.cache))

    async def decide(
        self,
        candidate: Candidate,
        snapshot: PortfolioSnapshot,
        regime: RegimeState,
        *,
        position: dict[str, Any] | None = None,
        decision_id: str = "",
        market_context: dict[str, Any] | None = None,
    ) -> AgentResult:
        key = (candidate.symbol, FeatureEngine.feature_hash(candidate.features))
        record = self.cache.get(key)
        if record is None or position is not None:
            self.misses += 1
            decision = AgentDecision(
                action=AgentAction.HOLD,
                confidence=0.0,
                size_fraction=0.0,
                order_type="LIMIT",
                limit_offset_bps=0.0,
                take_profit_bps=self.settings.take_profit_bps,
                stop_loss_bps=self.settings.stop_loss_bps,
                horizon_sec=self.settings.time_stop_sec,
                regime=regime.label,
                rationale="HOLD: no cached AI decision for this feature snapshot",
                risk_flags=["cache_miss"],
            )
            return AgentResult(decision=decision, model="cached", response_hash=decision.response_hash())
        self.hits += 1
        agent = record.get("agent") or {}
        decision = AgentDecision(
            action=AgentAction.BUY,
            confidence=float(agent.get("confidence", 0.6)),
            size_fraction=float(agent.get("size_fraction", 1.0)),
            order_type=cast(
                Literal["LIMIT", "MARKETABLE_LIMIT"],
                str(agent.get("order_type", "MARKETABLE_LIMIT")),
            ),
            limit_offset_bps=float(agent.get("limit_offset_bps", 5.0)),
            take_profit_bps=float(agent.get("take_profit_bps", self.settings.take_profit_bps)),
            stop_loss_bps=float(agent.get("stop_loss_bps", self.settings.stop_loss_bps)),
            horizon_sec=int(agent.get("horizon_sec", self.settings.time_stop_sec)),
            regime=str(agent.get("regime", regime.label)),
            rationale=str(agent.get("rationale", "cached decision")),
            risk_flags=list(agent.get("risk_flags", [])),
        )
        return AgentResult(
            decision=decision,
            model="cached",
            response_hash=str(record.get("response_hash", "")),
        )

    async def close(self) -> None:
        return None

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total else 0.0,
            "cache_size": len(self.cache),
        }


class RiskOnlyAgent:
    """Deterministic baseline: trade every risk-approved candidate (no AI filter)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls = 0

    async def decide(
        self,
        candidate: Candidate,
        snapshot: PortfolioSnapshot,
        regime: RegimeState,
        *,
        position: dict[str, Any] | None = None,
        decision_id: str = "",
        market_context: dict[str, Any] | None = None,
    ) -> AgentResult:
        self.calls += 1
        decision = AgentDecision(
            action=AgentAction.BUY if position is None else AgentAction.HOLD,
            confidence=max(0.55, min(0.9, candidate.score)),
            size_fraction=1.0,
            order_type="MARKETABLE_LIMIT",
            limit_offset_bps=5.0,
            take_profit_bps=candidate.recommended_take_profit_bps,
            stop_loss_bps=candidate.recommended_stop_bps,
            horizon_sec=candidate.recommended_horizon_sec,
            regime=regime.label,
            rationale="baseline: trade every risk-approved candidate",
            risk_flags=[],
        )
        return AgentResult(decision=decision, model="risk_only", response_hash=decision.response_hash())

    async def close(self) -> None:
        return None


async def run_ablation(
    settings: Settings,
    symbols: list[str],
    start: str,
    end: str,
    *,
    decision_cache: str | None = None,
) -> dict[str, Any]:
    """Compare deterministic, risk-only, and cached-AI variants on the same data."""
    from wallex_trader.backtest.download import load_candles
    from wallex_trader.backtest.precompute import build_feature_frames
    from wallex_trader.backtest.simulator import BacktestSimulator

    frames = load_candles(settings, symbols, start, end)
    frames = {symbol: frame for symbol, frame in frames.items() if len(frame) >= 300}
    features, regime = build_feature_frames(frames, settings)
    prepared = (frames, features, regime)

    variants: dict[str, Any] = {}
    from wallex_trader.agent.client import DeterministicAgent

    for name, agent in (
        ("deterministic", DeterministicAgent(settings)),
        ("risk_only", RiskOnlyAgent(settings)),
        (
            "cached_ai",
            CachedDecisionAgent(settings, decision_cache) if decision_cache else None,
        ),
    ):
        if agent is None:
            continue
        simulator = BacktestSimulator(settings, symbols, agent=agent)
        result = await simulator.run(start, end, prepared=prepared)
        variants[name] = result.metrics
        await agent.close()

    out_dir = Path(settings.artifacts_dir) / "ablations" / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": f"{start} -> {end}",
        "symbols": symbols,
        "variants": variants,
        "cache": decision_cache,
        "config_hash": settings.config_hash(),
        "git_sha": settings.git_sha,
    }
    (out_dir / "ablation.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    lines = [
        "# ARGUS AI ablation",
        "",
        f"Period: {start} -> {end} · symbols: {', '.join(symbols)}",
        "",
        "| Variant | Net return % | Max DD % | Trades | Win rate | Profit factor | Turnover |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, metrics in variants.items():
        lines.append(
            f"| {name} | {metrics.get('net_return_pct', 0):.2f} | "
            f"{metrics.get('max_drawdown_pct', 0):.2f} | {metrics.get('trades', 0)} | "
            f"{metrics.get('win_rate', 0) * 100:.1f}% | {metrics.get('profit_factor', 0)} | "
            f"{metrics.get('turnover_x', 0)} |"
        )
    lines.append("")
    lines.append(
        "The AI-gated variant uses cached decisions from a shadow/live run; the risk-only "
        "variant trades every risk-approved candidate. Compare how many candidates the AI vetoed "
        "and whether vetoed candidates would have lost money."
    )
    (out_dir / "ablation.md").write_text("\n".join(lines), encoding="utf-8")
    return {"artifact_dir": str(out_dir), "variants": variants}


def decision_cache_key(symbol: str, features: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(features, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"{symbol}:{digest}"
