"""CachedChainAdapter (M25): a caching decorator for any ChainAdapter.

Wraps get_transactions / get_token_transfers / get_transaction_receipt /
get_code. Cache keys are chain + method + address + limit; values are
pydantic-JSON CanonicalTx lists (or plain JSON for receipt/code).
A corrupt cached entry is treated as a miss, never as data. Cache
errors never fail the call — the inner adapter is the fallback.

Freshness trade-off (read this before tuning the TTL): cached entries
are served for up to ttl_seconds. An address's history can grow inside
that window, so a cached read may miss the newest transactions.
Default 1h; lower it for hot-wallet monitoring, raise it for cold-case
backfills. Empty results ARE cached (dead addresses are the most common
repeat query). Indexer errors are NEVER cached.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..adapters.base import CanonicalTx, ChainAdapter
from .cache import IndexerCache


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0


class CachedChainAdapter(ChainAdapter):
    def __init__(self, inner: ChainAdapter, cache: IndexerCache,
                 ttl_seconds: int = 3600,
                 stats: Optional[CacheStats] = None):
        super().__init__()
        self._inner = inner
        self.chain = inner.chain
        self._cache = cache
        self._ttl = ttl_seconds
        self.stats = stats if stats is not None else CacheStats()

    # -- plumbing ------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        # Anything not cached (get_balance, adapter-specific helpers,
        # _client, ...) delegates straight to the inner adapter.
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def _key(self, method: str, address: str, limit: int = 0) -> str:
        return (f"{self.chain.value}:{method}:"
                f"{address}:{limit}")

    async def _cached(self, key: str, loader):
        """Return the cached value for key, else loader() and store it.

        loader returns a JSON-serializable payload. Any cache failure or
        corrupt entry falls back to loader(); loader exceptions are
        never cached and propagate to the caller.
        """
        try:
            raw = await self._cache.get(key)
        except Exception:  # noqa: BLE001 — cache must never fail a trace
            raw = None
        if raw is not None:
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                payload = None  # corrupt entry: treat as a miss
            if payload is not None:
                self.stats.hits += 1
                return payload
        self.stats.misses += 1
        payload = await loader()  # raises propagate; never cached
        try:
            await self._cache.set(key, json.dumps(payload), self._ttl)
        except Exception:  # noqa: BLE001 — cache must never fail a trace
            pass
        return payload

    # -- cached ChainAdapter surface ------------------------------------
    async def get_transactions(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        key = self._key("txlist", address, limit)

        async def loader():
            return [t.model_dump(mode="json")
                    for t in await self._inner.get_transactions(
                        address, limit)]

        payload = await self._cached(key, loader)
        return [CanonicalTx.model_validate(d) for d in payload]

    async def get_token_transfers(
        self, address: str, limit: int = 100
    ) -> list[CanonicalTx]:
        key = self._key("tokentx", address, limit)

        async def loader():
            return [t.model_dump(mode="json")
                    for t in await self._inner.get_token_transfers(
                        address, limit)]

        payload = await self._cached(key, loader)
        return [CanonicalTx.model_validate(d) for d in payload]

    async def get_transaction_receipt(self, tx_hash: str) -> dict:
        key = self._key("receipt", tx_hash)
        return await self._cached(
            key, lambda: self._inner.get_transaction_receipt(tx_hash))

    async def get_code(self, address: str) -> str:
        key = self._key("code", address)
        return await self._cached(
            key, lambda: self._inner.get_code(address))

    async def health_check(self) -> bool:
        # Liveness probe: never served from cache.
        return await self._inner.health_check()


# -- shared wiring -------------------------------------------------------

_shared_cache: Optional[IndexerCache] = None


def get_shared_cache() -> IndexerCache:
    """Process-wide cache built from settings (lazy, singleton)."""
    global _shared_cache
    if _shared_cache is None:
        from api.core.config import settings
        from .cache import make_indexer_cache
        _shared_cache = make_indexer_cache(
            settings.indexer_cache_backend,
            settings.redis_url,
            max_entries=settings.indexer_cache_max_entries,
            prefix=settings.indexer_cache_prefix)
    return _shared_cache


def with_indexer_cache(adapter: ChainAdapter) -> ChainAdapter:
    """Wrap an adapter in the shared cache. Backend 'none' = no-op."""
    from api.core.config import settings
    if settings.indexer_cache_backend.lower() == "none":
        return adapter
    return CachedChainAdapter(
        adapter, get_shared_cache(),
        ttl_seconds=settings.indexer_cache_ttl)
