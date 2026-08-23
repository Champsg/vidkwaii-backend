"""Redis-backed JSON cache with an automatic in-memory fallback.

The fallback keeps the API fully functional in local development when Redis
is not running, so ``uvicorn main:app`` works out of the box.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("media_hub.cache")


class MediaCache:
    def __init__(self, redis_url: str, ttl_seconds: int = 86400) -> None:
        self.ttl_seconds = ttl_seconds
        self._memory: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()
        self._redis = None
        self._redis_ok = False
        self._last_attempt: float = 0.0
        self._retry_after_seconds: float = 30.0
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as exc:  # pragma: no cover - depends on environment
            logger.warning("Redis client unavailable: %s", exc)

    async def ensure_redis(self) -> bool:
        if self._redis is None:
            return False
        if self._redis_ok:
            return True
        now = time.monotonic()
        if now - self._last_attempt < self._retry_after_seconds:
            return False
        self._last_attempt = now
        try:
            await self._redis.ping()
            self._redis_ok = True
        except Exception as exc:
            self._redis_ok = False
            logger.warning("Redis unreachable, using in-memory cache: %s", exc)
        return self._redis_ok

    async def get(self, key: str) -> Optional[str]:
        if await self.ensure_redis():
            try:
                return await self._redis.get(key)
            except Exception:
                pass
        async with self._lock:
            item = self._memory.get(key)
            if not item:
                return None
            stored_at, payload = item
            if time.monotonic() - stored_at > self.ttl_seconds:
                self._memory.pop(key, None)
                return None
            return payload

    async def set(self, key: str, payload: str) -> None:
        if await self.ensure_redis():
            try:
                await self._redis.set(key, payload, ex=self.ttl_seconds)
                return
            except Exception:
                pass
        async with self._lock:
            self._memory[key] = (time.monotonic(), payload)

    async def clear(self) -> None:
        async with self._lock:
            self._memory.clear()
