"""M25 tests: durable indexer cache."""
from datetime import datetime, timezone

import pytest

from engine.adapters.base import (
    AdapterError, Asset, AssetKind, CanonicalTx, Chain, ChainAdapter,
    DexSwap, FlowParty,
)
from engine.indexer.cache import (
    AutoIndexerCache, MemoryIndexerCache, RedisIndexerCache,
    make_indexer_cache,
)
from engine.indexer.cached_adapter import (
    CachedChainAdapter, CacheStats, with_indexer_cache,
)

_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")


def _tx(tx_hash: str, addr: str) -> CanonicalTx:
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM,
        block_number=123, block_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
        inputs=[FlowParty(address="0xaaa", value="1000000000000000000")],
        outputs=[FlowParty(address=addr, value="1000000000000000000")],
        asset=_ETH, fee="21000000000000",
        raw={"hash": tx_hash},
        dex_swap=DexSwap(tx_hash=tx_hash, chain=Chain.ETHEREUM,
                         trader="0xaaa"))


class StubAdapter(ChainAdapter):
    def __init__(self, txs=(), fail=False):
        super().__init__()
        self.chain = Chain.ETHEREUM
        self._txs = list(txs)
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []

    async def get_transactions(self, address, limit=100):
        self.calls.append(("txlist", address, limit))
        if self.fail:
            raise AdapterError("indexer down")
        return [t for t in self._txs
                if any(p.address == address for p in t.outputs)]

    async def get_token_transfers(self, address, limit=100):
        self.calls.append(("tokentx", address, limit))
        return []

    async def get_code(self, address):
        self.calls.append(("code", address, 0))
        return "0x60806040"

    async def health_check(self):
        return True


class BoomCache(MemoryIndexerCache):
    async def get(self, key):
        raise RuntimeError("cache exploded")

    async def set(self, key, value, ttl):
        raise RuntimeError("cache exploded")


# ---------- memory backend ----------

@pytest.mark.asyncio
async def test_memory_set_get_miss():
    c = MemoryIndexerCache()
    assert await c.get("k") is None
    await c.set("k", "v", 60)
    assert await c.get("k") == "v"
    await c.delete("k")
    assert await c.get("k") is None


@pytest.mark.asyncio
async def test_memory_ttl_expiry():
    c = MemoryIndexerCache()
    await c.set("k", "v", 0)  # ttl <= 0: never stored
    assert await c.get("k") is None


@pytest.mark.asyncio
async def test_memory_lru_eviction():
    c = MemoryIndexerCache(max_entries=2)
    await c.set("a", "1", 60)
    await c.set("b", "2", 60)
    assert await c.get("a") == "1"  # touch a: b is now LRU
    await c.set("c", "3", 60)
    assert await c.get("b") is None  # evicted
    assert await c.get("a") == "1"
    assert await c.get("c") == "3"


# ---------- factory ----------

def test_make_indexer_cache_backends():
    assert isinstance(make_indexer_cache("memory", ""), MemoryIndexerCache)
    assert isinstance(make_indexer_cache("redis", "redis://x:6379/0"),
                      RedisIndexerCache)
    assert isinstance(make_indexer_cache("auto", "redis://x:6379/0"),
                      AutoIndexerCache)
    with pytest.raises(ValueError):
        make_indexer_cache("bogus", "")


@pytest.mark.asyncio
async def test_auto_falls_back_to_memory_on_redis_failure():
    class DeadRedis(RedisIndexerCache):
        async def get(self, key):
            raise ConnectionError("no redis here")

        async def set(self, key, value, ttl):
            raise ConnectionError("no redis here")

        async def delete(self, key):
            raise ConnectionError("no redis here")

    auto = AutoIndexerCache(DeadRedis("redis://x:6379/0"),
                            MemoryIndexerCache())
    assert not auto.using_memory
    await auto.set("k", "v", 60)
    assert auto.using_memory  # sticky fallback after first failure
    assert await auto.get("k") == "v"  # served from memory
    # second call does not touch redis again
    await auto.set("k2", "v2", 60)
    assert await auto.get("k2") == "v2"


# ---------- cached adapter ----------

