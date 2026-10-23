from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from wallex_trader.agent.schemas import AgentDecision, parse_json_object


def test_buy_decision_hash_is_stable() -> None:
    decision = AgentDecision(
        action="BUY",
        confidence=0.7,
        size_fraction=0.4,
        order_type="LIMIT",
        limit_offset_bps=2,
        take_profit_bps=200,
        stop_loss_bps=100,
        horizon_sec=900,
        regime="range",
        rationale="test",
        risk_flags=[],
    )
    assert decision.response_hash() == decision.response_hash()
    assert len(decision.response_hash()) == 64


def test_hold_forces_zero_size() -> None:
    decision = AgentDecision(
        action="HOLD",
        confidence=0.7,
        size_fraction=0.4,
        order_type="LIMIT",
        take_profit_bps=200,
        stop_loss_bps=100,
        horizon_sec=900,
        regime="range",
        rationale="wait",
    )
    assert decision.size_fraction == 0


def test_buy_requires_positive_size() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(
            action="BUY",
            confidence=0.7,
            size_fraction=0,
            order_type="LIMIT",
            take_profit_bps=200,
            stop_loss_bps=100,
            horizon_sec=900,
            regime="range",
            rationale="no",
        )


def test_take_profit_must_cover_stop() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(
            action="BUY",
            confidence=0.7,
            size_fraction=0.4,
            order_type="LIMIT",
            take_profit_bps=50,
            stop_loss_bps=100,
            horizon_sec=900,
            regime="range",
            rationale="bad",
        )


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(
            action="HOLD",
            confidence=0.5,
            size_fraction=0,
            order_type="LIMIT",
            take_profit_bps=200,
            stop_loss_bps=100,
            horizon_sec=900,
            rationale="wait",
            leverage=10,
        )


def test_parse_json_object_tolerates_fences() -> None:
    payload = {"action": "HOLD", "confidence": 0.5}
    assert parse_json_object(f"```json\n{json.dumps(payload)}\n```") == payload
    assert parse_json_object(f"prefix {json.dumps(payload)} suffix") == payload
