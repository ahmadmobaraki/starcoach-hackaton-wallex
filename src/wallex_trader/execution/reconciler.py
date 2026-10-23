from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from wallex_trader.execution.dust import ZERO, base_asset
from wallex_trader.execution.live_broker import LiveBroker
from wallex_trader.execution.shadow_broker import ShadowBroker
from wallex_trader.strategy.portfolio import Portfolio

logger = logging.getLogger("trader.reconciler")


class Reconciler:
    def __init__(
        self,
        broker: ShadowBroker | LiveBroker,
        portfolio: Portfolio,
        *,
        dust: dict[str, Decimal] | None = None,
        cash_tolerance_tmn: Decimal = Decimal("1"),
        quantity_tolerance: Decimal = Decimal("0.00000001"),
    ) -> None:
        self.broker = broker
        self.portfolio = portfolio
        self.dust = dust if dust is not None else {}
        self.cash_tolerance_tmn = cash_tolerance_tmn
        self.quantity_tolerance = quantity_tolerance

    async def check(self) -> dict[str, Any]:
        if isinstance(self.broker, ShadowBroker):
            return {
                "ok": True,
                "mode": "shadow",
                "open_orders": len(self.broker.open_orders()),
                "checks": [{"name": "shadow_state", "passed": True}],
            }
        result = await self.broker.reconcile()
        balances = await self.broker.balances()
        checks: list[dict[str, Any]] = []
        tmn_balance = Decimal(str((balances.get("TMN") or {}).get("value", 0) or 0))
        expected_cash = self.portfolio.cash_tmn
        cash_diff = abs(tmn_balance - expected_cash)
        checks.append(
            {
                "name": "tmn_balance",
                "passed": cash_diff <= self.cash_tolerance_tmn,
                "balance": str(tmn_balance),
                "expected": str(expected_cash),
                "difference": str(cash_diff),
            }
        )
        for symbol, position in self.portfolio.positions.items():
            asset = base_asset(symbol)
            actual = Decimal(str((balances.get(asset) or {}).get("value", 0) or 0))
            known_dust = self.dust.get(asset, ZERO)
            adjusted = max(ZERO, actual - known_dust)
            diff = abs(adjusted - position.quantity)
            checks.append(
                {
                    "name": f"position_{symbol}",
                    "passed": diff <= max(self.quantity_tolerance, position.quantity * Decimal("0.001")),
                    "balance": str(actual),
                    "dust": str(known_dust),
                    "adjusted_balance": str(adjusted),
                    "expected": str(position.quantity),
                    "difference": str(diff),
                }
            )
        mismatch = bool(result.get("mismatch")) or any(not check["passed"] for check in checks)
        if mismatch:
            logger.error("reconciliation mismatch: %s", {"orders": result, "checks": checks})
        return {
            "ok": not mismatch,
            "mode": "live",
            "orders": result,
            "checks": checks,
        }
