from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from wallex_trader.agent.client import DecisionAgentProtocol, DeterministicAgent
from wallex_trader.backtest.download import data_manifest, load_candles
from wallex_trader.backtest.fills import (
    fee_tmn,
    limit_fill_price,
    stop_fill_price,
    target_fill_price,
)
from wallex_trader.backtest.metrics import compute_metrics
from wallex_trader.backtest.precompute import (
    build_feature_frames,
    build_swing_features,
    row_to_features,
)
from wallex_trader.backtest.report import write_backtest_artifacts
from wallex_trader.config import Settings
from wallex_trader.models import (
    BookLevel,
    FeeRate,
    Fill,
    MarketMeta,
    OrderBook,
    Side,
)
from wallex_trader.risk.kernel import RiskKernel
from wallex_trader.risk.killswitch import KillSwitch
from wallex_trader.strategy.exits import evaluate_signal_exit, update_trailing_stop
from wallex_trader.strategy.portfolio import Portfolio, PortfolioError
from wallex_trader.strategy.regime import RegimeState
from wallex_trader.strategy.signals import SignalEngine
from wallex_trader.strategy.swing import SwingSignalEngine

logger = logging.getLogger("trader.backtest")


@dataclass(slots=True)
class BacktestResult:
    run_id: str
    metrics: dict[str, Any]
    equity: pd.DataFrame
    trades: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    artifacts: dict[str, str]
    period: str
    data_manifest: dict[str, Any] = field(default_factory=dict)


