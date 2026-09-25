"""One turn at a time per conversation (a Telegram double-tap must not run two turns concurrently)."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Protocol

from redis.asyncio import Redis

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


class TurnLock(Protocol):
    async def acquire(self, conversation_id: str, ttl_s: int) -> str | None: ...
    async def release(self, conversation_id: str, token: str) -> None: ...


class RedisTurnLock:
    """SET NX EX with an owner token; compare-and-delete release never frees someone else's lock."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def acquire(self, conversation_id: str, ttl_s: int) -> str | None:
        token = secrets.token_hex(16)
        ok = await self._redis.set(f"agent:turn:{conversation_id}", token, nx=True, ex=ttl_s)
        return token if ok else None

    async def release(self, conversation_id: str, token: str) -> None:
        await self._redis.eval(_RELEASE, 1, f"agent:turn:{conversation_id}", token)


class InMemoryTurnLock:
    """Single-process lock for development and tests."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[str, float]] = {}
        self._mutex = asyncio.Lock()

    async def acquire(self, conversation_id: str, ttl_s: int) -> str | None:
        async with self._mutex:
            held = self._held.get(conversation_id)
            if held and held[1] > time.monotonic():
                return None
            token = secrets.token_hex(16)
            self._held[conversation_id] = (token, time.monotonic() + ttl_s)
            return token

    async def release(self, conversation_id: str, token: str) -> None:
        async with self._mutex:
            if self._held.get(conversation_id, ("", 0.0))[0] == token:
                del self._held[conversation_id]
