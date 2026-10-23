from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.models import ZERO


@dataclass(slots=True)
class RiskCheck:
    name: str
    passed: bool
    value: Any = None
    limit: Any = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "value": self.value,
            "limit": self.limit,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RiskApproval:
    approved: bool
    checks: list[RiskCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    quantity: Decimal = ZERO
    price: Decimal = ZERO
    notional_tmn: Decimal = ZERO
    fee_tmn: Decimal = ZERO
    stop_price: Decimal = ZERO
    take_profit_price: Decimal = ZERO
    order_type: str = "LIMIT"
    max_notional_tmn: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "checks": [check.to_dict() for check in self.checks],
            "reasons": self.reasons,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "notional_tmn": str(self.notional_tmn),
            "fee_tmn": str(self.fee_tmn),
            "stop_price": str(self.stop_price),
            "take_profit_price": str(self.take_profit_price),
            "order_type": self.order_type,
            "max_notional_tmn": str(self.max_notional_tmn),
        }


class RiskLimits:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def to_dict(self) -> dict[str, Any]:
        s = self.settings
        return {
            "risk_per_trade": s.risk_per_trade,
            "max_position_pct": s.max_position_pct,
            "max_gross_exposure": s.max_gross_exposure,
            "max_portfolio_heat": s.max_portfolio_heat,
            "max_open_positions": s.max_open_positions,
            "max_correlated_exposure": s.max_correlated_exposure,
            "min_cash_buffer": s.min_cash_buffer,
            "daily_loss_limit": s.daily_loss_limit,
            "max_drawdown": s.max_drawdown,
            "min_confidence": s.min_confidence,
            "max_slippage_bps": s.max_slippage_bps,
            "max_spread_bps": s.max_spread_bps,
            "max_order_notional_tmn": s.max_order_notional_tmn,
            "min_notional_tmn": s.min_notional_tmn,
            "stop_loss_bps": s.stop_loss_bps,
            "take_profit_bps": s.take_profit_bps,
            "time_stop_sec": s.time_stop_sec,
        }
