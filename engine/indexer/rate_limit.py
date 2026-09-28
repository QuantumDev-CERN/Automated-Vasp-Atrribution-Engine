"""Shared per-chain async rate limiter (M31).

Etherscan's free tier enforces 3 calls/second; bursting past it used to
produce silent empty traces (M30 made those loud, with retry/backoff).
The correct fix is to never exceed the limit in the first place: every
indexer call funnels through one shared AsyncRateLimiter per chain, so
concurrent traces, the API, and the workers throttle cooperatively
instead of tripping the cap and backing off.

Placement: with_rate_limit() wraps the raw adapter INSIDE the M25 cache
(cache -> limiter -> adapter), so cache hits never consume throttle
budget — only real network calls wait.

Conservative by design: the default 2.5 calls/sec keeps a full margin
under Etherscan's 3/sec. Tune via INDEXER_RATE_LIMIT_PER_SEC.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..adapters.base import ChainAdapter


class AsyncRateLimiter:
    """Minimum-interval spacing between granted acquisitions.

    calls_per_second=2.5 -> at least 0.4s between acquisitions, so any
    rolling 1-second window stays at or under 2-3 calls. Strictly
    conservative: staying under the limit beats squeezing it.
    """

    def __init__(self, calls_per_second: float):
        if calls_per_second <= 0:
            raise ValueError("calls_per_second must be positive")
        self._interval = 1.0 / calls_per_second
        self._lock = asyncio.Lock()
        self._last: float = 0.0

    @property
    def calls_per_second(self) -> float:
        return 1.0 / self._interval

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


_LIMITERS: dict[str, AsyncRateLimiter] = {}


def get_rate_limiter(chain_key: str,
                     calls_per_second: float) -> AsyncRateLimiter:
    """Process-wide limiter for a chain key (e.g. "ethereum").

    First call wins on the rate; every adapter for the chain shares the
    one instance, so concurrent traces throttle cooperatively.
    """
    limiter = _LIMITERS.get(chain_key)
    if limiter is None:
        limiter = AsyncRateLimiter(calls_per_second)
        _LIMITERS[chain_key] = limiter
    return limiter


def reset_rate_limiters() -> None:
    """Drop all shared limiters (tests only)."""
    _LIMITERS.clear()


class RateLimitedChainAdapter(ChainAdapter):
    """Throttling decorator: every indexer call waits its turn.

    Mirrors CachedChainAdapter's delegation pattern: the four indexer
    reads are throttled, everything else (health_check, get_balance,
    adapter-specific helpers) passes straight through.
    """

    def __init__(self, inner: ChainAdapter, limiter: AsyncRateLimiter):
        super().__init__()
        self._inner = inner
        self.chain = inner.chain
        self._limiter = limiter

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    async def _throttled(self, method: str, *args: Any, **kwargs: Any) -> Any:
        await self._limiter.acquire()
        return await getattr(self._inner, method)(*args, **kwargs)

    async def get_transactions(self, address: str, limit: int = 100):
        return await self._throttled(
            "get_transactions", address, limit=limit)

    async def get_token_transfers(self, address: str, limit: int = 100):
        return await self._throttled(
            "get_token_transfers", address, limit=limit)

    async def get_transaction_receipt(self, tx_hash: str):
        return await self._throttled("get_transaction_receipt", tx_hash)

    async def get_code(self, address: str):
        return await self._throttled("get_code", address)


def with_rate_limit(adapter: ChainAdapter) -> ChainAdapter:
    """Wrap an adapter in its chain's shared rate limiter."""
    from api.core.config import settings
    limiter = get_rate_limiter(
        adapter.chain.value, float(settings.indexer_rate_limit_per_sec))
    return RateLimitedChainAdapter(adapter, limiter)