@pytest.mark.asyncio
async def test_cached_adapter_hit_miss_stats():
    inner = StubAdapter([_tx("h1", "0xabc")])
    cached = CachedChainAdapter(inner, MemoryIndexerCache(), ttl_seconds=60)
    first = await cached.get_transactions("0xabc")
    second = await cached.get_transactions("0xabc")
    assert len(inner.calls) == 1  # second call served from cache
    assert first[0].tx_hash == second[0].tx_hash == "h1"
    assert cached.stats.hits == 1 and cached.stats.misses == 1
    assert cached.stats.hit_rate == 0.5


@pytest.mark.asyncio
async def test_cached_adapter_key_includes_chain_method_limit():
    inner = StubAdapter([_tx("h1", "0xabc")])
    cached = CachedChainAdapter(inner, MemoryIndexerCache(), ttl_seconds=60)
    await cached.get_transactions("0xabc", limit=100)
    await cached.get_transactions("0xabc", limit=50)  # different limit: miss
    await cached.get_token_transfers("0xabc")          # different method: miss
    assert len(inner.calls) == 3


@pytest.mark.asyncio
async def test_cached_adapter_caches_empty_results():
    inner = StubAdapter([])
    cached = CachedChainAdapter(inner, MemoryIndexerCache(), ttl_seconds=60)
    assert await cached.get_transactions("0xdead") == []
    assert await cached.get_transactions("0xdead") == []
    assert len(inner.calls) == 1  # the empty list was cached


@pytest.mark.asyncio
async def test_cached_adapter_never_caches_errors():
    inner = StubAdapter(fail=True)
    cached = CachedChainAdapter(inner, MemoryIndexerCache(), ttl_seconds=60)
    with pytest.raises(AdapterError):
        await cached.get_transactions("0xabc")
    with pytest.raises(AdapterError):
        await cached.get_transactions("0xabc")
    assert len(inner.calls) == 2  # retried, not served from cache


@pytest.mark.asyncio
async def test_cached_adapter_round_trips_canonical_tx():
    tx = _tx("h9", "0xabc")
    inner = StubAdapter([tx])
    cached = CachedChainAdapter(inner, MemoryIndexerCache(), ttl_seconds=60)
    first = await cached.get_transactions("0xabc")
    second = await cached.get_transactions("0xabc")  # from cache
    assert second[0].model_dump(mode="json") == tx.model_dump(mode="json")
    assert second[0].block_time == tx.block_time
    assert second[0].dex_swap is not None
    assert second[0].dex_swap.trader == "0xaaa"
    assert first[0] == second[0]


@pytest.mark.asyncio
async def test_cached_adapter_corrupt_entry_is_a_miss():
    inner = StubAdapter([_tx("h1", "0xabc")])
    cache = MemoryIndexerCache()
    cached = CachedChainAdapter(inner, cache, ttl_seconds=60)
    await cache.set("ethereum:txlist:0xabc:100", "{not json", 60)
    result = await cached.get_transactions("0xabc")
    assert result[0].tx_hash == "h1"
    assert len(inner.calls) == 1
    assert cached.stats.hits == 0 and cached.stats.misses == 1


@pytest.mark.asyncio
async def test_cached_adapter_survives_cache_failure():
    inner = StubAdapter([_tx("h1", "0xabc")])
    cached = CachedChainAdapter(inner, BoomCache(), ttl_seconds=60)
    result = await cached.get_transactions("0xabc")
    assert result[0].tx_hash == "h1"  # inner result, no exception


@pytest.mark.asyncio
async def test_cached_adapter_caches_code_and_receipt():
    inner = StubAdapter()
    cached = CachedChainAdapter(inner, MemoryIndexerCache(), ttl_seconds=60)
    assert await cached.get_code("0xc0de") == "0x60806040"
    assert await cached.get_code("0xc0de") == "0x60806040"
    assert len(inner.calls) == 1
    # health_check delegates, never cached
    assert await cached.health_check() is True
    # unknown attrs delegate to the inner adapter
    assert cached.api_key == inner.api_key


@pytest.mark.asyncio
async def test_with_indexer_cache_none_is_noop(monkeypatch):
    from api.core import config as config_mod
    monkeypatch.setattr(config_mod.settings, "indexer_cache_backend", "none")
    inner = StubAdapter()
    assert with_indexer_cache(inner) is inner
