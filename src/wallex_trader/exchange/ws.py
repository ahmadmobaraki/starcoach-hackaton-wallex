from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import websockets

from wallex_trader.config import Settings

logger = logging.getLogger("trader.ws")

MessageHandler = Callable[[str, Any], Awaitable[None] | None]
LifecycleHandler = Callable[[], Awaitable[None] | None]


class WallexWebSocket:
    """Centrifugo v2 client for Wallex public channels.

    Order-book channels publish full snapshots that replace the local book.
    Reconnects resync through REST via the on_connect callback.
    """

    def __init__(
        self,
        settings: Settings,
        channels: list[str],
        on_message: MessageHandler,
        on_connect: LifecycleHandler | None = None,
        on_disconnect: LifecycleHandler | None = None,
    ) -> None:
        if len(channels) > 64:
            raise ValueError("Wallex allows at most 64 channels per connection")
        self.settings = settings
        self.channels = list(dict.fromkeys(channels))
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.connected = False
        self.client_id: str | None = None
        self.reconnects = 0
        self.messages = 0
        self.last_message_at: datetime | None = None
        self.last_error: str | None = None
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._next_id = 1

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever(), name="wallex-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close_socket()

    @property
    def age_since_message_ms(self) -> float:
        if self.last_message_at is None:
            return float("inf")
        return (datetime.now(UTC) - self.last_message_at).total_seconds() * 1000

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "channels": len(self.channels),
            "reconnects": self.reconnects,
            "messages": self.messages,
            "last_message_age_ms": round(self.age_since_message_ms, 1)
            if self.last_message_at
            else None,
            "last_error": self.last_error,
        }

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any socket failure
                self.last_error = str(exc)
                logger.warning("WebSocket disconnected: %s", exc)
            finally:
                was_connected = self.connected
                self.connected = False
                await self._close_socket()
                if was_connected and self.on_disconnect is not None:
                    with contextlib.suppress(Exception):
                        result = self.on_disconnect()
                        if asyncio.iscoroutine(result):
                            await result
            if self._stop.is_set():
                break
            self.reconnects += 1
            delay = min(30.0, backoff) + random.random() * 0.5
            logger.info("WebSocket reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            backoff = min(30.0, backoff * 2)

    async def _connect_and_read(self) -> None:
        async with websockets.connect(
            self.settings.wallex_ws_url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
            open_timeout=15,
        ) as ws:
            self._ws = ws
            await self._send({"id": self._next_id, "connect": {}})
            self._next_id += 1
            ack = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(ack)
            if "error" in data:
                raise RuntimeError(f"Centrifugo connect failed: {data['error']}")
            self.client_id = (data.get("connect") or {}).get("client")
            self.connected = True
            logger.info("WebSocket connected (client=%s)", self.client_id)
            for channel in self.channels:
                await self._send(
                    {"id": self._next_id, "subscribe": {"channel": channel}}
                )
                self._next_id += 1
            if self.on_connect is not None:
                result = self.on_connect()
                if asyncio.iscoroutine(result):
                    await result
            async for raw in ws:
                await self._handle_raw(raw)

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            text = raw.decode() if isinstance(raw, bytes) else raw
        except UnicodeDecodeError:
            return
        if text.strip() in ("", "{}"):
            await self._send({})
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        self.messages += 1
        self.last_message_at = datetime.now(UTC)
        push = data.get("push") if isinstance(data, dict) else None
        if not push:
            if isinstance(data, dict) and data.get("error"):
                self.last_error = str(data["error"])
            return
        channel = str(push.get("channel", ""))
        pub = push.get("pub") or {}
        payload = pub.get("data", pub) if isinstance(pub, dict) else pub
        try:
            result = self.on_message(channel, payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - a bad message must not kill the feed
            logger.exception("WS handler failed for %s: %s", channel, exc)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        async with self._send_lock:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))

    async def _close_socket(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()
