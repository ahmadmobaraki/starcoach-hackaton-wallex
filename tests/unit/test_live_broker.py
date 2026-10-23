from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wallex_trader.config import LIVE_CONFIRM_PHRASE, Settings
from wallex_trader.execution.live_broker import LiveBroker
from wallex_trader.models import (
    FeeRate,
    MarketMeta,
    Order,
    OrderStatus,
    OrderType,
    Side,
)


class FakeREST:
    async def create_order(self, payload: dict) -> dict:
        return {
            "order": {
                "id": "ex-1",
                "clientOrderId": payload["client_id"],
                "status": "FILLED",
                "executedQuantity": "1",
                "averagePrice": "100",
                "fills": [
                    {
                        "price": "100",
                        "quantity": "1",
                        "fee": "0.3",
                        "feeAsset": "TMN",
                    }
                ],
            }
        }

    async def get_order(self, client_order_id: str) -> dict | None:
        return None


@pytest.mark.asyncio
async def test_live_broker_preserves_oms_entry_metadata(settings) -> None:
    live_settings = Settings(
        _env_file=None,
        mode="live",
        live_confirm=LIVE_CONFIRM_PHRASE,
        wallex_api_key="test-key",
        data_dir=settings.data_dir,
        artifacts_dir=settings.artifacts_dir,
        logs_dir=settings.logs_dir,
        cache_dir=settings.cache_dir,
        run_state_dir=settings.run_state_dir,
        journal_path=settings.journal_path,
        postgres_dsn=settings.postgres_dsn,
        max_order_notional_tmn=250_000,
        max_position_pct=0.10,
        risk_per_trade=0.005,
    )
    broker = LiveBroker(live_settings, FakeREST())  # type: ignore[arg-type]
    order = Order(
        client_order_id="argus_test_1",
        exchange_order_id=None,
        symbol="BTCTMN",
        side=Side.BUY,
        order_type=OrderType.MARKETABLE_LIMIT,
        price=Decimal("100"),
        quantity=Decimal("1"),
        status=OrderStatus.CREATED,
        raw_request={
            "entry_meta": {
                "decision_id": "dec-1",
                "signal": "TREND",
                "stop_bps": 1000,
                "take_profit_bps": 2500,
                "horizon_sec": 1_728_000,
            }
        },
    )
    meta = MarketMeta(
        symbol="BTCTMN",
        base_asset="BTC",
        quote_asset="TMN",
        price_precision=0,
        amount_precision=8,
    )
    fills = await broker.place(
        order,
        book=None,
        meta=meta,
        fee_rate=FeeRate(0.0025, 0.003),
        now=datetime.now(UTC),
    )
    assert fills and order.status == OrderStatus.FILLED
    assert order.raw_request["entry_meta"]["horizon_sec"] == 1_728_000
    assert "exchange_payload" in order.raw_request
