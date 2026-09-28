"""Indexer response cache backends (M25).

Two backends behind one interface:

- MemoryIndexerCache: process-local dict with TTL expiry and an LRU
  size cap. Always available, zero infra.
- RedisIndexerCache: durable across restarts, shared by every worker.
- AutoIndexerCache: tries Redis, and on the first connection failure
  permanently falls back to a memory cache (sticky fallback, so a dead
  Redis doesn't add a connection-timeout penalty to every call).

The cache stores strings (JSON payloads); serialization of
CanonicalTx lists lives in engine/indexer/cached_adapter.py.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Optional


class IndexerCache(ABC):
    """String key/value store with per-key TTL (seconds)."""

    @abstractmethod
    async def get(self, key: str) -> Optional[str]:
        """Return the value, or None on miss/expiry."""

    @abstractmethod
    async def set(self, key: str, value: str, ttl: int) -> None:
        """Store value, expiring after ttl seconds (ttl <= 0: no store)."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove a key; deleting a missing key is a no-op."""


class MemoryIndexerCache(IndexerCache):
    """Process-local LRU cache with TTL expiry."""

    def __init__(self, max_entries: int = 10000):
        self._max = max(1, max_entries)
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()

    async def get(self, key: str) -> Optional[str]:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if time.monotonic() >= expires_at:
            del self._data[key]
            return None
        self._data.move_to_end(key)  # LRU touch
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        if ttl <= 0:
            return
        self._data[key] = (value, time.monotonic() + ttl)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)  # evict least-recently-used

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:  # test seam
        return len(self._data)


class RedisIndexerCache(IndexerCache):
    """Durable cache on Redis. Client is created lazily on first use."""

    def __init__(self, redis_url: str, prefix: str = "vasp:idx:v1"):
        self._url = redis_url
        self._prefix = prefix
        self._client = None

    def _client_or_raise(self):
        if self._client is None:
            from redis.asyncio import from_url
            self._client = from_url(self._url, decode_responses=True)
        return self._client

    def _k(self, key: str) -> str:
        return f"{self._prefix}:{key}" if self._prefix else key

    async def get(self, key: str) -> Optional[str]:
        return await self._client_or_raise().get(self._k(key))

    async def set(self, key: str, value: str, ttl: int) -> None:
        if ttl <= 0:
            return
        await self._client_or_raise().setex(self._k(key), ttl, value)

    async def delete(self, key: str) -> None:
        await self._client_or_raise().delete(self._k(key))

    async def ping(self) -> bool:
        try:
            return bool(await self._client_or_raise().ping())
        except Exception:  # noqa: BLE001 — probe only
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class AutoIndexerCache(IndexerCache):
    """Redis with sticky fallback to memory.

    The first Redis failure (any exception on get/set/delete) flips the
    backend to memory permanently for this instance, so a dead Redis
    costs one failed call, not a timeout on every call.
    """

    def __init__(self, redis_cache: RedisIndexerCache,
                 memory_cache: MemoryIndexerCache):
        self._redis = redis_cache
        self._memory = memory_cache
        self._redis_dead = False

    @property
    def using_memory(self) -> bool:  # test/smoke seam
        return self._redis_dead

    async def _with_fallback(self, op: str, *args):
        if not self._redis_dead:
            try:
                return await getattr(self._redis, op)(*args)
            except Exception:  # noqa: BLE001 — cache must never fail
                self._redis_dead = True
        return await getattr(self._memory, op)(*args)

    async def get(self, key: str) -> Optional[str]:
        return await self._with_fallback("get", key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._with_fallback("set", key, value, ttl)

    async def delete(self, key: str) -> None:
        await self._with_fallback("delete", key)


def make_indexer_cache(backend: str, redis_url: str,
                       max_entries: int = 10000,
                       prefix: str = "vasp:idx:v1") -> IndexerCache:
    """Build a cache for backend: memory | redis | auto."""
    backend = (backend or "auto").lower()
    if backend == "memory":
        return MemoryIndexerCache(max_entries=max_entries)
    if backend == "redis":
        return RedisIndexerCache(redis_url, prefix=prefix)
    if backend == "auto":
        return AutoIndexerCache(
            RedisIndexerCache(redis_url, prefix=prefix),
            MemoryIndexerCache(max_entries=max_entries))
    raise ValueError(f"unknown indexer cache backend: {backend!r} "
                     "(want memory | redis | auto)")