class BacktestSimulator:
    def __init__(
        self,
        settings: Settings,
        symbols: list[str],
        *,
        agent: DecisionAgentProtocol | None = None,
    ) -> None:
        self.settings = settings
        self.symbols = symbols
        self.agent = agent or DeterministicAgent(settings)
        self.signal_engine = SignalEngine(settings)
        self.swing_engine = SwingSignalEngine(settings)
        self.risk = RiskKernel(settings, KillSwitch(settings))
        self._fill_seq = 0

    async def run(
        self,
        start: str,
        end: str,
        *,
        prepared: tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], pd.DataFrame] | None = None,
    ) -> BacktestResult:
        if prepared is None:
            frames = load_candles(
                self.settings,
                self.symbols,
                start,
                end,
                resolution=self.settings.backtest_resolution,
            )
            frames = {
                symbol: frame
                for symbol, frame in frames.items()
                if len(frame) >= 300
            }
            missing = [
                symbol for symbol in self.symbols if symbol not in frames
            ]
            if missing:
                logger.warning("backtest missing data for: %s", missing)
            if "BTCTMN" not in frames:
                raise ValueError("backtest requires BTCTMN data")
            features, regime = build_feature_frames(frames, self.settings)
            swing_features = (
                build_swing_features(frames, self.settings)
                if self.settings.strategy_mode in ("swing", "both")
                else {}
            )
        else:
            frames, features, regime = prepared
            swing_features = (
                build_swing_features(frames, self.settings)
                if self.settings.strategy_mode in ("swing", "both")
                else {}
            )
        symbols = [symbol for symbol in self.symbols if symbol in frames]
        if "BTCTMN" not in symbols:
            raise ValueError("backtest requires BTCTMN data after filtering")
        run_id = self._run_id(start, end)
        period = f"{start} → {end} ({len(symbols)} symbols, 1m)"

        arrays: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            frame = frames[symbol]
            arrays[symbol] = {
                "ts": frame.index.asi8,
                "open": frame["open"].to_numpy(dtype=float),
                "high": frame["high"].to_numpy(dtype=float),
                "low": frame["low"].to_numpy(dtype=float),
                "close": frame["close"].to_numpy(dtype=float),
                "volume": frame["volume"].to_numpy(dtype=float),
                "features": features[symbol],
                "ptr": -1,
            }
        union = np.unique(np.concatenate([arrays[symbol]["ts"] for symbol in symbols]))
        regime_index = regime.index.asi8
        swing_by_time: dict[int, dict[str, dict[str, float]]] = {}
        for symbol, frame in swing_features.items():
            for ts, row in frame.iterrows():
                swing_by_time.setdefault(int(ts.value), {})[symbol] = {
                    key: float(value) for key, value in row.to_dict().items()
                }

        portfolio = Portfolio(
            self.settings, Decimal(str(self.settings.backtest_initial_capital_tmn))
        )
        metas = {symbol: _market_meta(symbol, frames[symbol]) for symbol in symbols}
        fee_rates = {
            symbol: FeeRate(
                maker=self.settings.backtest_maker_fee,
                taker=self.settings.backtest_taker_fee,
            )
            for symbol in symbols
        }
        decision_interval_ns = self.settings.backtest_decision_interval_sec * 1_000_000_000
        start_ns = int(union[0])
        last_decision_ns = start_ns - decision_interval_ns
        pending: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        closed_trades: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        entry_fill_count = 0
        equity_rows: list[dict[str, Any]] = []
        regime_ptr = -1

        for t_ns in union:
            t_dt = datetime.fromtimestamp(int(t_ns) / 1e9, tz=UTC)
            bars: dict[str, dict[str, Any]] = {}
            for symbol in symbols:
                data = arrays[symbol]
                ptr = data["ptr"]
                ts_arr = data["ts"]
                while ptr + 1 < len(ts_arr) and ts_arr[ptr + 1] <= t_ns:
                    ptr += 1
                data["ptr"] = ptr
                if ptr >= 0 and ts_arr[ptr] == t_ns:
                    bars[symbol] = {
                        "open": float(data["open"][ptr]),
                        "high": float(data["high"][ptr]),
                        "low": float(data["low"][ptr]),
                        "close": float(data["close"][ptr]),
                        "volume": float(data["volume"][ptr]),
                        "ptr": ptr,
                    }
            if not bars:
                continue
            # 1. Execute entries queued at the previous close, at this bar's open.
            still_pending: list[dict[str, Any]] = []
            for entry in pending:
                symbol = entry["symbol"]
                bar = bars.get(symbol)
                if bar is None:
                    still_pending.append(entry)
                    continue
                fill = self._execute_entry(entry, bar, t_dt, portfolio)
                if fill is None:
                    entry["waits"] = entry.get("waits", 0) + 1
                    if entry["waits"] < 2:
                        still_pending.append(entry)
                    continue
                fills.append(fill.to_dict())
                entry_fill_count += 1
            pending = still_pending

            # 2. Stops, targets, trailing exits, signal exits and time stops.
            for symbol, position in list(portfolio.positions.items()):
                bar = bars.get(symbol)
                if bar is None:
                    continue
                # Conservative intrabar assumption: trailing stop can only use
                # the high water mark observed on prior bars, never this bar's
                # high before testing this bar's low.
                update_trailing_stop(
                    position, position.high_water or position.avg_entry, self.settings
                )
                exit_price: Decimal | None = None
                reason = ""
                if bar["low"] <= float(position.stop_price):
                    exit_price = stop_fill_price(
                        bar["open"],
                        position.stop_price,
                        Side.SELL,
                        self.settings.backtest_slippage_bps,
                    )
                    reason = "stop_loss"
                elif bar["high"] >= float(position.take_profit_price):
                    exit_price = target_fill_price(
                        bar["open"],
                        position.take_profit_price,
                        Side.SELL,
                        self.settings.backtest_slippage_bps,
                    )
                    reason = "take_profit"
                else:
                    ptr = bar["ptr"]
                    feature_dict = row_to_features(features[symbol].iloc[ptr], symbol)
                    strategy_reason = evaluate_signal_exit(
                        feature_dict,
                        position,
                        Decimal(str(bar["close"])),
                        self.settings,
                        self.settings.backtest_taker_fee,
                    )
                    if strategy_reason:
                        exit_price = Decimal(
                            str(bar["close"] * (1 - self.settings.backtest_slippage_bps / 10_000))
                        )
                        reason = strategy_reason
                    elif position.age_sec(t_dt) >= position.horizon_sec:
                        exit_price = Decimal(
                            str(bar["close"] * (1 - self.settings.backtest_slippage_bps / 10_000))
                        )
                        reason = "time_stop"
                    elif (
                        self.settings.challenge_end
                        and (self.settings.challenge_end - t_dt).total_seconds()
                        <= self.settings.flatten_before_end_sec
                    ):
                        exit_price = Decimal(
                            str(bar["close"] * (1 - self.settings.backtest_slippage_bps / 10_000))
                        )
                        reason = "endgame"
                if exit_price is not None:
                    trade = self._exit_position(
                        position, exit_price, t_dt, portfolio, reason, fills
                    )
                    if trade:
                        closed_trades.append(trade)
                else:
                    position.update_watermarks(
                        Decimal(str(bar["high"])), Decimal(str(bar["low"]))
                    )

            # 3. Mark to close.
            for symbol, bar in bars.items():
                portfolio.mark(symbol, Decimal(str(bar["close"])))

            # 4. Decisions every N minutes.
            if (
                t_ns - last_decision_ns >= decision_interval_ns
                and t_ns - start_ns >= 300 * 60 * 1_000_000_000
            ):
                last_decision_ns = t_ns
                if "BTCTMN" in bars:
                    while (
                        regime_ptr + 1 < len(regime_index)
                        and regime_index[regime_ptr + 1] <= t_ns
                    ):
                        regime_ptr += 1
                    if regime_ptr >= 0 and regime_index[regime_ptr] == t_ns:
                        regime_row = regime.iloc[regime_ptr]
                        regime_state = RegimeState(
                            label=str(regime_row["regime_label"]),
                            memo=str(regime_row["regime_memo"]),
                            btc_ret_5m_bps=float(regime_row["btc_ret_5m_bps"] or 0),
                            btc_ret_15m_bps=float(regime_row["btc_ret_15m_bps"] or 0),
                            btc_ret_1h_bps=float(regime_row["btc_ret_1h_bps"] or 0),
                            volatility_percentile=float(
                                regime_row["volatility_percentile"] or 50
                            ),
                            average_spread_bps=float(
                                self.settings.backtest_slippage_bps
                            ),
                            btc_trend=str(regime_row["btc_trend"]),
                            created_at=t_dt,
                            source="precomputed",
                        )
                        if self.settings.strategy_mode in ("intraday", "both"):
                            await self._decision_step(
                                bars=bars,
                                t_dt=t_dt,
                                portfolio=portfolio,
                                metas=metas,
                                fee_rates=fee_rates,
                                regime=regime_state,
                                pending=pending,
                                decisions=decisions,
                                arrays=arrays,
                                features=features,
                            )
                        swing_rows = swing_by_time.get(int(t_ns), {})
                        if swing_rows and self.settings.strategy_mode in ("swing", "both"):
                            await self._swing_decision_step(
                                swing_rows=swing_rows,
                                t_dt=t_dt,
                                portfolio=portfolio,
                                metas=metas,
                                fee_rates=fee_rates,
                                regime=regime_state,
                                pending=pending,
                                decisions=decisions,
                            )

            # 5. Equity snapshot.
            snapshot = portfolio.snapshot(t_dt)
            equity_rows.append(
                {
                    "ts": t_dt.isoformat(),
                    "equity": float(snapshot.equity_tmn),
                    "cash": float(snapshot.cash_tmn),
                    "positions_value": float(snapshot.positions_value_tmn),
                    "gross_exposure_pct": snapshot.gross_exposure_pct,
                    "drawdown_pct": snapshot.drawdown_pct * 100,
                    "daily_pnl_pct": snapshot.daily_pnl_pct * 100,
                    "net_return_pct": snapshot.net_return_pct * 100,
                    "realized_pnl": float(snapshot.realized_pnl_tmn),
                    "unrealized_pnl": float(snapshot.unrealized_pnl_tmn),
                    "fees": float(snapshot.fees_tmn),
                }
            )

        # Endgame liquidation at the last known marks.
        for symbol, position in list(portfolio.positions.items()):
            mark = portfolio.marks.get(symbol, position.avg_entry)
            exit_price = mark * Decimal(
                str(1 - self.settings.backtest_slippage_bps / 10_000)
            )
            trade = self._exit_position(
                position,
                exit_price,
                datetime.fromtimestamp(int(union[-1]) / 1e9, tz=UTC),
                portfolio,
                "final_liquidation",
                fills,
            )
            if trade:
                closed_trades.append(trade)
        final_snapshot = portfolio.snapshot(
            datetime.fromtimestamp(int(union[-1]) / 1e9, tz=UTC)
        )
        equity_rows.append(
            {
                "ts": final_snapshot.ts.isoformat(),
                "equity": float(final_snapshot.equity_tmn),
                "cash": float(final_snapshot.cash_tmn),
                "positions_value": float(final_snapshot.positions_value_tmn),
                "gross_exposure_pct": final_snapshot.gross_exposure_pct,
                "drawdown_pct": final_snapshot.drawdown_pct * 100,
                "daily_pnl_pct": final_snapshot.daily_pnl_pct * 100,
                "net_return_pct": final_snapshot.net_return_pct * 100,
                "realized_pnl": float(final_snapshot.realized_pnl_tmn),
                "unrealized_pnl": float(final_snapshot.unrealized_pnl_tmn),
                "fees": float(final_snapshot.fees_tmn),
            }
        )
        equity_df = pd.DataFrame(equity_rows)
        buy_decisions = sum(1 for decision in decisions if decision["action"] == "BUY")
        metrics = compute_metrics(
            equity_df,
            closed_trades,
            initial_capital=float(self.settings.backtest_initial_capital_tmn),
            fees_tmn=float(portfolio.fees_tmn),
            decisions=len(decisions),
            buy_decisions=buy_decisions,
            fills=entry_fill_count,
            interval_minutes=1,
        )
        metrics["symbols"] = symbols
        metrics["period"] = period
        metrics["agent_mode"] = self.settings.backtest_agent_mode
        manifest = data_manifest(self.settings, symbols)
        artifacts = write_backtest_artifacts(
            settings=self.settings,
            run_id=run_id,
            metrics=metrics,
            equity=equity_df,
            trades=closed_trades,
            decisions=decisions,
            data_manifest=manifest,
            git_sha=self.settings.git_sha or "no-git",
            period=period,
        )
        logger.info(
            "backtest complete %s: %.2f%% net, %.2f%% max DD, %d trades",
            run_id,
            metrics.get("net_return_pct", 0),
            metrics.get("max_drawdown_pct", 0),
            metrics.get("trades", 0),
        )
        return BacktestResult(
            run_id=run_id,
            metrics=metrics,
            equity=equity_df,
            trades=closed_trades,
            decisions=decisions,
            artifacts=artifacts,
            period=period,
            data_manifest=manifest,
        )

    async def _decision_step(
        self,
        *,
        bars: dict[str, dict[str, Any]],
        t_dt: datetime,
        portfolio: Portfolio,
        metas: dict[str, MarketMeta],
        fee_rates: dict[str, FeeRate],
        regime: RegimeState,
        pending: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        arrays: dict[str, dict[str, Any]],
        features: dict[str, pd.DataFrame],
    ) -> None:
        blocked = set(portfolio.positions) | {entry["symbol"] for entry in pending}
        candidates = []
        for symbol, bar in bars.items():
            if symbol in blocked or symbol not in metas:
                continue
            ptr = bar["ptr"]
            row = features[symbol].iloc[ptr]
            feature_dict = row_to_features(row, symbol)
            candidate = self.signal_engine.evaluate(
                symbol,
                feature_dict,
                regime,
                fee_taker=fee_rates[symbol].taker,
                fee_maker=fee_rates[symbol].maker,
            )
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(key=lambda item: item.score, reverse=True)
        selected = candidates[: self.settings.max_candidates_per_cycle]
        for candidate in selected:
            book = _synthetic_book(candidate, bars[candidate.symbol]["close"], self.settings)
            meta = metas[candidate.symbol]
            snapshot = portfolio.snapshot(t_dt)
            decision_id = hashlib.sha256(
                f"{candidate.symbol}:{t_dt.isoformat()}:{candidate.score}".encode()
            ).hexdigest()[:24]
            pre = self.risk.pre_check(
                candidate,
                portfolio,
                book,
                regime,
                meta,
                [],
                now=t_dt,
                ws_healthy=True,
                api_error_rate=0.0,
                clock_skew_sec=0.0,
            )
            if not pre.approved:
                decisions.append(
                    _decision_record(
                        decision_id=decision_id,
                        t_dt=t_dt,
                        candidate=candidate,
                        action="HOLD",
                        agent=None,
                        risk=pre.to_dict(),
                        execution={"status": "PRECHECK_VETO"},
                        rationale="; ".join(pre.reasons)[:600],
                        mode="backtest",
                    )
                )
                continue
            result = await self.agent.decide(
                candidate,
                snapshot,
                regime,
                position=None,
                decision_id=decision_id,
            )
            decision = result.decision
            if decision.action != "BUY":
                decisions.append(
                    _decision_record(
                        decision_id=decision_id,
                        t_dt=t_dt,
                        candidate=candidate,
                        action=decision.action.value,
                        agent={
                            "model": result.model,
                            "action": decision.action.value,
                            "confidence": decision.confidence,
                            "rationale": decision.rationale,
                            "risk_flags": decision.risk_flags,
                            "response_hash": result.response_hash,
                        },
                        risk=pre.to_dict(),
                        execution={"status": "NO_TRADE"},
                        rationale=decision.rationale,
                        mode="backtest",
                    )
                )
                continue
            approval = self.risk.approve(
                decision,
                candidate,
                portfolio,
                book,
                meta,
                fee_rate=fee_rates[candidate.symbol],
                position=None,
                open_orders=[],
                now=t_dt,
            )
            if not approval.approved:
                decisions.append(
                    _decision_record(
                        decision_id=decision_id,
                        t_dt=t_dt,
                        candidate=candidate,
                        action="HOLD",
                        agent={
                            "model": result.model,
                            "action": decision.action.value,
                            "confidence": decision.confidence,
                            "rationale": decision.rationale,
                        },
                        risk=approval.to_dict(),
                        execution={"status": "RISK_VETO"},
                        rationale="; ".join(approval.reasons)[:600],
                        mode="backtest",
                    )
                )
                continue
            pending.append(
                {
                    "symbol": candidate.symbol,
                    "order_type": approval.order_type,
                    "limit_price": approval.price,
                    "quantity": approval.quantity,
                    "stop_bps": decision.stop_loss_bps,
                    "take_profit_bps": decision.take_profit_bps,
                    "horizon_sec": decision.horizon_sec,
                    "decision": decision,
                    "candidate": candidate,
                    "decision_id": decision_id,
                    "created_ts": t_dt,
                    "waits": 0,
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
                        "agent": {
                            "model": result.model,
                            "confidence": decision.confidence,
                            "rationale": decision.rationale,
                        },
                    },
                }
            )
            decisions.append(
                _decision_record(
                    decision_id=decision_id,
                    t_dt=t_dt,
                    candidate=candidate,
                    action="BUY",
                    agent={
                        "model": result.model,
                        "action": decision.action.value,
                        "confidence": decision.confidence,
                        "size_fraction": decision.size_fraction,
                        "order_type": decision.order_type,
                        "rationale": decision.rationale,
                        "response_hash": result.response_hash,
                    },
                    risk=approval.to_dict(),
                    execution={
                        "status": "PENDING_ENTRY",
                        "price": str(approval.price),
                        "quantity": str(approval.quantity),
                    },
                    rationale=decision.rationale,
                    mode="backtest",
                )
            )

    async def _swing_decision_step(
        self,
        *,
        swing_rows: dict[str, dict[str, float]],
        t_dt: datetime,
        portfolio: Portfolio,
        metas: dict[str, MarketMeta],
        fee_rates: dict[str, FeeRate],
        regime: RegimeState,
        pending: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> None:
        blocked = set(portfolio.positions) | {entry["symbol"] for entry in pending}
        candidates = []
        for symbol, row in swing_rows.items():
            if symbol in blocked or symbol not in metas:
                continue
            candidate = self.swing_engine.evaluate_values(
                symbol,
                row,
                regime,
                fee_taker=fee_rates[symbol].taker,
                fee_maker=fee_rates[symbol].maker,
            )
            if candidate is not None:
                candidates.append((candidate, row))
        candidates.sort(key=lambda item: item[0].score, reverse=True)
        for candidate, row in candidates[: self.settings.max_candidates_per_cycle]:
            book = _synthetic_book(candidate, float(row["close"]), self.settings)
            meta = metas[candidate.symbol]
            snapshot = portfolio.snapshot(t_dt)
            decision_id = hashlib.sha256(
                f"swing:{candidate.symbol}:{t_dt.isoformat()}:{candidate.score}".encode()
            ).hexdigest()[:24]
            pre = self.risk.pre_check(
                candidate,
                portfolio,
                book,
                regime,
                meta,
                [],
                now=t_dt,
                ws_healthy=True,
                api_error_rate=0.0,
                clock_skew_sec=0.0,
            )
            if not pre.approved:
                decisions.append(
                    _decision_record(
                        decision_id=decision_id,
                        t_dt=t_dt,
                        candidate=candidate,
                        action="HOLD",
                        agent=None,
                        risk=pre.to_dict(),
                        execution={"status": "PRECHECK_VETO"},
                        rationale="; ".join(pre.reasons)[:600],
                        mode="backtest",
                    )
                )
                continue
            result = await self.agent.decide(
                candidate,
                snapshot,
                regime,
                position=None,
                decision_id=decision_id,
            )
            decision = result.decision
            agent_payload = {
                "model": result.model,
                "action": decision.action.value,
                "confidence": decision.confidence,
                "size_fraction": decision.size_fraction,
                "order_type": decision.order_type,
                "limit_offset_bps": decision.limit_offset_bps,
                "take_profit_bps": decision.take_profit_bps,
                "stop_loss_bps": decision.stop_loss_bps,
                "horizon_sec": decision.horizon_sec,
                "rationale": decision.rationale,
                "risk_flags": decision.risk_flags,
                "response_hash": result.response_hash,
            }
            if decision.action != "BUY":
                decisions.append(
                    _decision_record(
                        decision_id=decision_id,
                        t_dt=t_dt,
                        candidate=candidate,
                        action=decision.action.value,
                        agent=agent_payload,
                        risk=pre.to_dict(),
                        execution={"status": "NO_TRADE"},
                        rationale=decision.rationale,
                        mode="backtest",
                    )
                )
                continue
            approval = self.risk.approve(
                decision,
                candidate,
                portfolio,
                book,
                meta,
                fee_rate=fee_rates[candidate.symbol],
                position=None,
                open_orders=[],
                now=t_dt,
            )
            if not approval.approved:
                decisions.append(
                    _decision_record(
                        decision_id=decision_id,
                        t_dt=t_dt,
                        candidate=candidate,
                        action="HOLD",
                        agent=agent_payload,
                        risk=approval.to_dict(),
                        execution={"status": "RISK_VETO"},
                        rationale="; ".join(approval.reasons)[:600],
                        mode="backtest",
                    )
                )
                continue
            pending.append(
                {
                    "symbol": candidate.symbol,
                    "order_type": approval.order_type,
                    "limit_price": approval.price,
                    "quantity": approval.quantity,
                    "stop_bps": decision.stop_loss_bps,
                    "take_profit_bps": decision.take_profit_bps,
                    "horizon_sec": decision.horizon_sec,
                    "decision": decision,
                    "candidate": candidate,
                    "decision_id": decision_id,
                    "created_ts": t_dt,
                    "waits": 0,
                    "entry_meta": {
                        "decision_id": decision_id,
                        "signal": "TREND",
                        "confidence": decision.confidence,
                        "rationale": decision.rationale,
                        "regime": decision.regime,
                        "stop_bps": decision.stop_loss_bps,
                        "take_profit_bps": decision.take_profit_bps,
                        "horizon_sec": decision.horizon_sec,
                        "features": candidate.features,
                        "agent": {
                            "model": result.model,
                            "confidence": decision.confidence,
                            "rationale": decision.rationale,
                        },
                    },
                }
            )
            decisions.append(
                _decision_record(
                    decision_id=decision_id,
                    t_dt=t_dt,
                    candidate=candidate,
                    action="BUY",
                    agent=agent_payload,
                    risk=approval.to_dict(),
                    execution={
                        "status": "PENDING_ENTRY",
                        "price": str(approval.price),
                        "quantity": str(approval.quantity),
                    },
                    rationale=decision.rationale,
                    mode="backtest",
                )
            )

    def _execute_entry(
        self,
        entry: dict[str, Any],
        bar: dict[str, Any],
        t_dt: datetime,
        portfolio: Portfolio,
    ) -> Fill | None:
        symbol = entry["symbol"]
        limit = entry["limit_price"]
        if entry["order_type"] == "MARKETABLE_LIMIT":
            if bar["open"] > float(limit) * 1.00001:
                return None
            price = min(
                float(limit),
                bar["open"] * (1 + self.settings.backtest_slippage_bps / 10_000),
            )
            rate = self.settings.backtest_taker_fee
            liquidity = "taker"
        else:
            fill_price = limit_fill_price(
                bar["open"], bar["high"], bar["low"], limit, Side.BUY
            )
            if fill_price is None:
                return None
            price = float(fill_price)
            rate = self.settings.backtest_maker_fee
            liquidity = "maker"
        quantity = entry["quantity"]
        notional = Decimal(str(price)) * quantity
        fee = fee_tmn(notional, rate)
        fill = Fill(
            fill_id=self._next_fill_id(),
            order_id=f"bt_{entry['decision_id']}",
            symbol=symbol,
            side=Side.BUY,
            price=Decimal(str(price)),
            quantity=quantity,
            fee=fee,
            fee_asset="TMN",
            ts=t_dt,
            liquidity=liquidity,
            decision_id=entry["decision_id"],
        )
        try:
            portfolio.apply_fill(fill, entry_meta=entry["entry_meta"])
        except PortfolioError as exc:
            logger.warning("backtest entry rejected by portfolio: %s", exc)
            return None
        return fill

    def _exit_position(
        self,
        position: Any,
        price: Decimal,
        t_dt: datetime,
        portfolio: Portfolio,
        reason: str,
        fills: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        notional = price * position.quantity
        fee = fee_tmn(notional, self.settings.backtest_taker_fee)
        fill = Fill(
            fill_id=self._next_fill_id(),
            order_id=f"bt_exit_{position.decision_id}",
            symbol=position.symbol,
            side=Side.SELL,
            price=price,
            quantity=position.quantity,
            fee=fee,
            fee_asset="TMN",
            ts=t_dt,
            liquidity="taker",
            decision_id=position.decision_id,
        )
        try:
            trade = portfolio.apply_fill(fill, exit_reason=reason)
        except PortfolioError as exc:
            logger.warning("backtest exit rejected by portfolio: %s", exc)
            return None
        fills.append(fill.to_dict())
        return trade

    def _next_fill_id(self) -> str:
        self._fill_seq += 1
        return f"btfill_{self._fill_seq:09d}"

    @staticmethod
    def _run_id(start: str, end: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        nonce = f"{os.getpid()}:{uuid.uuid4().hex}"
        digest = hashlib.sha256(f"{start}:{end}:{stamp}:{nonce}".encode()).hexdigest()[:8]
        return f"bt_{stamp}_{digest}"


def _synthetic_book(candidate: Any, close: float, settings: Settings) -> OrderBook:
    spread = max(2.0, settings.backtest_slippage_bps)
    mid = close
    bid = mid * (1 - spread / 2 / 10_000)
    ask = mid * (1 + spread / 2 / 10_000)
    depth_tmn = float(candidate.features.get("top5_depth_tmn") or 50_000_000)
    qty = max(1e-8, depth_tmn / mid / 5)
    return OrderBook(
        symbol=candidate.symbol,
        bids=[BookLevel(price=Decimal(str(bid)), quantity=Decimal(str(qty))) for _ in range(5)],
        asks=[BookLevel(price=Decimal(str(ask)), quantity=Decimal(str(qty))) for _ in range(5)],
        source="synthetic",
    )


def _market_meta(symbol: str, frame: pd.DataFrame) -> MarketMeta:
    price = float(frame["close"].iloc[-1])
    if price >= 1000:
        price_precision = 0
    elif price >= 1:
        price_precision = 4
    elif price >= 0.01:
        price_precision = 6
    else:
        price_precision = 8
    return MarketMeta(
        symbol=symbol,
        base_asset=symbol[:-3] if symbol.endswith("TMN") else symbol,
        quote_asset="TMN",
        price_precision=price_precision,
        amount_precision=8,
        price=price,
        min_notional=50_000,
        min_qty=0.0,
    )


def _decision_record(
    *,
    decision_id: str,
    t_dt: datetime,
    candidate: Any,
    action: str,
    agent: dict[str, Any] | None,
    risk: dict[str, Any],
    execution: dict[str, Any],
    rationale: str,
    mode: str,
) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "ts": t_dt.isoformat(),
        "symbol": candidate.symbol,
        "action": action,
        "confidence": (agent or {}).get("confidence", 0.0),
        "signal": candidate.signal,
        "score": candidate.score,
        "mode": mode,
        "candidate": candidate.to_dict(),
        "features": candidate.features,
        "agent": agent or {},
        "risk": risk,
        "execution": execution,
        "rationale": rationale,
        "prompt_hash": "",
        "response_hash": (agent or {}).get("response_hash", ""),
        "latency_ms": 0.0,
    }
