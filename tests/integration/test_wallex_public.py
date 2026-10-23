from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from wallex_trader.exchange.rest import WallexRESTClient

pytestmark = pytest.mark.live


def live_enabled() -> bool:
    return os.environ.get("RUN_LIVE_TESTS") == "1"


@pytest.mark.asyncio
async def test_wallex_public_market_data(settings) -> None:
    if not live_enabled():
        pytest.skip("set RUN_LIVE_TESTS=1 to call the live Wallex API")
    async with WallexRESTClient(settings) as rest:
        markets = await rest.market_metadata()
        assert "BTCTMN" in markets
        depth = await rest.depth("BTCTMN", limit=10)
        assert depth.bids and depth.asks
        assert depth.best_bid is not None and depth.best_ask is not None
        trades = await rest.trades("BTCTMN")
        assert trades
        now = datetime.now(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        candles = await rest.candles(
            "BTCTMN", resolution="1", start=today - timedelta(days=1), end=today
        )
        assert len(candles) > 500
