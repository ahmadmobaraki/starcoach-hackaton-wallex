from __future__ import annotations

import asyncio
import time
from collections import deque


class TokenBucket:
    """Simple async token bucket with a rolling-window order budget."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None) -> None:
        self.rate = max(0.01, rate_per_sec)
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            async with self.lock:
                now = time.monotonic()
                elapsed = now - self.updated
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
                needed = (tokens - self.tokens) / self.rate
            if time.monotonic() + needed > deadline:
                raise TimeoutError("rate limiter acquire timeout")
            await asyncio.sleep(min(needed, 0.5))


class SlidingWindowLimiter:
    """Rolling window limiter for exchange order rate limits (e.g. 20/10s)."""

    def __init__(self, limit: int, window_sec: float = 10.0) -> None:
        self.limit = limit
        self.window = window_sec
        self.events: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0] >= self.window:
                    self.events.popleft()
                if len(self.events) < self.limit:
                    self.events.append(now)
                    return
                wait = self.window - (now - self.events[0]) + 0.01
            if time.monotonic() + wait > deadline:
                raise TimeoutError("order rate limit budget exhausted")
            await asyncio.sleep(min(wait, 0.5))
