from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from wallex_trader.agent.client import AgentUnavailable, DeepSeekLLM
from wallex_trader.agent.memory import AgentMemory
from wallex_trader.agent.prompts import reflection_messages, session_messages
from wallex_trader.agent.schemas import (
    ReflectionNote,
    SessionReview,
    parse_json_object,
)
from wallex_trader.config import Settings
from wallex_trader.models import PortfolioSnapshot
from wallex_trader.storage.repositories import Repository
from wallex_trader.strategy.portfolio import Portfolio

logger = logging.getLogger("trader.reflection")


class ReflectionAgent:
    """Turns completed trades and session evidence into durable lessons."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        memory: AgentMemory,
        llm: DeepSeekLLM | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.memory = memory
        self.llm = llm or DeepSeekLLM(settings, repo)
        self.last_reflection_at = 0.0
        self.last_session_review_at = time.monotonic()
        self.trades_since_session_review = 0
        self._lock = asyncio.Lock()
        self.notes_written = 0
        self.failures = 0

    @property
    def reflection_model(self) -> str:
        return self.settings.agent_reflection_model or self.settings.deepseek_deep_model

    async def on_trade_closed(
        self,
        trade: dict[str, Any],
        snapshot: PortfolioSnapshot,
        *,
        force: bool = False,
    ) -> dict[str, Any] | None:
        if not self.settings.agent_reflection_enabled:
            return None
        now = time.monotonic()
        if not force and now - self.last_reflection_at < self.settings.agent_reflection_min_interval_sec:
            return None
        async with self._lock:
            if not force and now - self.last_reflection_at < self.settings.agent_reflection_min_interval_sec:
                return None
            self.last_reflection_at = now
            notes = await self.memory.recent_for_prompt(str(trade.get("symbol", "")), limit=8)
            outcome = "win" if bool(trade.get("win")) else "loss"
            if abs(float(trade.get("return_bps", 0.0))) < 5:
                outcome = "neutral"
            try:
                chat = await self.llm.chat(
                    reflection_messages(trade, _snapshot_payload(snapshot), notes),
                    model=self.reflection_model,
                    max_tokens=1800,
                    temperature=0.1,
                    json_mode=True,
                    kind="reflection",
                    prompt_hash="",
                )
                review = ReflectionNote.model_validate(parse_json_object(chat.content))
                await self.memory.add_note(
                    note=review.note,
                    lesson=review.lesson,
                    symbol=str(trade.get("symbol", "")) or None,
                    kind="trade",
                    outcome=review.outcome or outcome,
                    tags=list(dict.fromkeys([*review.tags, *review.avoid_patterns])),
                    importance=max(review.importance, _importance_from_trade(trade)),
                    pnl_tmn=float(trade.get("pnl_tmn", 0.0)),
                    metadata={
                        "trade": trade,
                        "confidence_calibration": review.confidence_calibration,
                        "suggested_adjustment": review.suggested_adjustment,
                        "model": chat.model,
                        "tokens_in": chat.tokens_in,
                        "tokens_out": chat.tokens_out,
                        "source": "deepseek_reflection",
                    },
                )
                self.notes_written += 1
                self.trades_since_session_review += 1
                logger.info(
                    "reflection stored for %s: %s",
                    trade.get("symbol"),
                    review.lesson[:120],
                )
                return review.model_dump(mode="json")
            except (AgentUnavailable, ValueError, json.JSONDecodeError) as exc:
                self.failures += 1
                logger.warning("reflection failed: %s", exc)
                lesson = (
                    f"Trade closed {outcome} with {float(trade.get('return_bps', 0)):+.0f} bps. "
                    "Reflection model unavailable; no learned lesson was recorded."
                )
                await self.memory.add_note(
                    note=(
                        f"{trade.get('symbol')} {trade.get('signal')} trade closed "
                        f"{outcome} after {float(trade.get('duration_sec', 0)) / 60:.0f} min. "
                        f"Agent rationale at entry: {str(trade.get('rationale', ''))[:300]}"
                    ),
                    lesson=lesson,
                    symbol=str(trade.get("symbol", "")) or None,
                    kind="trade",
                    outcome=outcome,
                    tags=["reflection_failed"],
                    importance=0.4,
                    pnl_tmn=float(trade.get("pnl_tmn", 0.0)),
                    metadata={"trade": trade, "error": str(exc)[:500]},
                )
                self.trades_since_session_review += 1
                return None
            finally:
                await self.llm.close()

    async def maybe_session_review(
        self,
        portfolio: Portfolio,
        snapshot: PortfolioSnapshot,
        decisions: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> dict[str, Any] | None:
        if not self.settings.agent_reflection_enabled:
            return None
        now = time.monotonic()
        due = now - self.last_session_review_at >= self.settings.agent_session_review_interval_sec
        if not force and not due:
            return None
        if not force and self.trades_since_session_review == 0:
            return None
        self.last_session_review_at = now
        summary = {
            "snapshot": _snapshot_payload(snapshot),
            "closed_trades_last_10": portfolio.recent_trades(10),
            "recent_decisions": decisions[:20],
            "win_rate": portfolio.win_rate(),
            "consecutive_losses": portfolio.consecutive_losses,
            "fees_tmn": str(portfolio.fees_tmn),
            "trade_count": portfolio.wins + portfolio.losses,
        }
        notes = await self.memory.recent_for_prompt(None, limit=10)
        try:
            chat = await self.llm.chat(
                session_messages(summary, notes),
                model=self.reflection_model,
                max_tokens=1800,
                temperature=0.1,
                json_mode=True,
                kind="session_review",
                prompt_hash="",
            )
            review = SessionReview.model_validate(parse_json_object(chat.content))
            await self.memory.add_note(
                note=review.summary,
                lesson=" | ".join(review.lessons[:5]),
                symbol=None,
                kind="session",
                outcome="neutral",
                tags=list(
                    dict.fromkeys([*review.best_patterns, *review.avoid_patterns])
                )[:12],
                importance=review.importance,
                metadata={
                    "session": summary,
                    "model": chat.model,
                    "best_patterns": review.best_patterns,
                    "avoid_patterns": review.avoid_patterns,
                    "source": "deepseek_session_review",
                },
            )
            self.notes_written += 1
            self.trades_since_session_review = 0
            logger.info("session review stored: %s", review.summary[:160])
            return review.model_dump(mode="json")
        except (AgentUnavailable, ValueError, json.JSONDecodeError) as exc:
            self.failures += 1
            logger.warning("session review failed: %s", exc)
            return None
        finally:
            await self.llm.close()

    async def close(self) -> None:
        await self.llm.close()

    def stats(self) -> dict[str, Any]:
        return {
            "notes_written": self.notes_written,
            "failures": self.failures,
            "trades_since_session_review": self.trades_since_session_review,
        }


def _snapshot_payload(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    return {
        "ts": snapshot.ts.isoformat(),
        "equity_tmn": float(snapshot.equity_tmn),
        "realized_pnl_tmn": float(snapshot.realized_pnl_tmn),
        "unrealized_pnl_tmn": float(snapshot.unrealized_pnl_tmn),
        "fees_tmn": float(snapshot.fees_tmn),
        "net_return_pct": round(snapshot.net_return_pct * 100, 3),
        "drawdown_pct": round(snapshot.drawdown_pct * 100, 3),
        "daily_pnl_pct": round(snapshot.daily_pnl_pct * 100, 3),
        "open_positions": snapshot.open_positions,
        "gross_exposure_pct": round(snapshot.gross_exposure_pct * 100, 2),
    }


def _importance_from_trade(trade: dict[str, Any]) -> float:
    magnitude = abs(float(trade.get("return_bps", 0.0)))
    return min(0.95, 0.35 + magnitude / 500.0)
