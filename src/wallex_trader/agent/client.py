from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from wallex_trader import PROMPT_VERSION
from wallex_trader.agent.prompts import build_decision_payload, decision_messages
from wallex_trader.agent.schemas import (
    AgentAction,
    AgentDecision,
    parse_json_object,
)
from wallex_trader.config import Settings
from wallex_trader.exchange.mcp import MCPError, WallexMCPClient
from wallex_trader.models import Candidate, PortfolioSnapshot
from wallex_trader.storage.repositories import Repository
from wallex_trader.strategy.regime import RegimeState

logger = logging.getLogger("trader.agent")


class AgentUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class ChatResult:
    content: str
    raw: dict[str, Any]
    model: str
    latency_ms: float
    tokens_in: int
    tokens_out: int
    prompt_hash: str
    error: str | None = None


@dataclass(slots=True)
class AgentResult:
    decision: AgentDecision
    raw_response: dict[str, Any] = field(default_factory=dict)
    prompt_payload: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    prompt_hash: str = ""
    response_hash: str = ""
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False
    escalated: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class DecisionAgentProtocol(Protocol):
    async def decide(
        self,
        candidate: Candidate,
        snapshot: PortfolioSnapshot,
        regime: RegimeState,
        *,
        position: dict[str, Any] | None = None,
        decision_id: str = "",
        market_context: dict[str, Any] | None = None,
    ) -> AgentResult: ...

    async def close(self) -> None: ...


