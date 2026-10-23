from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from wallex_trader.agent.client import (
    AgentResult,
    DecisionAgent,
    DecisionAgentProtocol,
    DeepSeekLLM,
    DeterministicAgent,
)
from wallex_trader.agent.memory import AgentMemory
from wallex_trader.agent.reflection import ReflectionAgent
from wallex_trader.agent.schemas import AgentAction, AgentDecision
from wallex_trader.config import Settings
from wallex_trader.exchange.mcp import MCPError, WallexMCPClient
from wallex_trader.exchange.rest import WallexAPIError, WallexRESTClient
from wallex_trader.execution.live_broker import LiveBroker
from wallex_trader.execution.oms import OMS
from wallex_trader.execution.reconciler import Reconciler
from wallex_trader.execution.shadow_broker import ShadowBroker
from wallex_trader.logging import setup_logging
from wallex_trader.marketdata.features import FeatureEngine
from wallex_trader.marketdata.feed import MarketDataFeed
from wallex_trader.models import (
    Candidate,
    FeeRate,
    MarketMeta,
    OrderBook,
    Position,
    Side,
)
from wallex_trader.risk.kernel import RiskKernel
from wallex_trader.risk.killswitch import KillSwitch
from wallex_trader.storage.db import create_database
from wallex_trader.storage.repositories import Repository, json_safe
from wallex_trader.strategy.exits import evaluate_signal_exit, update_trailing_stop
from wallex_trader.strategy.portfolio import Portfolio
from wallex_trader.strategy.regime import RegimeDetector, RegimeState
from wallex_trader.strategy.signals import SignalEngine
from wallex_trader.strategy.swing import SwingSignalEngine
from wallex_trader.strategy.universe import UniverseSelector

logger = logging.getLogger("trader.engine")


class DisabledAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def decide(
        self,
        candidate: Candidate,
        snapshot: Any,
        regime: RegimeState,
        *,
        position: dict[str, Any] | None = None,
        decision_id: str = "",
        market_context: dict[str, Any] | None = None,
    ) -> AgentResult:
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
            rationale="HOLD: AI agent disabled and deterministic fallback not allowed",
            risk_flags=["agent_disabled"],
        )
        return AgentResult(decision=decision, model="disabled", response_hash=decision.response_hash())

    async def close(self) -> None:
        return None


class TradingEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = setup_logging(settings, component="trader")
        self.db = create_database(settings)
        self.run_id = self._make_run_id()
        self.repo = Repository(self.db, self.run_id)
        self.rest = WallexRESTClient(settings)
        self.mcp: WallexMCPClient | None = None
        self.feed: MarketDataFeed | None = None
        self.portfolio: Portfolio | None = None
        self.broker: ShadowBroker | LiveBroker | None = None
        self.oms: OMS | None = None
        self.risk: RiskKernel | None = None
        self.agent: DecisionAgentProtocol | None = None
        self.memory: AgentMemory | None = None
        self.reflection: ReflectionAgent | None = None
        self.reconciler: Reconciler | None = None
        self.kill = KillSwitch(settings)
        self.feature_engine = FeatureEngine(settings)
        self.signal_engine = SignalEngine(settings)
        self.swing_engine = SwingSignalEngine(settings)
        self.regime_detector = RegimeDetector(settings)
        self.universe_selector = UniverseSelector(settings)
        self.metadatas: dict[str, MarketMeta] = {}
        self.symbols: list[str] = []
        self.fee_rates: dict[str, FeeRate] = {}
        self.regime: RegimeState | None = None
        self.ai_memo = ""
        self.ai_regime_label = ""
        self.last_regime_at = 0.0
        self.latest_snapshot: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._started = False
        self._decision_busy = False
        self._stopped = False
        self._ws_down_since: float | None = None
        self._kill_handled = False
        self._reconcile_failures = 0
        self._endgame_flattened = False
        self._decision_seq = 0
        self._review_lock = asyncio.Lock()
        self._last_strategy_check: dict[str, float] = {}
        self._last_swing_bar: dict[str, datetime] = {}
        self.last_scan_at: datetime | None = None
        self.last_candidate_count = 0
        self.last_scan_candidates: list[str] = []
        self._review_symbols: set[str] = set()
        self._reflection_tasks: set[asyncio.Task[Any]] = set()
        self.memo_llm: DeepSeekLLM | None = None

    # ------------------------------------------------------------------
    def _make_run_id(self) -> str:
        configured = (self.settings.run_id or "").strip()
        if configured and configured not in ("argus-local", "local"):
            return configured
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
        return f"{self.settings.mode.value}_{stamp}_{suffix}"

    async def start(self, *, confirm_live: bool = False) -> None:
        if self.settings.is_live and not confirm_live:
            raise RuntimeError("live mode requires --confirm-live")
        if self.settings.is_live and self.settings.live_confirm != "I_UNDERSTAND_REAL_MONEY":
            raise RuntimeError("live mode requires LIVE_CONFIRM in .env")
        self.settings.ensure_dirs()
        await self.db.init()
        await self.repo.start_run(self.settings, self.run_id)
        try:
            await self.rest.connect()
            self.metadatas = await self.rest.market_metadata()
            pre_books: dict[str, OrderBook] = {}
            for symbol in self.settings.universe_or_default():
                if symbol not in self.metadatas:
                    continue
                try:
                    pre_books[symbol] = await self.rest.depth(symbol, limit=50)
                except WallexAPIError as exc:
                    logger.warning("initial depth failed for %s: %s", symbol, exc)
            universe = self.universe_selector.select(
                self.metadatas, pre_books if pre_books else None
            )
            self.symbols = [meta.symbol for meta in universe.selected]
            if not self.symbols:
                raise RuntimeError(f"universe selection failed: {universe.excluded}")
            fees: dict[str, FeeRate] = {}
            if self.settings.has_wallex_key:
                try:
                    fees = await self.rest.fees()
                except WallexAPIError as exc:
                    logger.warning("fee fetch failed; using configured defaults: %s", exc)
            self.fee_rates = {
                symbol: fees.get(
                    symbol, FeeRate(self.settings.maker_fee, self.settings.taker_fee)
                )
                for symbol in self.symbols
            }
            self.feed = MarketDataFeed(self.settings, self.rest, self.symbols)
            await self.feed.start(seed_days=2)
            await self._init_broker_and_portfolio()
            self.risk = RiskKernel(self.settings, self.kill)
            if self.portfolio is None or self.broker is None:
                raise RuntimeError("portfolio/broker initialization failed")
            self.oms = OMS(
                self.settings,
                self.repo,
                self.portfolio,
                self.broker,
                self.fee_rates,
            )
            self.reconciler = Reconciler(self.broker, self.portfolio)
            self.memory = AgentMemory(self.settings, self.repo)
            await self._init_agents()
            if self.kill.file_requested():
                self.kill.check_file()
                await self._activate_kill(
                    self.kill.state.reason or "kill switch file present",
                    trigger="file",
                )
            now = datetime.now(UTC)
            self.regime = self.regime_detector.detect(
                self.feed.candles, self.feed.books_map(), now
            )
            self.latest_snapshot = self.portfolio.snapshot(now)
            await self.repo.system_event(
                "info",
                "engine",
                "ARGUS engine started",
                {
                    "run_id": self.run_id,
                    "mode": self.settings.mode.value,
                    "symbols": self.symbols,
                    "fee_rates": {
                        symbol: {"maker": rate.maker, "taker": rate.taker}
                        for symbol, rate in self.fee_rates.items()
                    },
                    "git_sha": self.settings.git_sha,
                    "config_hash": self.settings.config_hash(),
                    "agent": type(self.agent).__name__ if self.agent else "none",
                    "agent_model": self.settings.deepseek_model,
                    "equity_tmn": str(self.latest_snapshot.equity_tmn),
                    "kill_switch": self.kill.state.to_dict(),
                },
            )
            self.log.info(
                "engine started",
                extra={
                    "context": {
                        "run_id": self.run_id,
                        "mode": self.settings.mode.value,
                        "symbols": self.symbols,
                        "equity_tmn": str(self.latest_snapshot.equity_tmn),
                    }
                },
            )
            self._started = True
        except Exception as exc:
            await self.repo.system_event(
                "error", "engine", f"startup failed: {exc}", {"run_id": self.run_id}
            )
            await self.repo.finish_run("failed")
            raise

    async def _init_broker_and_portfolio(self) -> None:
        if self.feed is None:
            raise RuntimeError("feed not initialized")
        if self.settings.is_live:
            balances = await self.rest.balances()
            sub_account = (self.settings.wallex_sub_account_client_id or "").strip()
            if sub_account:
                main_balances = await self.rest.balances_for_account(None)
                main_tmn = Decimal(
                    str((main_balances.get("TMN") or {}).get("value", 0) or 0)
                )
                sub_tmn = Decimal(str((balances.get("TMN") or {}).get("value", 0) or 0))
                if abs(main_tmn - sub_tmn) > Decimal("1"):
                    raise RuntimeError(
                        "API key account mismatch: WALLEX_API_KEY appears to belong to "
                        f"the main account (TMN={main_tmn}), while the sub-account has "
                        f"TMN={sub_tmn}. Create/copy the API key of the prop "
                        "sub-account, set WALLEX_API_KEY to it, and clear "
                        "WALLEX_SUB_ACCOUNT_CLIENT_ID."
                    )
            tmn = Decimal(str((balances.get("TMN") or {}).get("value", 0) or 0))
            self.portfolio = Portfolio(self.settings, tmn)
            self.broker = LiveBroker(self.settings, self.rest)
            if tmn < Decimal(str(self.settings.min_notional_tmn)):
                raise RuntimeError(
                    f"live preflight failed: TMN balance {tmn} is below minimum "
                    f"{self.settings.min_notional_tmn}. Fund the challenge account first."
                )
            restore = await self.repo.latest_positions_for_mode("live")
            self.portfolio.restore_positions(restore)
            self.log.info(
                "live portfolio restored",
                extra={"context": {"cash_tmn": str(tmn), "positions": len(restore)}},
            )
        else:
            self.portfolio = Portfolio(
                self.settings, Decimal(str(self.settings.initial_capital_tmn))
            )
            self.broker = ShadowBroker(self.settings)
            if self.settings.has_wallex_key:
                try:
                    balances = await self.rest.balances()
                    tmn = Decimal(str((balances.get("TMN") or {}).get("value", 0) or 0))
                    await self.repo.system_event(
                        "info",
                        "engine",
                        "read-only live account context",
                        {"tmn_balance": str(tmn), "mode": "shadow", "real_money": False},
                    )
                except WallexAPIError as exc:
                    logger.warning("shadow read-only balance context failed: %s", exc)

    async def _init_agents(self) -> None:
        if not self.settings.has_wallex_key:
            logger.warning("WALLEX_API_KEY missing; MCP preflight disabled")
        else:
            try:
                self.mcp = WallexMCPClient(self.settings)
                await self.mcp.initialize()
                tools = await self.mcp.tools()
                await self.repo.system_event(
                    "info",
                    "mcp",
                    "Wallex MCP initialized",
                    {"tools": len(tools), "names": [tool.get("name") for tool in tools[:40]]},
                )
            except (MCPError, WallexAPIError) as exc:
                self.mcp = None
                logger.warning("MCP unavailable: %s", exc)
                await self.repo.system_event("warning", "mcp", f"MCP unavailable: {exc}")
        if self.settings.agent_enabled and self.settings.has_deepseek_key:
            assert self.memory is not None
            self.agent = DecisionAgent(
                self.settings, self.repo, mcp=self.mcp, memory=self.memory
            )
            self.memo_llm = DeepSeekLLM(self.settings, self.repo)
        elif self.settings.allow_agent_fallback:
            logger.warning("DeepSeek unavailable; using deterministic fallback (explicitly allowed)")
            self.agent = DeterministicAgent(self.settings)
        else:
            logger.warning("DeepSeek unavailable; agent disabled, entries will HOLD")
            self.agent = DisabledAgent(self.settings)
        assert self.memory is not None
        self.reflection = ReflectionAgent(self.settings, self.repo, self.memory)

    # ------------------------------------------------------------------
    async def run(
        self, duration_sec: float | None = None, *, confirm_live: bool = False
    ) -> None:
        if not self._started:
            await self.start(confirm_live=confirm_live)
        assert self.feed is not None and self.portfolio is not None and self.oms is not None
        self._tasks = [
            asyncio.create_task(self._decision_loop(), name="decision-loop"),
            asyncio.create_task(self._position_loop(), name="position-loop"),
            asyncio.create_task(self._equity_loop(), name="equity-loop"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile-loop"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance-loop"),
            asyncio.create_task(self._universe_loop(), name="universe-loop"),
            asyncio.create_task(self._regime_loop(), name="regime-loop"),
            asyncio.create_task(self._session_review_loop(), name="session-review-loop"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat-loop"),
        ]
        try:
            if duration_sec:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=duration_sec)
            else:
                await self._stop_event.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        logger.info("stopping engine")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for task in list(self._reflection_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            if (
                self.settings.flatten_on_stop
                and self.portfolio is not None
                and self.portfolio.positions
                and self.feed is not None
            ):
                try:
                    await self._flatten_all("shutdown")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("flatten_on_stop failed: %s", exc)
            if self.oms is not None:
                await self.oms.cancel_all()
            if self.portfolio is not None:
                now = datetime.now(UTC)
                if self.feed is not None:
                    self.portfolio.marks_for(self.feed.books_map())
                snapshot = self.portfolio.snapshot(now)
                if self.feed is not None:
                    try:
                        await self.repo.save_position_snapshot(snapshot, self.portfolio)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("final position snapshot failed: %s", exc)
                try:
                    await self.repo.save_equity(snapshot)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("final equity snapshot failed: %s", exc)
                try:
                    await self._write_run_artifacts(snapshot)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("run artifact write failed: %s", exc)
            try:
                await self.repo.system_event(
                    "info", "engine", "engine stopped", {"run_id": self.run_id}
                )
                await self.repo.finish_run("stopped")
            except Exception as exc:  # noqa: BLE001
                logger.warning("final run bookkeeping failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("shutdown error: %s", exc)
        for component in (self.memo_llm, self.agent, self.reflection):
            if component is not None:
                with contextlib.suppress(Exception):
                    await component.close()
        if self.mcp is not None:
            with contextlib.suppress(Exception):
                await self.mcp.close()
        if self.feed is not None:
            with contextlib.suppress(Exception):
                await self.feed.stop()
        await self.rest.close()
        await self.db.close()
        logger.info("engine stopped")

    def request_stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    async def _decision_loop(self) -> None:
        assert self.feed is not None and self.portfolio is not None and self.risk is not None
        while not self._stop_event.is_set():
            try:
                await self._sleep(self.settings.decision_interval_sec)
                if self._decision_busy or not self._entries_allowed():
                    continue
                self._decision_busy = True
                try:
                    await self._run_decision_cycle()
                finally:
                    self._decision_busy = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("decision loop error: %s", exc)
                await self._safe_system_event("error", "decision", f"decision loop error: {exc}")

    async def _run_decision_cycle(self) -> None:
        assert self.feed is not None and self.portfolio is not None and self.risk is not None
        assert self.oms is not None
        now = datetime.now(UTC)
        books = self.feed.books_map()
        self.portfolio.marks_for(books)
        self.regime = self.regime_detector.detect(self.feed.candles, books, now)
        if self.ai_memo:
            self.regime.memo = f"{self.regime.memo} AI regime memo: {self.ai_memo}"
        candidates: list[tuple[Candidate, dict[str, Any], OrderBook, MarketMeta]] = []
        if self.settings.strategy_mode in ("intraday", "both"):
            for symbol in self.symbols:
                if symbol in self.portfolio.positions:
                    continue
                book = books.get(symbol)
                meta = self.metadatas.get(symbol)
                if (
                    book is None
                    or meta is None
                    or not book.is_healthy(self.settings.max_depth_age_ms)
                ):
                    continue
                if any(order.symbol == symbol for order in self.oms.open_orders()):
                    continue
                features = self.feature_engine.compute(
                    symbol,
                    self.feed.candles,
                    book=book,
                    tape=self.feed.tape,
                    cross_symbols=self.symbols,
                    now=now,
                )
                if features is None:
                    continue
                candidate = self.signal_engine.evaluate(
                    symbol,
                    features,
                    self.regime,
                    fee_taker=self.fee_rates.get(symbol, FeeRate(0, 0)).taker,
                    fee_maker=self.fee_rates.get(symbol, FeeRate(0, 0)).maker,
                )
                if candidate is not None:
                    candidates.append((candidate, features, book, meta))
        if self.settings.strategy_mode in ("swing", "both"):
            for symbol in self.symbols:
                bars_4h = self.feed.swing_candles(symbol, now=now)
                if len(bars_4h) < self.settings.swing_slow_ema + 2:
                    continue
                last_bar = bars_4h[-1].ts
                if self._last_swing_bar.get(symbol) == last_bar:
                    continue
                self._last_swing_bar[symbol] = last_bar
                if symbol in self.portfolio.positions:
                    continue
                if any(order.symbol == symbol for order in self.oms.open_orders()):
                    continue
                book = books.get(symbol)
                meta = self.metadatas.get(symbol)
                if (
                    book is None
                    or meta is None
                    or not book.is_healthy(self.settings.max_depth_age_ms)
                ):
                    continue
                candidate = self.swing_engine.evaluate(
                    symbol,
                    bars_4h,
                    self.regime,
                    fee_taker=self.fee_rates.get(symbol, FeeRate(0, 0)).taker,
                    fee_maker=self.fee_rates.get(symbol, FeeRate(0, 0)).maker,
                )
                if candidate is not None:
                    candidate.spread_bps = book.spread_bps
                    candidate.top5_depth_tmn = book.top5_depth_tmn()
                    candidates.append((candidate, candidate.features, book, meta))
        candidates.sort(key=lambda item: item[0].score, reverse=True)
        selected = candidates[: self.settings.max_candidates_per_cycle]
        self.last_scan_at = now
        self.last_candidate_count = len(selected)
        self.last_scan_candidates = [
            f"{item[0].symbol}:{item[0].signal}:{item[0].score:.2f}" for item in selected
        ]
        if not selected:
            return
        snapshot = self.portfolio.snapshot(now)
        health = self.feed.health()
        api_error_rate = float(self.rest.stats().get("error_rate", 0))
        clock_skew = self.rest.clock_skew_sec
        candidate_context = [
            {
                "symbol": item[0].symbol,
                "signal": item[0].signal,
                "score": item[0].score,
                "expected_edge_bps": item[0].expected_edge_bps,
                "spread_bps": item[0].spread_bps,
                "regime": item[0].regime,
            }
            for item in selected
        ]
        for candidate, features, book, meta in selected:
            decision_id = self._new_decision_id(candidate.symbol)
            pre = self.risk.pre_check(
                candidate,
                self.portfolio,
                book,
                self.regime,
                meta,
                self.oms.open_orders(),
                now=now,
                ws_healthy=bool(health.get("ws_connected")),
                api_error_rate=api_error_rate,
                clock_skew_sec=clock_skew,
            )
            if not pre.approved:
                await self._record_decision(
                    decision_id=decision_id,
                    ts=now,
                    symbol=candidate.symbol,
                    action="HOLD",
                    confidence=0.0,
                    candidate=candidate,
                    features=features,
                    agent={},
                    risk=pre.to_dict(),
                    execution={"status": "PRECHECK_VETO"},
                    rationale="; ".join(pre.reasons)[:600],
                )
                continue
            others = [
                item for item in candidate_context if item["symbol"] != candidate.symbol
            ]
            result = await self._agent_decide(
                candidate,
                snapshot,
                decision_id,
                market_context={"other_candidates": others},
            )
            agent_payload = _agent_payload(result)
            if result.error or result.decision.action != AgentAction.BUY:
                await self._record_decision(
                    decision_id=decision_id,
                    ts=now,
                    symbol=candidate.symbol,
                    action=result.decision.action.value,
                    confidence=result.decision.confidence,
                    candidate=candidate,
                    features=features,
                    agent=agent_payload,
                    risk=pre.to_dict(),
                    execution={"status": "NO_TRADE", "agent_error": result.error},
                    rationale=result.decision.rationale,
                )
                continue
            approval = self.risk.approve(
                result.decision,
                candidate,
                self.portfolio,
                book,
                meta,
                fee_rate=self.fee_rates.get(candidate.symbol),
                position=None,
                open_orders=self.oms.open_orders(),
                now=now,
            )
            if not approval.approved:
                await self._record_decision(
                    decision_id=decision_id,
                    ts=now,
                    symbol=candidate.symbol,
                    action="HOLD",
                    confidence=result.decision.confidence,
                    candidate=candidate,
                    features=features,
                    agent=agent_payload,
                    risk=approval.to_dict(),
                    execution={"status": "RISK_VETO"},
                    rationale="; ".join(approval.reasons)[:600],
                )
                continue
            execution = await self.oms.execute_entry(
                decision=result.decision,
                candidate=candidate,
                approval=approval,
                meta=meta,
                book=book,
                decision_id=decision_id,
                fee_rate=self.fee_rates.get(candidate.symbol),
                agent_payload=agent_payload,
            )
            await self._record_decision(
                decision_id=decision_id,
                ts=now,
                symbol=candidate.symbol,
                action="BUY",
                confidence=result.decision.confidence,
                candidate=candidate,
                features=features,
                agent=agent_payload,
                risk=approval.to_dict(),
                execution=execution,
                rationale=result.decision.rationale,
            )
            for trade in execution.get("closed_trades", []) or []:
                self._schedule_reflection(trade)
            await self.repo.save_market_snapshot(
                now,
                candidate.symbol,
                float(book.best_bid or 0),
                float(book.best_ask or 0),
                book.spread_bps,
                float(book.microprice or 0),
                {
                    "top5_depth_tmn": book.top5_depth_tmn(),
                    "imbalance_5": book.imbalance(5),
                },
            )

    async def _agent_decide(
        self,
        candidate: Candidate,
        snapshot: Any,
        decision_id: str,
        market_context: dict[str, Any] | None = None,
    ) -> AgentResult:
        assert self.agent is not None
        if self.regime is None:
            raise RuntimeError("regime unavailable")
        try:
            return await self.agent.decide(
                candidate,
                snapshot,
                self.regime,
                position=None,
                decision_id=decision_id,
                market_context=market_context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent decide failed: %s", exc)
            decision = AgentDecision(
                action=AgentAction.HOLD,
                confidence=0.0,
                size_fraction=0.0,
                order_type="LIMIT",
                limit_offset_bps=0.0,
                take_profit_bps=self.settings.take_profit_bps,
                stop_loss_bps=self.settings.stop_loss_bps,
                horizon_sec=self.settings.time_stop_sec,
                regime=self.regime.label,
                rationale=f"HOLD: agent exception: {exc}"[:600],
                risk_flags=["agent_exception"],
            )
            return AgentResult(
                decision=decision,
                model="exception",
                response_hash=decision.response_hash(),
                error=str(exc),
            )

    # ------------------------------------------------------------------
    async def _position_loop(self) -> None:
        assert self.feed is not None and self.portfolio is not None
        while not self._stop_event.is_set():
            try:
                await self._sleep(self.settings.position_interval_sec)
                books = self.feed.books_map()
                self.portfolio.marks_for(books)
                assert self.oms is not None
                closed = await self.oms.maintain(
                    books, self.feed.recent_trades_map(10), now=datetime.now(UTC)
                )
                for trade in closed:
                    self._schedule_reflection(trade)
                now = datetime.now(UTC)
                self._check_data_health(now)
                for symbol, position in list(self.portfolio.positions.items()):
                    mark = self.portfolio.mark_price(symbol)
                    if mark is None:
                        last = self.feed.candles.last_price(symbol)
                        if last is not None:
                            mark = Decimal(str(last))
                            self.portfolio.mark(symbol, mark)
                    if mark is None:
                        continue
                    pos_book = books.get(symbol)
                    update_trailing_stop(position, mark, self.settings)
                    reason = self._exit_reason(position, mark, now)
                    if not reason and position.age_sec(now) >= 300:
                        last_check = self._last_strategy_check.get(symbol, 0.0)
                        if time.monotonic() - last_check >= 5:
                            self._last_strategy_check[symbol] = time.monotonic()
                            features = self.feature_engine.compute(
                                symbol,
                                self.feed.candles,
                                book=pos_book,
                                tape=self.feed.tape,
                                cross_symbols=self.symbols,
                                now=now,
                            )
                            if features:
                                reason = evaluate_signal_exit(
                                    features,
                                    position,
                                    mark,
                                    self.settings,
                                    self.fee_rates.get(symbol, FeeRate(0, 0)).taker,
                                ) or None
                    if reason:
                        exit_trade = await self._exit_position(
                            position, reason, pos_book, mark, now
                        )
                        if exit_trade:
                            self._schedule_reflection(exit_trade)
                        self._last_strategy_check.pop(symbol, None)
                        continue
                    if self._should_review(position, now):
                        self._schedule_position_review(position, pos_book, mark, now)
                await self._maybe_endgame_flatten(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("position loop error: %s", exc)

    def _exit_reason(self, position: Position, mark: Decimal, now: datetime) -> str | None:
        if mark <= position.stop_price:
            return "stop_loss"
        if mark >= position.take_profit_price:
            return "take_profit"
        if position.age_sec(now) >= position.horizon_sec:
            return "time_stop"
        if self.settings.challenge_end:
            remaining = (self.settings.challenge_end - now).total_seconds()
            if remaining <= self.settings.flatten_before_end_sec:
                return "endgame"
        return None

    def _should_review(self, position: Position, now: datetime) -> bool:
        if not self.settings.agent_enabled or self.kill.state.active:
            return False
        if position.symbol in self._review_symbols:
            return False
        if position.signal == "TREND" and not self.settings.agent_review_trend_positions:
            return False
        if isinstance(self.agent, DisabledAgent):
            return False
        if position.age_sec(now) < 300:
            return False
        if position.last_review_at and (now - position.last_review_at).total_seconds() < 120:
            return False
        return True

    def _schedule_position_review(
        self, position: Position, book: OrderBook | None, mark: Decimal, now: datetime
    ) -> None:
        self._review_symbols.add(position.symbol)
        task = asyncio.create_task(
            self._review_position(position, book, mark, now),
            name=f"review-{position.symbol}",
        )
        task.add_done_callback(lambda _: self._review_symbols.discard(position.symbol))

    async def _review_position(
        self, position: Position, book: OrderBook | None, mark: Decimal, now: datetime
    ) -> None:
        async with self._review_lock:
            assert self.feed is not None and self.portfolio is not None and self.regime is not None
            if position.symbol not in self.portfolio.positions:
                return
            features = self.feature_engine.compute(
                position.symbol,
                self.feed.candles,
                book=book,
                tape=self.feed.tape,
                cross_symbols=self.symbols,
                now=now,
            )
            if features is None:
                features = dict(position.metadata or {})
            candidate = Candidate(
                symbol=position.symbol,
                side=Side.SELL,
                signal="position_review",
                score=0.0,
                expected_edge_bps=0.0,
                spread_bps=float(features.get("spread_bps") or 0.0),
                top5_depth_tmn=float(features.get("top5_depth_tmn") or 0.0),
                features=features,
                regime=self.regime.label,
                rationale="normal exit review of an open position",
            )
            decision_id = self._new_decision_id(position.symbol)
            snapshot = self.portfolio.snapshot(now)
            result = await self._agent_decide(
                candidate, snapshot, decision_id
            )
            position.last_review_at = now
            agent_payload = _agent_payload(result)
            if result.decision.action == AgentAction.SELL:
                book_for_exit = book or self.feed.books.get(position.symbol)
                trade = await self._exit_position(
                    position, "agent_exit", book_for_exit, mark, now, decision_id=decision_id
                )
                if trade:
                    self._schedule_reflection(trade)
            else:
                await self._record_decision(
                    decision_id=decision_id,
                    ts=now,
                    symbol=position.symbol,
                    action=result.decision.action.value,
                    confidence=result.decision.confidence,
                    candidate=candidate,
                    features=features,
                    agent=agent_payload,
                    risk={"approved": True, "reason": "position review"},
                    execution={"status": "HOLD_POSITION"},
                    rationale=result.decision.rationale,
                )

    async def _exit_position(
        self,
        position: Position,
        reason: str,
        book: OrderBook | None,
        mark: Decimal,
        now: datetime,
        *,
        decision_id: str | None = None,
    ) -> dict[str, Any] | None:
        assert self.risk is not None and self.oms is not None and self.portfolio is not None
        meta = self.metadatas.get(position.symbol)
        if meta is None:
            return None
        did = decision_id or self._new_decision_id(position.symbol)
        approval = self.risk.approve_exit(
            position,
            book,
            meta,
            reason=reason,
            fee_rate=self.fee_rates.get(position.symbol),
            now=now,
            fallback_price=mark,
        )
        if not approval.approved:
            await self.repo.risk_event(
                "exit_rejected",
                f"exit for {position.symbol} rejected: {approval.reasons}",
                severity="error",
                evidence=approval.to_dict(),
            )
            return None
        execution = await self.oms.execute_exit(
            position=position,
            approval=approval,
            meta=meta,
            book=book,
            decision_id=did,
            reason=reason,
            fee_rate=self.fee_rates.get(position.symbol),
        )
        trades = execution.get("closed_trades", []) or []
        trade = trades[0] if trades else None
        await self._record_decision(
            decision_id=did,
            ts=now,
            symbol=position.symbol,
            action="SELL",
            confidence=1.0,
            candidate=None,
            features={},
            agent={},
            risk=approval.to_dict(),
            execution={**execution, "trigger": reason},
            rationale=f"forced exit: {reason}",
        )
        return trade

    async def _flatten_all(self, reason: str) -> int:
        assert self.portfolio is not None and self.feed is not None
        closed = 0
        for _ in range(3):
            positions = list(self.portfolio.positions.values())
            if not positions:
                break
            books = self.feed.books_map()
            for position in positions:
                mark = self.portfolio.mark_price(position.symbol)
                if mark is None:
                    last = self.feed.candles.last_price(position.symbol)
                    if last is not None:
                        mark = Decimal(str(last))
                if mark is None:
                    continue
                trade = await self._exit_position(
                    position,
                    reason,
                    books.get(position.symbol),
                    mark,
                    datetime.now(UTC),
                )
                if trade:
                    closed += 1
                    self._schedule_reflection(trade)
            await self.oms.maintain(  # type: ignore[union-attr]
                self.feed.books_map(), self.feed.recent_trades_map(10)
            )
            await asyncio.sleep(0.5)
        if self.oms is not None:
            await self.oms.cancel_all()
        await self._safe_system_event("warning", "risk", f"flatten complete ({reason})", {"closed": closed})
        return closed

    async def _maybe_endgame_flatten(self, now: datetime) -> None:
        if not self.settings.challenge_end or self._endgame_flattened:
            return
        remaining = (self.settings.challenge_end - now).total_seconds()
        if remaining <= self.settings.flatten_before_end_sec:
            self._endgame_flattened = True
            await self._flatten_all("endgame")

    # ------------------------------------------------------------------
    async def _equity_loop(self) -> None:
        assert self.portfolio is not None and self.feed is not None
        while not self._stop_event.is_set():
            try:
                await self._sleep(15)
                now = datetime.now(UTC)
                self.portfolio.marks_for(self.feed.books_map())
                snapshot = self.portfolio.snapshot(now)
                self.latest_snapshot = snapshot
                await self.repo.save_equity(snapshot)
                await self.repo.save_position_snapshot(snapshot, self.portfolio)
                await self._check_kill_conditions(now, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("equity loop error: %s", exc)

    async def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._sleep(5)
                if self.reconciler is None:
                    continue
                result = await self.reconciler.check()
                if result.get("ok"):
                    self._reconcile_failures = 0
                    continue
                self._reconcile_failures += 1
                await self._safe_system_event(
                    "warning",
                    "reconcile",
                    f"reconciliation mismatch ({self._reconcile_failures}/3)",
                    result,
                )
                if self._reconcile_failures >= 3:
                    await self._activate_kill(
                        "reconciliation mismatch persisted for 3 checks",
                        trigger="reconcile",
                        evidence=result,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("reconcile loop error: %s", exc)

    async def _maintenance_loop(self) -> None:
        assert self.feed is not None
        cycle = 0
        while not self._stop_event.is_set():
            try:
                await self._sleep(30)
                cycle += 1
                now = datetime.now(UTC)
                for symbol in self.symbols:
                    book = self.feed.books.get(symbol)
                    if book is None or book.age_ms(now) > 15_000:
                        try:
                            await self.feed.refresh_depth(symbol)
                        except WallexAPIError as exc:
                            logger.warning("depth refresh failed for %s: %s", symbol, exc)
                    if not self.feed.tape.recent(symbol, 120, now):
                        try:
                            await self.feed.refresh_trades(symbol)
                        except WallexAPIError as exc:
                            logger.warning("trade refresh failed for %s: %s", symbol, exc)
                if cycle % 2 == 0:
                    await self._save_market_snapshots(now)
                if cycle % 4 == 0:
                    stats = {
                        "rest": self.rest.stats(),
                        "ws": self.feed.health(),
                        "agent": self._agent_stats(),
                        "portfolio": self.portfolio.state() if self.portfolio else {},
                    }
                    await self._safe_system_event("info", "system", "health snapshot", stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("maintenance loop error: %s", exc)

    async def _universe_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._sleep(self.settings.universe_refresh_sec)
                await self._refresh_universe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("universe refresh error: %s", exc)

    async def _refresh_universe(self) -> None:
        assert self.feed is not None and self.portfolio is not None
        metadata = await self.rest.market_metadata()
        books: dict[str, OrderBook] = {}
        for symbol in self.settings.universe_or_default():
            if symbol in metadata:
                try:
                    books[symbol] = await self.rest.depth(symbol, limit=50)
                except WallexAPIError:
                    continue
        universe = self.universe_selector.select(metadata, books or None)
        selected = [meta.symbol for meta in universe.selected]
        for symbol in self.portfolio.positions:
            if symbol not in selected:
                selected.append(symbol)
        if set(selected) != set(self.symbols):
            self.symbols = selected
            self.metadatas = metadata
            await self.feed.update_symbols(selected)
            if self.settings.has_wallex_key:
                try:
                    fees = await self.rest.fees()
                    self.fee_rates = {
                        symbol: fees.get(
                            symbol,
                            FeeRate(self.settings.maker_fee, self.settings.taker_fee),
                        )
                        for symbol in selected
                    }
                    if self.oms is not None:
                        self.oms.fee_rates = self.fee_rates
                except WallexAPIError:
                    pass
            await self._safe_system_event(
                "info",
                "universe",
                "universe refreshed",
                {"selected": selected, "excluded": universe.excluded},
            )

    async def _regime_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._sleep(60)
                if time.monotonic() - self.last_regime_at < self.settings.regime_memo_interval_sec:
                    continue
                self.last_regime_at = time.monotonic()
                await self._generate_regime_memo()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("regime memo error: %s", exc)

    async def _generate_regime_memo(self) -> None:
        assert self.feed is not None and self.portfolio is not None
        now = datetime.now(UTC)
        regime = self.regime_detector.detect(self.feed.candles, self.feed.books_map(), now)
        memo = regime.memo
        source = "deterministic"
        if self.memo_llm is not None and self.settings.has_deepseek_key:
            try:
                btc = self.feature_engine.compute(
                    "BTCTMN",
                    self.feed.candles,
                    book=self.feed.books.get("BTCTMN"),
                    tape=self.feed.tape,
                    cross_symbols=self.symbols,
                    now=now,
                )
                chat = await self.memo_llm.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "You are a conservative regime analyst for a spot-only, long-only "
                                "crypto trading system on Wallex. Given deterministic features, write "
                                "a two-sentence regime memo. No advice about leverage or shorting. "
                                "Return JSON only: {\"regime\":\"range|trend_up|trend_down|high_vol|chaos|illiquid\","
                                "\"memo\":\"...\"}."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "deterministic_regime": regime.to_dict(),
                                    "btc_features": btc or {},
                                    "portfolio": self._portfolio_payload(now),
                                },
                                default=str,
                            ),
                        },
                    ],
                    model=self.settings.deepseek_deep_model,
                    max_tokens=1500,
                    temperature=0.0,
                    json_mode=True,
                    kind="regime_memo",
                    prompt_hash="",
                    reasoning_effort="low",
                )
                from wallex_trader.agent.schemas import parse_json_object

                payload = parse_json_object(chat.content)
                memo = str(payload.get("memo", memo))[:800]
                source = f"deepseek:{chat.model}"
                self.ai_regime_label = str(payload.get("regime", regime.label))
            except Exception as exc:  # noqa: BLE001
                logger.warning("AI regime memo failed: %s", exc)
        self.ai_memo = memo
        if self.memory is not None:
            await self.memory.add_note(
                note=memo,
                lesson="",
                symbol=None,
                kind="regime",
                outcome="neutral",
                tags=[regime.label, "regime"],
                importance=0.3,
                metadata={"regime": regime.to_dict(), "source": source},
            )
        await self._safe_system_event("info", "regime", "regime memo refreshed", {"memo": memo, "source": source})

    async def _session_review_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._sleep(900)
                if self.reflection is None or self.portfolio is None:
                    continue
                assert self.portfolio is not None
                decisions = await self.repo.recent_decisions(self.run_id, limit=50)
                snapshot = self.latest_snapshot or self.portfolio.snapshot(datetime.now(UTC))
                await self.reflection.maybe_session_review(
                    self.portfolio, snapshot, decisions
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("session review error: %s", exc)

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._sleep(self.settings.heartbeat_sec)
                await self.repo.heartbeat(
                    "trader",
                    {
                        "mode": self.settings.mode.value,
                        "symbols": len(self.symbols),
                        "equity_tmn": str(self.portfolio.equity_tmn()) if self.portfolio else None,
                        "kill": self.kill.state.to_dict(),
                        "ws": self.feed.health() if self.feed else {},
                        "agent": self._agent_stats(),
                        "scan": {
                            "at": self.last_scan_at.isoformat() if self.last_scan_at else None,
                            "candidates": self.last_candidate_count,
                            "selected": self.last_scan_candidates,
                            "regime": self.regime.label if self.regime else None,
                            "next_swing_close": _next_swing_close(
                                datetime.now(UTC),
                                self.settings.swing_timeframe_hours,
                            ).isoformat(),
                        },
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("heartbeat error: %s", exc)

    # ------------------------------------------------------------------
    async def _check_kill_conditions(self, now: datetime, snapshot: Any) -> None:
        if self.kill.state.active:
            return
        if snapshot.drawdown_pct >= self.settings.max_drawdown:
            await self._activate_kill(
                f"peak drawdown {snapshot.drawdown_pct:.2%} >= {self.settings.max_drawdown:.2%}",
                trigger="drawdown",
            )
            return
        if snapshot.daily_pnl_pct <= -self.settings.daily_loss_limit:
            await self._activate_kill(
                f"daily loss {snapshot.daily_pnl_pct:.2%} breached",
                trigger="daily_loss",
            )
            return
        stats = self.rest.stats()
        if (
            stats.get("clock_skew_sec") is not None
            and abs(float(stats["clock_skew_sec"])) > self.settings.clock_skew_kill_sec
        ):
            await self._activate_kill(
                f"clock skew above {self.settings.clock_skew_kill_sec:.1f}s",
                trigger="clock_skew",
            )
            return
        if stats.get("calls", 0) > 50 and stats.get("error_rate", 0) > 0.10:
            await self._activate_kill("API error rate above 10%", trigger="api_error_rate")
            return
        if self.kill.file_requested() and not self.kill.state.active:
            self.kill.check_file()
            await self._activate_kill(self.kill.state.reason, trigger="file")

    def _check_data_health(self, now: datetime) -> None:
        if self.feed is None or self.feed.ws is None:
            return
        if self.feed.ws.connected:
            self._ws_down_since = None
            return
        if self._ws_down_since is None:
            self._ws_down_since = time.monotonic()
            return
        down_sec = time.monotonic() - self._ws_down_since
        if down_sec > self.settings.ws_down_kill_sec and not self.kill.state.active:
            asyncio.create_task(
                self._activate_kill(
                    f"WebSocket down for {down_sec:.0f}s", trigger="ws_down"
                )
            )

    async def _activate_kill(
        self,
        reason: str,
        *,
        trigger: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        if self._kill_handled:
            return
        self._kill_handled = True
        self.kill.activate(reason, trigger=trigger)
        logger.error("KILL SWITCH: %s (%s)", reason, trigger)
        await self.repo.risk_event(
            "kill_switch",
            reason,
            severity="critical",
            evidence={
                "trigger": trigger,
                "snapshot": self.latest_snapshot.to_dict() if self.latest_snapshot else {},
                "details": evidence or {},
            },
        )
        await self._safe_system_event("critical", "risk", f"KILL SWITCH: {reason}", {"trigger": trigger})
        if self.oms is not None:
            await self.oms.cancel_all()
        if self.settings.flatten_on_kill and self.feed is not None:
            await self._flatten_all(f"kill:{trigger}")

    def _entries_allowed(self) -> bool:
        if self.kill.state.active:
            return False
        if self.feed is None:
            return False
        health = self.feed.health()
        if not health.get("ws_connected"):
            return False
        if self.settings.challenge_end:
            remaining = (self.settings.challenge_end - datetime.now(UTC)).total_seconds()
            if remaining <= self.settings.no_new_entries_before_end_sec:
                return False
        return True

    async def _save_market_snapshots(self, now: datetime) -> None:
        if self.feed is None:
            return
        for symbol in self.symbols:
            book = self.feed.books.get(symbol)
            if book and book.best_bid is not None and book.best_ask is not None:
                await self.repo.save_market_snapshot(
                    now,
                    symbol,
                    float(book.best_bid),
                    float(book.best_ask),
                    book.spread_bps,
                    float(book.microprice or 0),
                    {
                        "top5_depth_tmn": book.top5_depth_tmn(),
                        "imbalance_5": book.imbalance(5),
                        "source": book.source,
                    },
                )

    async def _record_decision(
        self,
        *,
        decision_id: str,
        ts: datetime,
        symbol: str,
        action: str,
        confidence: float,
        candidate: Candidate | None,
        features: dict[str, Any],
        agent: dict[str, Any],
        risk: dict[str, Any],
        execution: dict[str, Any],
        rationale: str,
    ) -> None:
        payload = {
            "decision_id": decision_id,
            "run_id": self.run_id,
            "ts": ts,
            "symbol": symbol,
            "action": action,
            "confidence": confidence,
            "signal": candidate.signal if candidate else "exit",
            "score": candidate.score if candidate else None,
            "mode": self.settings.mode.value,
            "candidate": candidate.to_dict() if candidate else {},
            "features": features,
            "agent": agent,
            "risk": risk,
            "execution": execution,
            "rationale": rationale,
            "prompt_hash": agent.get("prompt_hash", ""),
            "response_hash": agent.get("response_hash", ""),
            "latency_ms": agent.get("latency_ms", 0.0),
        }
        try:
            await self.repo.save_decision(payload)
            if candidate is not None:
                feature_hash = self.feature_engine.feature_hash(features)
                await self.repo.save_features(ts, symbol, features, feature_hash)
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to persist decision: %s", exc)

    def _schedule_reflection(self, trade: dict[str, Any]) -> None:
        if self._stopped or self.reflection is None or self.portfolio is None:
            return
        snapshot = self.latest_snapshot or self.portfolio.snapshot(datetime.now(UTC))
        task = asyncio.create_task(
            self.reflection.on_trade_closed(trade, snapshot),
            name=f"reflection-{trade.get('symbol', 'trade')}",
        )
        self._reflection_tasks.add(task)
        task.add_done_callback(self._reflection_tasks.discard)

    async def _write_run_artifacts(self, snapshot: Any) -> None:
        if self.portfolio is None:
            return
        mode = "live" if self.settings.is_live else "shadow"
        artifact_dir = Path(self.settings.artifacts_dir) / mode / self.run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        decisions = await self.repo.recent_decisions(self.run_id, limit=10_000)
        metrics = {
            "run_id": self.run_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": mode,
            "snapshot": snapshot.to_dict(),
            "symbols": self.symbols,
            "trades": self.portfolio.recent_trades(200),
            "win_rate": self.portfolio.win_rate(),
            "decisions": len(decisions),
            "agent": self._agent_stats(),
            "reflection": self.reflection.stats() if self.reflection else {},
            "ws": self.feed.health() if self.feed else {},
            "fees": self.fee_rates,
            "git_sha": self.settings.git_sha,
            "config_hash": self.settings.config_hash(),
            "positions": {
                symbol: position.to_dict()
                for symbol, position in self.portfolio.positions.items()
            },
            "orders": await self.repo.list_orders(self.run_id, 10_000),
            "fills": await self.repo.list_fills(self.run_id, 10_000),
            "equity": await self.repo.equity_series(self.run_id, 20_000),
        }
        (artifact_dir / "metrics.json").write_text(
            json.dumps(json_safe(metrics), indent=2), encoding="utf-8"
        )
        (artifact_dir / "decisions.jsonl").write_text(
            await self.repo.export_decisions_jsonl(self.run_id), encoding="utf-8"
        )
        (artifact_dir / "positions.json").write_text(
            json.dumps(json_safe(metrics["positions"]), indent=2), encoding="utf-8"
        )
        (artifact_dir / "orders.json").write_text(
            json.dumps(json_safe(metrics["orders"]), indent=2), encoding="utf-8"
        )
        (artifact_dir / "fills.json").write_text(
            json.dumps(json_safe(metrics["fills"]), indent=2), encoding="utf-8"
        )
        equity_rows = metrics.get("equity") or []
        if equity_rows:
            import csv

            with (artifact_dir / "equity.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(equity_rows[0].keys()))
                writer.writeheader()
                for row in equity_rows:
                    writer.writerow({key: row.get(key) for key in writer.fieldnames})
        journal_copy = artifact_dir / "agent_journal.md"
        if self.settings.journal_path.exists():
            journal_copy.write_text(
                self.settings.journal_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        if not self.settings.is_live:
            await self.repo.save_shadow_run(
                run_id=self.run_id,
                config=self.settings.safe_dump(redact=True),
                metrics=json_safe(metrics),
                artifact_path=str(artifact_dir),
            )

    def _portfolio_payload(self, now: datetime) -> dict[str, Any]:
        if self.portfolio is None:
            return {}
        snapshot = self.portfolio.snapshot(now)
        return {
            "equity_tmn": float(snapshot.equity_tmn),
            "cash_tmn": float(snapshot.cash_tmn),
            "open_positions": snapshot.open_positions,
            "drawdown_pct": round(snapshot.drawdown_pct * 100, 3),
            "daily_pnl_pct": round(snapshot.daily_pnl_pct * 100, 3),
            "net_return_pct": round(snapshot.net_return_pct * 100, 3),
        }

    def _agent_stats(self) -> dict[str, Any]:
        if isinstance(self.agent, DecisionAgent):
            return {**self.agent.llm.stats(), "type": "deepseek"}
        if isinstance(self.agent, DeterministicAgent):
            return {"type": "deterministic", "calls": self.agent.calls}
        return {"type": type(self.agent).__name__ if self.agent else "none"}

    async def _safe_system_event(
        self, level: str, component: str, message: str, context: dict[str, Any] | None = None
    ) -> None:
        try:
            await self.repo.system_event(level, component, message, context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("system event persist failed: %s", exc)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(0.01, seconds))
        except TimeoutError:
            return
        raise asyncio.CancelledError

    def _new_decision_id(self, symbol: str) -> str:
        self._decision_seq += 1
        stamp = int(time.time() * 1000)
        token = self.oms.run_token if self.oms else "run"
        return f"dec_{token}_{symbol}_{stamp}_{self._decision_seq:05d}"


def _next_swing_close(now: datetime, timeframe_hours: int) -> datetime:
    step = max(1, timeframe_hours) * 3600
    boundary = (int(now.timestamp()) // step + 1) * step
    return datetime.fromtimestamp(boundary, tz=UTC)


def _agent_payload(result: AgentResult) -> dict[str, Any]:
    decision = result.decision
    return {
        "model": result.model,
        "action": decision.action.value,
        "confidence": decision.confidence,
        "size_fraction": decision.size_fraction,
        "order_type": decision.order_type,
        "limit_offset_bps": decision.limit_offset_bps,
        "take_profit_bps": decision.take_profit_bps,
        "stop_loss_bps": decision.stop_loss_bps,
        "horizon_sec": decision.horizon_sec,
        "regime": decision.regime,
        "rationale": decision.rationale,
        "risk_flags": decision.risk_flags,
        "prompt_hash": result.prompt_hash,
        "response_hash": result.response_hash,
        "latency_ms": result.latency_ms,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cached": result.cached,
        "escalated": result.escalated,
        "tool_calls": result.tool_calls,
        "error": result.error,
    }
