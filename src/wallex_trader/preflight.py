from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.exchange.mcp import AGENT_ALLOWED_TOOLS, WallexMCPClient
from wallex_trader.exchange.rest import WallexAPIError, WallexRESTClient
from wallex_trader.exchange.ws import WallexWebSocket
from wallex_trader.tradability import blocked_markets

logger = logging.getLogger("trader.preflight")


async def run_preflight(settings: Settings, *, include_ws: bool = True) -> dict[str, Any]:
    settings.ensure_dirs()
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "mode": settings.mode.value,
        "git_sha": settings.git_sha,
        "config_hash": settings.config_hash(),
        "checks": checks,
    }

    def add(name: str, ok: bool, detail: Any = None, required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    rest = WallexRESTClient(settings)
    try:
        await rest.connect()
        try:
            markets = await rest.market_metadata()
            add("wallex_markets", len(markets) > 0, {"markets": len(markets)})
        except WallexAPIError as exc:
            markets = {}
            add("wallex_markets", False, str(exc))
        blocked = blocked_markets(settings.blocked_markets_file)
        allowlist = settings.universe_or_default()
        conflicts = sorted(set(allowlist) & blocked)
        add(
            "tradable_universe",
            not conflicts,
            {
                "allowlist": allowlist,
                "blocked_conflicts": conflicts,
                "blocked_markets": len(blocked),
                "min_notional_tmn": settings.min_notional_tmn,
            },
            required=True,
        )
        try:
            depth = await rest.depth("BTCTMN", limit=10)
            add(
                "wallex_depth",
                bool(depth.bids and depth.asks),
                {
                    "bid": str(depth.best_bid),
                    "ask": str(depth.best_ask),
                    "spread_bps": round(depth.spread_bps, 2),
                    "top5_depth_tmn": round(depth.top5_depth_tmn(), 2),
                },
            )
        except WallexAPIError as exc:
            add("wallex_depth", False, str(exc))
        try:
            trades = await rest.trades("BTCTMN")
            add("wallex_trades", len(trades) > 0, {"recent_trades": len(trades)})
        except WallexAPIError as exc:
            add("wallex_trades", False, str(exc))
        try:
            now = datetime.now(UTC)
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            candles = await rest.candles(
                "BTCTMN", resolution="1", start=today - timedelta(days=1), end=today
            )
            add("wallex_candles_1m", len(candles) > 1000, {"candles_1m": len(candles)})
        except WallexAPIError as exc:
            add("wallex_candles_1m", False, str(exc))

        if settings.has_wallex_key:
            try:
                balances = await rest.balances()
                tmn = Decimal(str((balances.get("TMN") or {}).get("value", 0) or 0))
                add(
                    "wallex_private_balances",
                    True,
                    {
                        "tmn_balance": str(tmn),
                        "assets_with_balance": [
                            asset
                            for asset, item in balances.items()
                            if Decimal(str(item.get("value", 0) or 0)) > 0
                        ][:20],
                    },
                )
                if settings.is_live:
                    add(
                        "live_balance_sufficient",
                        tmn >= Decimal(str(settings.min_notional_tmn)),
                        {
                            "tmn_balance": str(tmn),
                            "min_notional_tmn": settings.min_notional_tmn,
                        },
                    )
                sub_account = (settings.wallex_sub_account_client_id or "").strip()
                if sub_account:
                    try:
                        main_balances = await rest.balances_for_account(None)
                        main_tmn = Decimal(
                            str((main_balances.get("TMN") or {}).get("value", 0) or 0)
                        )
                        add(
                            "api_key_matches_sub_account",
                            abs(main_tmn - tmn) <= Decimal("1"),
                            {
                                "main_account_tmn": str(main_tmn),
                                "sub_account_tmn": str(tmn),
                                "hint": (
                                    "If these differ, the API key is the main-account key. "
                                    "Create an API key inside the prop sub-account, use it as "
                                    "WALLEX_API_KEY, and clear WALLEX_SUB_ACCOUNT_CLIENT_ID."
                                ),
                            },
                            required=settings.is_live,
                        )
                    except WallexAPIError as exc:
                        add(
                            "api_key_matches_sub_account",
                            False,
                            str(exc),
                            required=settings.is_live,
                        )
                else:
                    add(
                        "api_key_matches_sub_account",
                        True,
                        "no sub-account header configured; the key account is used",
                        required=settings.is_live,
                    )
            except WallexAPIError as exc:
                add("wallex_private_balances", False, str(exc), required=settings.is_live)
            try:
                fees = await rest.fees()
                btc_fee = fees.get("BTCTMN")
                add(
                    "wallex_fees",
                    btc_fee is not None,
                    {
                        "BTCTMN": {
                            "maker": btc_fee.maker if btc_fee else None,
                            "taker": btc_fee.taker if btc_fee else None,
                        },
                        "configured_fallback": {
                            "maker": settings.maker_fee,
                            "taker": settings.taker_fee,
                        },
                    },
                )
            except WallexAPIError as exc:
                add("wallex_fees", False, str(exc), required=settings.is_live)
            try:
                orders = await rest.open_orders()
                add("wallex_open_orders", len(orders) == 0, {"count": len(orders)}, required=settings.is_live)
            except WallexAPIError as exc:
                add("wallex_open_orders", False, str(exc), required=settings.is_live)
        else:
            add(
                "wallex_private_balances",
                not settings.is_live,
                "no API key; public-shadow mode only",
                required=settings.is_live,
            )

        add(
            "clock_skew",
            rest.clock_skew_sec is None or abs(rest.clock_skew_sec) <= settings.clock_skew_kill_sec,
            {"skew_sec": rest.clock_skew_sec},
            required=settings.is_live,
        )

        if include_ws:
            ws_result = await _test_ws(settings)
            add("wallex_websocket", ws_result["ok"], ws_result, required=True)

        if settings.has_deepseek_key:
            ds = await _test_deepseek(settings)
            add("deepseek", ds["ok"], ds, required=settings.agent_enabled)
        else:
            add(
                "deepseek",
                not settings.agent_enabled,
                "DEEPSEEK_API_KEY missing; agent would be disabled",
                required=settings.agent_enabled,
            )

        if settings.has_wallex_key:
            mcp = WallexMCPClient(settings)
            try:
                info = await mcp.initialize()
                tools = await mcp.tools()
                add(
                    "wallex_mcp",
                    True,
                    {
                        "server": info.get("serverInfo"),
                        "tools": len(tools),
                        "agent_tools": sorted(
                            str(tool.get("name"))
                            for tool in tools
                            if str(tool.get("name", "")) in AGENT_ALLOWED_TOOLS
                        ),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                add("wallex_mcp", False, str(exc), required=False)
            finally:
                await mcp.close()
        else:
            add("wallex_mcp", not settings.is_live, "no API key", required=settings.is_live)
    finally:
        await rest.close()

    required_ok = all(
        check["ok"] for check in checks if check.get("required")
    )
    result["ok"] = required_ok
    result["checks"] = checks
    path = Path(settings.artifacts_dir) / "preflight" / f"{datetime.now(UTC):%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    result["artifact"] = str(path)
    return result


async def _test_ws(settings: Settings) -> dict[str, Any]:
    messages = 0
    connected = False

    async def on_message(_channel: str, _payload: Any) -> None:
        nonlocal messages
        messages += 1

    async def on_connect() -> None:
        nonlocal connected
        connected = True

    ws = WallexWebSocket(
        settings,
        ["orderbook:BTCTMN@buyDepth", "public:BTCTMN@trade", "candle:BTCTMN@1m"],
        on_message,
        on_connect=on_connect,
    )
    started = time.perf_counter()
    await ws.start()
    try:
        for _ in range(20):
            if messages >= 2 and connected:
                break
            await asyncio.sleep(0.75)
    finally:
        await ws.stop()
    return {
        "ok": connected and messages >= 1,
        "connected": connected,
        "messages": messages,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "reconnects": ws.reconnects,
        "error": ws.last_error,
    }


async def _test_deepseek(settings: Settings) -> dict[str, Any]:
    import httpx

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            base_url=settings.deepseek_base_url.rstrip("/"),
            timeout=45,
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
        ) as client:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": settings.deepseek_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Output JSON only.",
                        },
                        {
                            "role": "user",
                            "content": 'Return exactly {"ok":true,"role":"argus-preflight"}.',
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": 120,
                    "response_format": {"type": "json_object"},
                },
            )
            latency = (time.perf_counter() - started) * 1000
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "status": response.status_code,
                    "error": response.text[:400],
                    "latency_ms": round(latency, 1),
                }
            payload = response.json()
            content = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
            usage = payload.get("usage") or {}
            return {
                "ok": "true" in content,
                "model": payload.get("model"),
                "latency_ms": round(latency, 1),
                "tokens_in": usage.get("prompt_tokens"),
                "tokens_out": usage.get("completion_tokens"),
                "content": content[:200],
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
