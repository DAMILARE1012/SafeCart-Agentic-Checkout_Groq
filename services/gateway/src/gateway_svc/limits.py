"""Fixed-window rate limiting and one-shot de-duplication (Redis in deployments, in-process for dev/tests)."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from redis.asyncio import Redis


class RateLimiter(Protocol):
    async def allow(self, key: str, limit: int, window_s: int) -> bool: ...


class Deduper(Protocol):
    async def first_time(self, key: str, ttl_s: int) -> bool: ...


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def allow(self, key: str, limit: int, window_s: int) -> bool:
        bucket = f"rl:{key}:{int(time.time() // window_s)}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(bucket)
            pipe.expire(bucket, window_s + 1)
            count, _ = await pipe.execute()
        return int(count) <= limit


class RedisDeduper:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def first_time(self, key: str, ttl_s: int) -> bool:
        return bool(await self._redis.set(f"dedupe:{key}", "1", nx=True, ex=ttl_s))


class InMemoryLimits:
    """Both protocols, in-process. Development and tests only."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._seen: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_s: int) -> bool:
        bucket = f"{key}:{int(time.time() // window_s)}"
        async with self._lock:
            self._counts[bucket] = self._counts.get(bucket, 0) + 1
            return self._counts[bucket] <= limit

    async def first_time(self, key: str, ttl_s: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            if self._seen.get(key, 0) > now:
                return False
            self._seen[key] = now + ttl_s
            return True
