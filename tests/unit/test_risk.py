from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from wallex_trader.agent.schemas import AgentAction, AgentDecision
from wallex_trader.models import BookLevel, Candidate, FeeRate, MarketMeta, OrderBook, Side
from wallex_trader.risk.kernel import RiskKernel
from wallex_trader.risk.killswitch import KillSwitch
from wallex_trader.strategy.portfolio import Portfolio
from wallex_trader.strategy.regime import RegimeState


def setup_objects(settings):
    meta = MarketMeta(
        symbol="BTCTMN",
        base_asset="BTC",
        quote_asset="TMN",
        price_precision=0,
        amount_precision=8,
        min_notional=50_000,
    )
    mid = Decimal("100000")
    book = OrderBook(
        symbol="BTCTMN",
        bids=[BookLevel(mid - Decimal(i * 5), Decimal("100")) for i in range(1, 6)],
        asks=[BookLevel(mid + Decimal(i * 5), Decimal("100")) for i in range(1, 6)],
        source="test",
    )
    candidate = Candidate(
        symbol="BTCTMN",
        side=Side.BUY,
        signal="MR",
        score=0.82,
        expected_edge_bps=180,
        spread_bps=1,
        top5_depth_tmn=50_000_000,
        features={"price": 100000},
        regime="range",
        rationale="test",
        recommended_stop_bps=100,
        recommended_take_profit_bps=200,
        recommended_horizon_sec=600,
    )
    portfolio = Portfolio(settings, Decimal("5000000"))
    regime = RegimeState(
        label="range",
        memo="test",
        btc_ret_5m_bps=0,
        btc_ret_15m_bps=0,
        btc_ret_1h_bps=0,
        volatility_percentile=50,
        average_spread_bps=1,
        btc_trend="up",
        created_at=datetime.now(UTC),
    )
    return meta, book, candidate, portfolio, regime


def make_decision(**overrides):
    values = {
        "action": AgentAction.BUY,
        "confidence": 0.78,
        "size_fraction": 0.5,
        "order_type": "MARKETABLE_LIMIT",
        "limit_offset_bps": 5.0,
        "take_profit_bps": 200.0,
        "stop_loss_bps": 100.0,
        "horizon_sec": 600,
        "regime": "range",
        "rationale": "test",
        "risk_flags": [],
    }
    values.update(overrides)
    return AgentDecision(**values)


def test_risk_approval_sizes_within_caps(settings) -> None:
    meta, book, candidate, portfolio, regime = setup_objects(settings)
    settings.risk_per_trade = 0.02
    kernel = RiskKernel(settings, KillSwitch(settings))
    pre = kernel.pre_check(
        candidate,
        portfolio,
        book,
        regime,
        meta,
        [],
        now=datetime.now(UTC),
        ws_healthy=True,
        api_error_rate=0,
        clock_skew_sec=0,
    )
    assert pre.approved
    approval = kernel.approve(
        make_decision(size_fraction=0.8),
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=FeeRate(0.0025, 0.003),
        position=None,
        open_orders=[],
        now=datetime.now(UTC),
    )
    assert approval.approved
    assert approval.quantity > 0
    assert approval.notional_tmn <= Decimal(str(settings.max_order_notional_tmn))
    assert approval.stop_price < approval.price < approval.take_profit_price


def test_low_confidence_is_vetoed(settings) -> None:
    meta, book, candidate, portfolio, _ = setup_objects(settings)
    kernel = RiskKernel(settings, KillSwitch(settings))
    approval = kernel.approve(
        make_decision(confidence=0.2),
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=FeeRate(0.0025, 0.003),
        position=None,
        open_orders=[],
        now=datetime.now(UTC),
    )
    assert not approval.approved
    assert any("confidence" in reason for reason in approval.reasons)


def test_invalid_stop_is_vetoed(settings) -> None:
    meta, book, candidate, portfolio, _ = setup_objects(settings)
    kernel = RiskKernel(settings, KillSwitch(settings))
    approval = kernel.approve(
        make_decision(stop_loss_bps=1),
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=FeeRate(0.0025, 0.003),
        position=None,
        open_orders=[],
        now=datetime.now(UTC),
    )
    assert not approval.approved


def test_kill_switch_blocks_entries(settings) -> None:
    meta, book, candidate, portfolio, regime = setup_objects(settings)
    kill = KillSwitch(settings)
    kill.activate("test kill")
    kernel = RiskKernel(settings, kill)
    pre = kernel.pre_check(
        candidate,
        portfolio,
        book,
        regime,
        meta,
        [],
        now=datetime.now(UTC),
        ws_healthy=True,
        api_error_rate=0,
        clock_skew_sec=0,
    )
    assert not pre.approved
    assert any(check.name == "kill_switch" and not check.passed for check in pre.checks)


def test_trend_size_floor_rejects_small_ai_size(settings) -> None:
    meta, book, candidate, portfolio, _ = setup_objects(settings)
    candidate.signal = "TREND"
    candidate.recommended_stop_bps = 1000
    candidate.recommended_take_profit_bps = 2500
    candidate.recommended_horizon_sec = 1_728_000
    decision = make_decision(
        size_fraction=0.5,
        stop_loss_bps=1000,
        take_profit_bps=2500,
        horizon_sec=1_728_000,
    )
    kernel = RiskKernel(settings, KillSwitch(settings))
    approval = kernel.approve(
        decision,
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=FeeRate(0.0025, 0.003),
        position=None,
        open_orders=[],
        now=datetime.now(UTC),
    )
    assert not approval.approved
    assert any("TREND size" in reason for reason in approval.reasons)


def test_intraday_sleeve_uses_a_smaller_risk_budget(settings) -> None:
    meta, book, candidate, portfolio, _ = setup_objects(settings)
    settings.risk_per_trade = 0.02
    kernel = RiskKernel(settings, KillSwitch(settings))
    candidate.signal = "TREND"
    trend = kernel.approve(
        make_decision(size_fraction=0.8, stop_loss_bps=1000, take_profit_bps=2500, horizon_sec=1_728_000),
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=FeeRate(0.0025, 0.003),
        position=None,
        open_orders=[],
        now=datetime.now(UTC),
    )
    candidate.signal = "MR"
    intraday = kernel.approve(
        make_decision(size_fraction=0.8, stop_loss_bps=1000, take_profit_bps=2500, horizon_sec=1_728_000),
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=FeeRate(0.0025, 0.003),
        position=None,
        open_orders=[],
        now=datetime.now(UTC),
    )
    assert trend.approved and intraday.approved
    assert intraday.quantity < trend.quantity
    assert intraday.notional_tmn < trend.notional_tmn


def test_order_below_min_notional_is_vetoed(settings) -> None:
    meta, book, candidate, portfolio, _ = setup_objects(settings)
    settings.min_notional_tmn = 400_000
    kernel = RiskKernel(settings, KillSwitch(settings))
    approval = kernel.approve(
        make_decision(),
        candidate,
        portfolio,
        book,
        meta,
        fee_rate=FeeRate(0.0025, 0.003),
        position=None,
        open_orders=[],
        now=datetime.now(UTC),
    )
    assert not approval.approved
    assert any("minimum notional" in reason for reason in approval.reasons)