class DeepSeekLLM:
    """Low-level OpenAI-compatible DeepSeek client."""

    def __init__(self, settings: Settings, repo: Repository | None = None) -> None:
        self.settings = settings
        self.repo = repo
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.agent_max_concurrent)
        self.calls = 0
        self.errors = 0
        self.latencies: list[float] = []
        self.tokens_in = 0
        self.tokens_out = 0

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=self.settings.deepseek_base_url.rstrip("/"),
            timeout=httpx.Timeout(max(30.0, self.settings.agent_timeout_sec + 20)),
            headers={
                "Authorization": f"Bearer {self.settings.deepseek_api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
        kind: str,
        decision_id: str | None = None,
        prompt_hash: str = "",
        attempts: int = 2,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        if not self.settings.has_deepseek_key:
            raise AgentUnavailable("DEEPSEEK_API_KEY is not configured")
        await self.connect()
        assert self._client is not None
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        effort = reasoning_effort
        if effort is None:
            effort = (
                self.settings.deepseek_deep_reasoning_effort
                if model == self.settings.deepseek_deep_model
                else self.settings.deepseek_reasoning_effort
            )
        if effort:
            payload["reasoning_effort"] = effort

        last_error: Exception | None = None
        for attempt in range(attempts):
            async with self._semaphore:
                started = time.perf_counter()
                try:
                    response = await self._client.post("/chat/completions", json=payload)
                    latency_ms = (time.perf_counter() - started) * 1000
                    self.calls += 1
                    self.latencies.append(latency_ms)
                    self.latencies = self.latencies[-500:]
                    if response.status_code >= 400:
                        raise AgentUnavailable(
                            f"DeepSeek HTTP {response.status_code}: {response.text[:400]}"
                        )
                    raw = response.json()
                    choice = (raw.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    content = str(message.get("content") or "").strip()
                    usage = raw.get("usage") or {}
                    tokens_in = int(usage.get("prompt_tokens") or 0)
                    tokens_out = int(usage.get("completion_tokens") or 0)
                    self.tokens_in += tokens_in
                    self.tokens_out += tokens_out
                    result = ChatResult(
                        content=content,
                        raw=raw,
                        model=str(raw.get("model") or model),
                        latency_ms=latency_ms,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        prompt_hash=prompt_hash,
                    )
                    if self.repo is not None:
                        await self.repo.save_llm_call(
                            model=result.model,
                            prompt_hash=prompt_hash,
                            response={
                                "prompt": messages,
                                "content": content,
                                "raw": raw,
                            },
                            latency_ms=latency_ms,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            cost_usd=0.0,
                            success=bool(content),
                            kind=kind,
                            decision_id=decision_id,
                            error=None if content else "empty content",
                        )
                    if not content:
                        raise AgentUnavailable("DeepSeek returned empty content")
                    return result
                except (httpx.TransportError, AgentUnavailable, ValueError) as exc:
                    self.errors += 1
                    last_error = exc
                    if "empty content" in str(exc):
                        payload["max_tokens"] = max(int(payload["max_tokens"]) * 2, 2400)
                        payload["reasoning_effort"] = "low"
                    if self.repo is not None:
                        await self.repo.save_llm_call(
                            model=model,
                            prompt_hash=prompt_hash,
                            response={},
                            latency_ms=(time.perf_counter() - started) * 1000,
                            tokens_in=0,
                            tokens_out=0,
                            cost_usd=0.0,
                            success=False,
                            kind=kind,
                            decision_id=decision_id,
                            error=str(exc)[:1000],
                        )
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0.7 * (attempt + 1))
        raise AgentUnavailable(f"DeepSeek call failed: {last_error}")

    def stats(self) -> dict[str, Any]:
        latencies = sorted(self.latencies)
        return {
            "calls": self.calls,
            "errors": self.errors,
            "success_rate": (self.calls - self.errors) / self.calls if self.calls else 0.0,
            "latency_p50_ms": round(latencies[len(latencies) // 2], 1) if latencies else 0.0,
            "latency_p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 1)
            if latencies
            else 0.0,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }


class DecisionAgent:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        *,
        mcp: WallexMCPClient | None = None,
        memory: Any | None = None,
        llm: DeepSeekLLM | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.mcp = mcp
        self.memory = memory
        self.llm = llm or DeepSeekLLM(settings, repo)
        self._symbol_last_call: dict[str, float] = {}
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._calls_budget_cache: tuple[float, int] | None = None

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
        now = time.monotonic()
        if now < self._circuit_open_until:
            return self._hold("circuit breaker open", decision_id)
        last_call = self._symbol_last_call.get(candidate.symbol, 0.0)
        if now - last_call < self.settings.agent_symbol_cooldown_sec:
            return self._hold("symbol cooldown", decision_id)
        if not await self._budget_available():
            return self._hold("daily LLM budget exhausted", decision_id)
        try:
            notes = (
                await self.memory.recent_for_prompt(
                    candidate.symbol, limit=self.settings.agent_notes_in_prompt
                )
                if self.memory is not None
                else []
            )
            recent = await self.repo.recent_decisions_for_symbol(candidate.symbol, limit=10)
            risk_limits = self._risk_limits(snapshot, candidate)
            payload = build_decision_payload(
                decision_id=decision_id,
                candidate=candidate,
                portfolio=self._portfolio_payload(snapshot),
                regime_memo=regime.memo,
                risk_limits=risk_limits,
                notes=notes,
                recent_decisions=recent,
                position=position,
                market_context=market_context,
            )
            prompt_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
            prompt_hash = hashlib.sha256(prompt_json.encode()).hexdigest()
            cache_key = hashlib.sha256(
                (
                    self.settings.deepseek_model
                    + str(self.settings.agent_temperature)
                    + PROMPT_VERSION
                    + prompt_json
                ).encode()
            ).hexdigest()
            cached = await self.repo.llm_cache_get(cache_key)
            if cached and cached.get("decision"):
                decision = AgentDecision.model_validate(cached["decision"])
                return AgentResult(
                    decision=decision,
                    raw_response=cached.get("raw", {}),
                    prompt_payload=payload,
                    model=self.settings.deepseek_model,
                    prompt_hash=prompt_hash,
                    response_hash=decision.response_hash(),
                    cached=True,
                    latency_ms=0.0,
                )

            messages = decision_messages(payload)
            result = await self._call_and_validate(messages, decision_id, prompt_hash)
            if result.decision.action != AgentAction.HOLD and not (
                0.45 <= result.decision.confidence <= 0.60
            ):
                pass
            elif (
                self.settings.agent_escalation_enabled
                and self.mcp is not None
                and 0.45 <= result.decision.confidence <= 0.60
            ):
                escalated = await self._escalate(
                    candidate, payload, result, decision_id, prompt_hash
                )
                if escalated is not None:
                    result = escalated

            await self.repo.llm_cache_put(
                cache_key,
                result.model,
                prompt_hash,
                {
                    "decision": result.decision.model_dump(mode="json"),
                    "raw": result.raw_response,
                },
            )
            self._symbol_last_call[candidate.symbol] = now
            self._failure_count = 0
            return result
        except (AgentUnavailable, ValueError, json.JSONDecodeError, MCPError) as exc:
            self._failure_count += 1
            if self._failure_count >= 5:
                self._circuit_open_until = now + 120
                self._failure_count = 0
            logger.warning("agent unavailable: %s", exc)
            return self._hold(f"agent unavailable: {exc}", decision_id)

    async def _call_and_validate(
        self,
        messages: list[dict[str, str]],
        decision_id: str,
        prompt_hash: str,
    ) -> AgentResult:
        chat = await self.llm.chat(
            messages,
            model=self.settings.deepseek_model,
            max_tokens=self.settings.agent_max_tokens,
            temperature=self.settings.agent_temperature,
            json_mode=True,
            kind="decision",
            decision_id=decision_id,
            prompt_hash=prompt_hash,
        )
        try:
            decision = AgentDecision.model_validate(parse_json_object(chat.content))
        except (ValueError, json.JSONDecodeError) as validation_error:
            retry_messages = [
                *messages,
                {"role": "assistant", "content": chat.content[:4000]},
                {
                    "role": "user",
                    "content": (
                        "Your response failed validation: "
                        f"{validation_error}. Return one corrected JSON object only, "
                        "matching the schema exactly."
                    ),
                },
            ]
            retry = await self.llm.chat(
                retry_messages,
                model=self.settings.deepseek_model,
                max_tokens=self.settings.agent_max_tokens,
                temperature=0.0,
                json_mode=True,
                kind="decision_retry",
                decision_id=decision_id,
                prompt_hash=prompt_hash,
            )
            decision = AgentDecision.model_validate(parse_json_object(retry.content))
            chat = retry
        return AgentResult(
            decision=decision,
            raw_response=chat.raw,
            model=chat.model,
            prompt_hash=chat.prompt_hash,
            response_hash=decision.response_hash(),
            latency_ms=chat.latency_ms,
            tokens_in=chat.tokens_in,
            tokens_out=chat.tokens_out,
        )

    async def _escalate(
        self,
        candidate: Candidate,
        payload: dict[str, Any],
        original: AgentResult,
        decision_id: str,
        prompt_hash: str,
    ) -> AgentResult | None:
        if self.mcp is None:
            return None
        tool_calls: list[dict[str, Any]] = []
        context: dict[str, Any] = {}
        try:
            depth = await self.mcp.call_tool(
                "wallex_basic_get_market_depth", {"symbol": candidate.symbol}
            )
            trades = await self.mcp.call_tool(
                "wallex_basic_get_latest_trades", {"symbol": candidate.symbol}
            )
            context["mcp_market_depth"] = _compact_depth(depth)
            context["mcp_latest_trades"] = _compact_trades(trades)
            tool_calls.extend(self.mcp.tool_log[-2:])
        except MCPError as exc:
            logger.warning("MCP escalation failed: %s", exc)
            context["mcp_error"] = str(exc)
        if not context:
            return None
        escalated_payload = {**payload, "market_context": context}
        messages = decision_messages(escalated_payload)
        result = await self._call_and_validate(messages, decision_id, prompt_hash)
        result.escalated = True
        result.tool_calls = tool_calls
        result.prompt_payload = escalated_payload
        return result

    async def _budget_available(self) -> bool:
        now = time.monotonic()
        if self._calls_budget_cache and now - self._calls_budget_cache[0] < 60:
            return self._calls_budget_cache[1] < self.settings.agent_max_calls_per_day
        count = await self.repo.count_llm_calls_today()
        self._calls_budget_cache = (now, count)
        return count < self.settings.agent_max_calls_per_day

    def _risk_limits(
        self, snapshot: PortfolioSnapshot, candidate: Candidate | None = None
    ) -> dict[str, Any]:
        equity = float(snapshot.equity_tmn) if snapshot.equity_tmn > 0 else 1.0
        minimum_size = (
            self.settings.agent_min_size_fraction
            if candidate is not None and candidate.signal == "TREND"
            else 0.15
        )
        return {
            "max_size_fraction": min(
                1.0, self.settings.max_order_notional_tmn / max(1.0, equity)
            ),
            "max_notional_tmn": self.settings.max_order_notional_tmn,
            "min_confidence": self.settings.agent_min_confidence,
            "max_spread_bps": self.settings.max_spread_bps,
            "max_position_pct": self.settings.max_position_pct,
            "min_cash_buffer": self.settings.min_cash_buffer,
            "max_stop_bps": self.settings.max_stop_bps,
            "max_take_profit_bps": self.settings.max_take_profit_bps,
            "max_horizon_sec": 2_592_000,
            "min_size_fraction": minimum_size,
            "min_notional_tmn": self.settings.min_notional_tmn,
        }

    @staticmethod
    def _portfolio_payload(snapshot: PortfolioSnapshot) -> dict[str, Any]:
        return {
            "equity_tmn": float(snapshot.equity_tmn),
            "cash_tmn": float(snapshot.cash_tmn),
            "open_positions": snapshot.open_positions,
            "gross_exposure_pct": round(snapshot.gross_exposure_pct * 100, 2),
            "portfolio_heat_pct": round(snapshot.portfolio_heat_pct * 100, 2),
            "daily_pnl_pct": round(snapshot.daily_pnl_pct * 100, 2),
            "drawdown_pct": round(snapshot.drawdown_pct * 100, 2),
            "net_return_pct": round(snapshot.net_return_pct * 100, 2),
            "fees_tmn": float(snapshot.fees_tmn),
        }

    def _hold(self, reason: str, decision_id: str) -> AgentResult:
        decision = AgentDecision(
            action=AgentAction.HOLD,
            confidence=0.0,
            size_fraction=0.0,
            order_type="LIMIT",
            limit_offset_bps=0.0,
            take_profit_bps=self.settings.take_profit_bps,
            stop_loss_bps=self.settings.stop_loss_bps,
            horizon_sec=self.settings.time_stop_sec,
            regime="unknown",
            rationale=f"HOLD: {reason}"[:600],
            risk_flags=["agent_unavailable"],
        )
        return AgentResult(
            decision=decision,
            model=self.settings.deepseek_model,
            response_hash=decision.response_hash(),
            error=reason,
            prompt_payload={"decision_id": decision_id},
        )

    async def close(self) -> None:
        await self.llm.close()


class DeterministicAgent:
    """Cost-free baseline used for backtests and ablation."""

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
        size = min(1.0, max(0.25, regime.size_multiplier()))
        decision = AgentDecision(
            action=AgentAction.BUY if position is None else AgentAction.HOLD,
            confidence=round(min(0.95, 0.55 + candidate.score * 0.4), 4),
            size_fraction=size,
            order_type="MARKETABLE_LIMIT",
            limit_offset_bps=5.0,
            take_profit_bps=candidate.recommended_take_profit_bps,
            stop_loss_bps=candidate.recommended_stop_bps,
            horizon_sec=candidate.recommended_horizon_sec,
            regime=regime.label,
            rationale=candidate.rationale[:600],
            risk_flags=[],
        )
        return AgentResult(
            decision=decision,
            prompt_payload={"decision_id": decision_id, "mode": "deterministic"},
            model="deterministic",
            response_hash=decision.response_hash(),
            latency_ms=0.0,
        )

    async def close(self) -> None:
        return None


def _compact_depth(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"raw": str(payload)[:500]}
    bids = payload.get("bid") or payload.get("bids") or []
    asks = payload.get("ask") or payload.get("asks") or []
    return {
        "top_bids": bids[:5] if isinstance(bids, list) else bids,
        "top_asks": asks[:5] if isinstance(asks, list) else asks,
    }


def _compact_trades(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        items = payload.get("latestTrades") or payload.get("trades") or payload.get("message") or []
    else:
        items = payload
    if isinstance(items, list):
        return items[:10]
    return [items]
