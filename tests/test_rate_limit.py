"""M31 tests: shared per-chain async rate limiter.

The limiter is the structural fix for indexer throttling — instead of
tripping Etherscan's 3/sec cap and backing off, every indexer call
waits its turn cooperatively through one shared limiter per chain.
"""
import time

import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.indexer.rate_limit import (
    AsyncRateLimiter,
    RateLimitedChainAdapter,
    get_rate_limiter,
    reset_rate_limiters,
    with_rate_limit,
)


@pytest.fixture(autouse=True)
def _clean_limiters():
    reset_rate_limiters()
    yield
    reset_rate_limiters()


def _tx(tx_hash: str, src: str, dst: str) -> CanonicalTx:
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value="100")],
        outputs=[FlowParty(address=dst, value="100")])


class CountingAdapter(ChainAdapter):
    chain = Chain.ETHEREUM

    def __init__(self, txs):
        super().__init__()
        self._txs = txs
        self.calls = 0

    async def get_transactions(self, address: str, limit: int = 100):
        self.calls += 1
        return [t for t in self._txs
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address: str, limit: int = 100):
        self.calls += 1
        return []

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_limiter_spaces_acquisitions():
    limiter = AsyncRateLimiter(calls_per_second=5)  # 0.2s spacing
    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.3  # 2 intervals x 0.2s, tolerant bound


def test_limiter_rejects_nonpositive_rate():
    with pytest.raises(ValueError):
        AsyncRateLimiter(calls_per_second=0)


def test_get_rate_limiter_shared_per_chain():
    a = get_rate_limiter("ethereum", 10)
    b = get_rate_limiter("ethereum", 10)
    c = get_rate_limiter("bitcoin", 10)
    assert a is b
    assert a is not c


@pytest.mark.asyncio
async def test_with_rate_limit_throttles_and_delegates(monkeypatch):
    from api.core.config import settings
    monkeypatch.setattr(settings, "indexer_rate_limit_per_sec", 2.0)
    inner = CountingAdapter([_tx("0xt1", "0xsubject", "0xmid")])
    adapter = with_rate_limit(inner)
    assert isinstance(adapter, RateLimitedChainAdapter)
    assert adapter.chain == Chain.ETHEREUM

    start = time.monotonic()
    txs = await adapter.get_transactions("0xsubject")
    await adapter.get_transactions("0xsubject")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.4  # 2/sec -> 0.5s spacing, tolerant bound
    assert inner.calls == 2  # limiter never swallows the call
    assert [t.tx_hash for t in txs] == ["0xt1"]
    # non-throttled surface delegates straight through
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_make_adapter_wraps_limiter_inside_cache(monkeypatch):
    """make_adapter: cache -> limiter -> raw adapter, so cache hits
    never consume throttle budget."""
    from api.core.config import settings
    from engine.indexer.cached_adapter import CachedChainAdapter
    from engine.jobs.pipeline import make_adapter

    monkeypatch.setattr(settings, "indexer_cache_backend", "memory")
    adapter = make_adapter("ethereum")
    assert isinstance(adapter, CachedChainAdapter)
    assert isinstance(adapter._inner, RateLimitedChainAdapter)
    assert adapter._inner.chain == Chain.ETHEREUM
