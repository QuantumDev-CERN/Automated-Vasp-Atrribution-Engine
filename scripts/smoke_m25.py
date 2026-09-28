"""M25 live smoke: durable indexer cache.

Two sections:

1. Memory backend (offline): CachedChainAdapter over a stub inner
   adapter — miss on first call, hit on second, stats reflect it.

2. Redis backend (live): probes settings.redis_url. If Redis is
   unreachable the section SKIPs. Otherwise: set/get round-trip, TTL
   expiry, cross-instance durability (write with one RedisIndexerCache,
   read with another — the point of "durable"), and a CachedChainAdapter
   end-to-end against Redis.

Run: uv run python scripts/smoke_m25.py
"""
import asyncio
import socket
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.indexer.cache import RedisIndexerCache, make_indexer_cache
from engine.indexer.cached_adapter import CachedChainAdapter

_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")


class StubAdapter(ChainAdapter):
    def __init__(self):
        super().__init__()
        self.chain = Chain.ETHEREUM
        self.calls = 0

    async def get_transactions(self, address, limit=100):
        self.calls += 1
        return [CanonicalTx(
            tx_hash="smoke1", chain=Chain.ETHEREUM, asset=_ETH,
            inputs=[FlowParty(address="0xaaa", value="1")],
            outputs=[FlowParty(address=address, value="1")])]


class SectionSkip(Exception):
    pass


def _redis_reachable(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url)
        host, port = p.hostname or "localhost", p.port or 6379
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False


async def section_memory() -> None:
    inner = StubAdapter()
    cached = CachedChainAdapter(inner, make_indexer_cache("memory", ""),
                                ttl_seconds=60)
    first = await cached.get_transactions("0xsmoke")
    second = await cached.get_transactions("0xsmoke")
    assert inner.calls == 1, f"expected 1 inner call, got {inner.calls}"
    assert first[0].tx_hash == second[0].tx_hash == "smoke1"
    assert cached.stats.hits == 1 and cached.stats.misses == 1
    print(f"[memory] hit_rate={cached.stats.hit_rate:.0%} "
          f"(1 miss, 1 hit); round-trip OK")


async def section_redis() -> None:
    from api.core.config import settings
    url = settings.redis_url
    if not _redis_reachable(url):
        raise SectionSkip(f"m25: no redis at {url}")

    w = RedisIndexerCache(url, prefix="vasp:idx:smoke")
    r = RedisIndexerCache(url, prefix="vasp:idx:smoke")
    probe_key, probe_val = "m25-probe", "durable-value"

    await w.delete(probe_key)
    assert await r.get(probe_key) is None
    await w.set(probe_key, probe_val, 60)
    assert await r.get(probe_key) == probe_val  # cross-instance: durable
    print(f"[redis] cross-instance set/get OK at {url}")

    await w.set("m25-ttl", "x", 1)
    await asyncio.sleep(1.2)
    assert await r.get("m25-ttl") is None
    print("[redis] TTL expiry OK")

    inner = StubAdapter()
    cached = CachedChainAdapter(inner, w, ttl_seconds=60)
    await cached.get_transactions("0xsmoke")
    await cached.get_transactions("0xsmoke")
    assert inner.calls == 1, f"expected 1 inner call, got {inner.calls}"
    assert cached.stats.hit_rate == 0.5
    print("[redis] CachedChainAdapter end-to-end OK "
          f"(hit_rate={cached.stats.hit_rate:.0%})")

    await w.delete(probe_key)
    await w.delete("m25-ttl")
    await w.aclose()
    await r.aclose()


async def amain() -> int:
    ok = True
    try:
        await section_memory()
        print("PASS: memory-backed cache")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}")
        ok = False
    try:
        await section_redis()
        print("PASS: redis-backed durable cache")
    except SectionSkip as s:
        print(f"SKIP: {s}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}")
        ok = False
    return 0 if ok else 1


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
