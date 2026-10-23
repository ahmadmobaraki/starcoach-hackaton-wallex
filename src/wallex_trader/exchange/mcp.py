from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from wallex_trader.config import Settings
from wallex_trader.exchange.auth import private_headers

logger = logging.getLogger("trader.mcp")

AGENT_ALLOWED_TOOLS = {
    "wallex_basic_get_market_depth",
    "wallex_basic_get_latest_trades",
    "wallex_basic_candle_history",
    "wallex_basic_get_account_balances",
    "wallex_spot_get_open_order",
    "wallex_spot_get_orders",
    "wallex_spot_get_orders_client",
}

STATE_CHANGING_MARKERS = (
    "create",
    "cancel",
    "add_",
    "withdraw",
    "transfer",
    "credit",
    "margin",
    "close",
    "modify",
)


class MCPError(RuntimeError):
    pass


class WallexMCPClient:
    """Minimal MCP JSON-RPC client. Read-only tools only for the trading agent."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._initialized = False
        self._next_id = 1
        self._tools: list[dict[str, Any]] = []
        self._tools_fetched_at = 0.0
        self.calls = 0
        self.errors = 0
        self.latencies: list[float] = []
        self.tool_log: list[dict[str, Any]] = []

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            http2=True,
            headers={
                **private_headers(self.settings),
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._initialized = False

    async def initialize(self) -> dict[str, Any]:
        await self.connect()
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "argus", "version": "1.0"},
            },
        )
        self._initialized = True
        logger.info("Wallex MCP initialized: %s", result.get("serverInfo"))
        return result

    async def tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tools and not refresh and time.time() - self._tools_fetched_at < 300:
            return self._tools
        result = await self._rpc("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        self._tools = list(tools)
        self._tools_fetched_at = time.time()
        return self._tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_state_changing: bool = False,
    ) -> Any:
        if not allow_state_changing:
            if name not in AGENT_ALLOWED_TOOLS or any(
                marker in name.lower() for marker in STATE_CHANGING_MARKERS
            ):
                raise MCPError(f"tool {name} is not allowed for the trading agent")
        started = time.perf_counter()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        latency_ms = (time.perf_counter() - started) * 1000
        self.tool_log.append(
            {
                "ts": time.time(),
                "tool": name,
                "arguments": arguments or {},
                "latency_ms": round(latency_ms, 1),
                "ok": not bool(result.get("isError")) if isinstance(result, dict) else True,
            }
        )
        self.tool_log = self.tool_log[-200:]
        if isinstance(result, dict) and result.get("isError"):
            raise MCPError(f"MCP tool {name} returned an error: {result}")
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = str(item.get("text", ""))
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
        return result

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        started = time.perf_counter()
        for attempt in range(2):
            self.calls += 1
            try:
                response = await self._client.post(self.settings.wallex_mcp_url, json=payload)
                self.latencies.append((time.perf_counter() - started) * 1000)
                self.latencies = self.latencies[-500:]
                if response.status_code >= 400:
                    raise MCPError(
                        f"MCP HTTP {response.status_code}: {response.text[:400]}"
                    )
                data = _parse_mcp_body(response.text)
                if not isinstance(data, dict):
                    raise MCPError("MCP returned an unexpected payload")
                if "error" in data:
                    raise MCPError(f"MCP error: {data['error']}")
                return data.get("result", {})
            except (httpx.TransportError, MCPError) as exc:
                self.errors += 1
                if attempt == 1:
                    raise MCPError(f"MCP {method} failed: {exc}") from exc
                import asyncio

                await asyncio.sleep(0.5)
        raise MCPError(f"MCP {method} failed")

    def stats(self) -> dict[str, Any]:
        latencies = sorted(self.latencies)
        return {
            "calls": self.calls,
            "errors": self.errors,
            "latency_p95_ms": round(latencies[int(len(latencies) * 0.95) - 1], 1)
            if latencies
            else 0.0,
            "tools_cached": len(self._tools),
        }


def _parse_mcp_body(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("event:") or "\ndata:" in stripped:
        data_lines = [
            line[5:].strip()
            for line in stripped.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            return json.loads("\n".join(data_lines))
    return json.loads(stripped)
