from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.execution.precision import (
    apply_bps,
    floor_quantity,
    quantize_price,
)
from wallex_trader.models import (
    ZERO,
    Candidate,
    FeeRate,
    MarketMeta,
    Order,
    OrderBook,
    OrderType,
    Position,
    Side,
)
from wallex_trader.risk.killswitch import KillSwitch
from wallex_trader.risk.limits import RiskApproval, RiskCheck
from wallex_trader.strategy.portfolio import CORRELATED, Portfolio
from wallex_trader.strategy.regime import RegimeState


class RiskKernel:
    def __init__(self, settings: Settings, kill_switch: KillSwitch) -> None:
        self.settings = settings
        self.kill_switch = kill_switch

    def pre_check(
        self,
        candidate: Candidate,
        portfolio: Portfolio,
        book: OrderBook | None,
        regime: RegimeState,
        meta: MarketMeta | None,
        open_orders: list[Order],
        *,
        now: datetime,
        ws_healthy: bool = True,
        api_error_rate: float = 0.0,
        clock_skew_sec: float | None = None,
    ) -> RiskApproval:
        checks: list[RiskCheck] = []
        reasons: list[str] = []
        equity = portfolio.equity_tmn()
        s = self.settings

        def check(name: str, passed: bool, value: Any = None, limit: Any = None, reason: str = "") -> None:
            checks.append(
                RiskCheck(name=name, passed=passed, value=value, limit=limit, reason=reason)
            )
            if not passed:
                reasons.append(reason or name)

        check("kill_switch", not self.kill_switch.state.active, limit="inactive", reason="kill switch active")
        check(
            "allowlist",
            candidate.symbol in s.universe_allowlist,
            value=candidate.symbol,
            reason="symbol outside allowlist",
        )
        if meta is not None:
            check("market_flags", not meta.is_risky, reason="market flagged risky")
            check("market_spot", meta.is_spot and meta.is_tmn_based, reason="not a TMN spot market")
        else:
            check("market_meta", False, reason="missing market metadata")
        check(
            "data_fresh",
            bool(book and book.is_healthy(s.max_depth_age_ms)),
            value=book.age_ms(now) if book else None,
            limit=s.max_depth_age_ms,
            reason="order book missing or stale",
        )
        check(
            "spread",
            bool(book and book.spread_bps <= s.max_spread_to_trade_bps),
            value=round(book.spread_bps, 2) if book else None,
            limit=s.max_spread_to_trade_bps,
            reason="spread above entry limit",
        )
        depth = book.top5_depth_tmn() if book else 0.0
        check(
            "depth",
            depth >= s.max_order_notional_tmn * 5,
            value=round(depth, 2),
            limit=s.max_order_notional_tmn * 5,
            reason="top-5 depth below 5x max order",
        )
        score_floor = (
            s.swing_min_score if candidate.signal == "TREND" else s.candidate_threshold
        )
        check(
            "score",
            candidate.score >= score_floor,
            value=candidate.score,
            limit=score_floor,
            reason="signal score below threshold",
        )
        check(
            "expected_edge",
            candidate.expected_edge_bps >= s.alpha_threshold_bps,
            value=candidate.expected_edge_bps,
            limit=s.alpha_threshold_bps,
            reason="expected edge below alpha threshold",
        )
        check(
            "daily_loss",
            portfolio.daily_pnl_pct() > -s.daily_loss_limit,
            value=round(portfolio.daily_pnl_pct(), 5),
            limit=-s.daily_loss_limit,
            reason="daily loss limit reached",
        )
        check(
            "drawdown",
            portfolio.drawdown_pct() < s.max_drawdown,
            value=round(portfolio.drawdown_pct(), 5),
            limit=s.max_drawdown,
            reason="drawdown kill level reached",
        )
        check(
            "hard_de_risk",
            portfolio.drawdown_pct() < s.hard_de_risk_drawdown_pct,
            value=round(portfolio.drawdown_pct(), 5),
            limit=s.hard_de_risk_drawdown_pct,
            reason="hard de-risk drawdown level reached",
        )
        check(
            "losing_streak",
            not portfolio.cooldown_active(now),
            reason="cooldown after consecutive losses",
        )
        check(
            "open_positions",
            len(portfolio.open_symbols()) < s.max_open_positions,
            value=len(portfolio.open_symbols()),
            limit=s.max_open_positions,
            reason="maximum open positions reached",
        )
        check(
            "open_orders",
            len([order for order in open_orders if order.status in _OPEN_STATUSES]) < s.max_open_orders,
            value=len(open_orders),
            limit=s.max_open_orders,
            reason="too many open orders",
        )
        check(
            "volatility",
            regime.volatility_percentile <= s.max_volatility_percentile,
            value=round(regime.volatility_percentile, 1),
            limit=s.max_volatility_percentile,
            reason="volatility percentile above entry limit",
        )
        check(
            "cash_buffer",
            portfolio.cash_buffer_pct() - (s.max_order_notional_tmn / max(1.0, float(equity)))
            >= s.min_cash_buffer,
            value=round(portfolio.cash_buffer_pct(), 4),
            limit=s.min_cash_buffer,
            reason="cash buffer would be breached",
        )
        check(
            "gross_exposure",
            portfolio.gross_exposure_pct() < s.max_gross_exposure,
            value=round(portfolio.gross_exposure_pct(), 4),
            limit=s.max_gross_exposure,
            reason="gross exposure cap reached",
        )
        check("ws_health", ws_healthy, reason="market data websocket unhealthy")
        check(
            "api_error_rate",
            api_error_rate <= 0.10,
            value=round(api_error_rate, 4),
            limit=0.10,
            reason="API error rate above 10%",
        )
        if clock_skew_sec is not None:
            check(
                "clock_skew",
                abs(clock_skew_sec) <= s.clock_skew_kill_sec,
                value=round(clock_skew_sec, 3),
                limit=s.clock_skew_kill_sec,
                reason=f"clock skew above {s.clock_skew_kill_sec:.1f}s",
            )
        if s.challenge_end:
            remaining = (s.challenge_end - now).total_seconds()
            check(
                "endgame_entries",
                remaining > s.no_new_entries_before_end_sec,
                value=round(remaining, 1),
                limit=s.no_new_entries_before_end_sec,
                reason="no new entries in the challenge endgame window",
            )
        approved = all(item.passed for item in checks)
        return RiskApproval(
            approved=approved,
            checks=checks,
            reasons=reasons,
            max_notional_tmn=Decimal(str(s.max_order_notional_tmn)),
        )

    def approve(
        self,
        decision: Any,
        candidate: Candidate,
        portfolio: Portfolio,
        book: OrderBook,
        meta: MarketMeta,
        *,
        fee_rate: FeeRate | None = None,
        position: Position | None = None,
        open_orders: list[Order] | None = None,
        now: datetime,
    ) -> RiskApproval:
        s = self.settings
        checks: list[RiskCheck] = []
        reasons: list[str] = []

        def check(name: str, passed: bool, value: Any = None, limit: Any = None, reason: str = "") -> None:
            checks.append(
                RiskCheck(name=name, passed=passed, value=value, limit=limit, reason=reason)
            )
            if not passed:
                reasons.append(reason or name)

        if decision.action == "HOLD":
            return RiskApproval(
                approved=False,
                checks=[RiskCheck(name="action", passed=False, value="HOLD", reason="agent chose HOLD")],
                reasons=["agent chose HOLD"],
            )
        if decision.action != "BUY":
            return RiskApproval(
                approved=False,
                checks=[RiskCheck(name="action", passed=False, value=decision.action, reason="only BUY entries are allowed")],
                reasons=["only BUY entries are allowed"],
            )
        check("no_position", position is None, reason="position already open for symbol")
        check(
            "confidence",
            decision.confidence >= max(s.min_confidence, s.agent_min_confidence),
            value=decision.confidence,
            limit=max(s.min_confidence, s.agent_min_confidence),
            reason="agent confidence below minimum",
        )
        check(
            "size_fraction",
            0 < decision.size_fraction <= 1,
            value=decision.size_fraction,
            reason="invalid agent size fraction",
        )
        if candidate.signal == "TREND":
            check(
                "trend_size_fraction",
                decision.size_fraction >= s.agent_min_size_fraction,
                value=decision.size_fraction,
                limit=s.agent_min_size_fraction,
                reason="TREND size fraction below the configured minimum",
            )
        check(
            "stop_bounds",
            s.min_stop_bps <= decision.stop_loss_bps <= s.max_stop_bps,
            value=decision.stop_loss_bps,
            limit=[s.min_stop_bps, s.max_stop_bps],
            reason="stop outside allowed bounds",
        )
        check(
            "take_profit_bounds",
            s.min_take_profit_bps <= decision.take_profit_bps <= s.max_take_profit_bps,
            value=decision.take_profit_bps,
            limit=[s.min_take_profit_bps, s.max_take_profit_bps],
            reason="take-profit outside allowed bounds",
        )
        horizon_floor = 7 * 86400 if candidate.signal == "TREND" else 60
        check(
            "horizon",
            horizon_floor <= decision.horizon_sec <= 2_592_000,
            value=decision.horizon_sec,
            limit=[horizon_floor, 2_592_000],
            reason="horizon outside allowed bounds",
        )
        order_type = str(decision.order_type)
        check(
            "order_type",
            order_type in ("LIMIT", "MARKETABLE_LIMIT"),
            value=order_type,
            reason="unsupported order type",
        )
        if book.best_bid is None or book.best_ask is None:
            reasons.append("missing best bid/ask")
            return RiskApproval(approved=False, checks=checks, reasons=reasons)
        mid = book.mid or book.best_bid
        if order_type == "MARKETABLE_LIMIT":
            raw_price = apply_bps(book.best_ask, decision.limit_offset_bps, up=True)
            cap = apply_bps(book.best_ask, s.max_slippage_bps, up=True)
            raw_price = min(raw_price, cap)
            price = quantize_price(raw_price, meta.tick, Side.BUY, aggressive=True)
        else:
            raw_price = apply_bps(book.best_bid, decision.limit_offset_bps, up=False)
            price = quantize_price(raw_price, meta.tick, Side.BUY, aggressive=False)
        slippage_bps = float((price - book.best_ask) / mid * Decimal(10_000)) if mid > 0 else 0.0
        check(
            "price_sanity",
            abs(float((price - mid) / mid)) <= 0.02 if mid > 0 else False,
            value=str(price),
            reason="limit price more than 2% from mid",
        )
        check(
            "slippage",
            slippage_bps <= s.max_slippage_bps,
            value=round(slippage_bps, 2),
            limit=s.max_slippage_bps,
            reason="estimated slippage above limit",
        )
        check(
            "self_trade",
            not _opposite_open_order(open_orders or [], candidate.symbol, Side.BUY),
            reason="opposite own order exists (self-trade prevention)",
        )

        equity = portfolio.equity_tmn()
        fee = fee_rate or FeeRate(maker=s.maker_fee, taker=s.taker_fee)
        fee_rate_value = (
            fee.taker if order_type == "MARKETABLE_LIMIT" else fee.maker
        )
        risk_amount = equity * Decimal(str(s.risk_per_trade))
        stop_distance = price * Decimal(str(decision.stop_loss_bps)) / Decimal(10_000)
        qty_risk = risk_amount / stop_distance if stop_distance > 0 else ZERO
        qty_position = equity * Decimal(str(s.max_position_pct)) / price
        top5_depth = Decimal(str(book.top5_depth_tmn()))
        qty_liquidity = top5_depth * Decimal(str(s.max_depth_fraction)) / price
        qty_order = Decimal(str(s.max_order_notional_tmn)) / price
        available_cash = max(ZERO, portfolio.cash_tmn - portfolio.reserved_cash_tmn)
        qty_cash = available_cash / (price * (Decimal(1) + Decimal(str(fee_rate_value))))
        max_qty = min(qty_risk, qty_position, qty_liquidity, qty_order, qty_cash)
        de_risk_scale = 0.5 if portfolio.drawdown_pct() >= s.de_risk_drawdown_pct else 1.0
        if de_risk_scale < 1.0:
            max_qty *= Decimal(str(de_risk_scale))
        if candidate.signal != "TREND":
            max_qty *= Decimal(str(max(0.0, min(1.0, s.intraday_risk_multiplier))))
        minimum_notional = Decimal(str(max(s.min_notional_tmn, meta.min_notional)))
        min_size_fraction = (
            minimum_notional / (max_qty * price) if max_qty > 0 and price > 0 else Decimal(1)
        )
        check(
            "min_size_fraction",
            Decimal(str(decision.size_fraction)) >= min_size_fraction,
            value=round(decision.size_fraction, 4),
            limit=str(round(float(min_size_fraction), 4)),
            reason="agent size fraction is below the minimum notional",
        )
        qty = floor_quantity(max_qty * Decimal(str(decision.size_fraction)), meta.step)
        notional = qty * price
        fee_tmn = notional * Decimal(str(fee_rate_value))
        checks.append(
            RiskCheck(
                name="size_cap",
                passed=qty > 0 and notional <= Decimal(str(s.max_order_notional_tmn)) + Decimal("1"),
                value=str(notional),
                limit=s.max_order_notional_tmn,
                reason="size cap",
            )
        )
        checks.append(
            RiskCheck(
                name="soft_de_risk",
                passed=True,
                value=de_risk_scale,
                limit=s.de_risk_drawdown_pct,
                reason="half size after soft drawdown" if de_risk_scale < 1 else "full size",
            )
        )
        if qty <= 0:
            reasons.append("risk-approved quantity rounds to zero")
        check(
            "min_notional",
            notional >= Decimal(str(max(s.min_notional_tmn, meta.min_notional))),
            value=str(notional),
            limit=max(s.min_notional_tmn, meta.min_notional),
            reason="order below minimum notional",
        )
        new_gross = (
            portfolio.positions_value_tmn() + notional
        ) / equity if equity > 0 else Decimal(1)
        check(
            "gross_exposure_after",
            new_gross <= Decimal(str(s.max_gross_exposure)),
            value=str(round(float(new_gross), 4)),
            limit=s.max_gross_exposure,
            reason="gross exposure cap after order",
        )
        heat_after = portfolio.portfolio_heat_pct() + float(
            qty * Decimal(str(decision.stop_loss_bps)) / Decimal(10_000) * price / equity
        )
        check(
            "portfolio_heat_after",
            heat_after <= s.max_portfolio_heat,
            value=round(heat_after, 5),
            limit=s.max_portfolio_heat,
            reason="portfolio heat cap after order",
        )
        correlated_after = portfolio.correlated_exposure_pct() + (
            float(notional / equity) if candidate.symbol in CORRELATED and equity > 0 else 0.0
        )
        check(
            "correlated_exposure",
            correlated_after <= s.max_correlated_exposure,
            value=round(correlated_after, 4),
            limit=s.max_correlated_exposure,
            reason="correlated exposure cap after order",
        )
        cash_after = (portfolio.cash_tmn - portfolio.reserved_cash_tmn - notional - fee_tmn) / equity
        check(
            "cash_buffer_after",
            cash_after >= Decimal(str(s.min_cash_buffer)),
            value=str(round(float(cash_after), 4)),
            limit=s.min_cash_buffer,
            reason="cash buffer cap after order",
        )
        approved = all(item.passed for item in checks)
        return RiskApproval(
            approved=approved,
            checks=checks,
            reasons=reasons,
            quantity=qty if approved else ZERO,
            price=price,
            notional_tmn=notional if approved else ZERO,
            fee_tmn=fee_tmn,
            stop_price=price * (Decimal(1) - Decimal(str(decision.stop_loss_bps)) / Decimal(10_000)),
            take_profit_price=price
            * (Decimal(1) + Decimal(str(decision.take_profit_bps)) / Decimal(10_000)),
            order_type=order_type,
            max_notional_tmn=Decimal(str(s.max_order_notional_tmn)),
        )

    def approve_exit(
        self,
        position: Position,
        book: OrderBook | None,
        meta: MarketMeta,
        *,
        reason: str,
        fee_rate: FeeRate | None = None,
        now: datetime,
        fallback_price: Decimal | None = None,
        offset_bps: float | None = None,
    ) -> RiskApproval:
        s = self.settings
        checks: list[RiskCheck] = []
        reasons: list[str] = []
        price: Decimal
        if book is not None and book.best_bid is not None:
            offset = offset_bps if offset_bps is not None else max(10.0, s.max_slippage_bps / 2)
            raw = apply_bps(book.best_bid, offset, up=False)
            floor_price = apply_bps(book.best_bid, 200.0, up=False)
            price = quantize_price(max(raw, floor_price), meta.tick, Side.SELL, aggressive=True)
            checks.append(RiskCheck(name="book", passed=True, value=round(book.spread_bps, 2)))
        elif fallback_price is not None and fallback_price > 0:
            price = quantize_price(fallback_price, meta.tick, Side.SELL, aggressive=True)
            checks.append(
                RiskCheck(name="book", passed=False, value="fallback_price", reason="using fallback mark")
            )
        else:
            return RiskApproval(
                approved=False,
                checks=[RiskCheck(name="book", passed=False, reason="no exit price available")],
                reasons=["no exit price available"],
            )
        qty = floor_quantity(position.quantity, meta.step)
        if qty <= 0:
            return RiskApproval(
                approved=False,
                checks=checks,
                reasons=["position quantity rounds to zero"],
            )
        fee = fee_rate or FeeRate(maker=s.maker_fee, taker=s.taker_fee)
        fee_tmn = qty * price * Decimal(str(fee.taker))
        checks.append(
            RiskCheck(
                name="risk_reducing",
                passed=True,
                value=reason,
                reason="exit orders bypass halt for risk reduction",
            )
        )
        return RiskApproval(
            approved=True,
            checks=checks,
            reasons=reasons,
            quantity=qty,
            price=price,
            notional_tmn=qty * price,
            fee_tmn=fee_tmn,
            order_type=OrderType.MARKETABLE_LIMIT.value,
            max_notional_tmn=Decimal(str(s.max_order_notional_tmn)),
        )


_OPEN_STATUSES = {"CREATED", "PENDING_SEND", "SENT", "OPEN", "PARTIAL", "UNKNOWN"}


def _opposite_open_order(orders: list[Order], symbol: str, side: Side) -> bool:
    opposite = Side.SELL if side == Side.BUY else Side.BUY
    return any(
        order.symbol == symbol
        and order.side == opposite
        and order.status.value in _OPEN_STATUSES
        and order.remaining > 0
        for order in orders
    )
