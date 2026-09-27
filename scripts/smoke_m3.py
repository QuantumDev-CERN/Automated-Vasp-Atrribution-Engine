"""M3 live smoke: adapter -> graph -> hop classifier -> traversal.

Bounded on purpose: expands one seed address (5 txs), then up to 4
discovered neighbours (3 txs each), then traverses max 2 hops. Proves the
pipeline on real chain data without an unbounded crawl.

Run: uv run python scripts/smoke_m3.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import AdapterError
from engine.adapters.bitcoin import BitcoinAdapter
from engine.classifier import classify_graph
from engine.graph import TxGraph, expand_address
from engine.traversal import TraversalConfig, traverse


def _transient(message: str) -> bool:
    m = message.lower()
    return any(
        h in m
        for h in ("429", "502", "503", "504", "timeout", "timed out",
                  "connecterror", "connection reset", "temporarily unavailable")
    )


async def main() -> None:
    try:
        await _smoke()
    except AdapterError as e:
        if _transient(str(e)):
            print(f"[m3] SKIP — mempool.space throttled/unreachable: {e}")
            return
        raise
    print("\nSMOKE M3 OK — build -> classify -> traverse on live BTC data.")


async def _smoke() -> None:
    btc = BitcoinAdapter()
    graph = TxGraph()

    seed = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"  # genesis block coinbase address
    n0 = await expand_address(graph, btc, seed, limit=5)
    print(f"[m3] seed {seed[:12]}…: +{n0} txs")

    # one bounded expansion round for 2-hop traversal
    neighbours = [a for a in graph.addresses() if a != seed][:4]
    for nb in neighbours:
        n = await expand_address(graph, btc, nb, limit=3)
        print(f"[m3]   neighbour {nb[:12]}…: +{n} txs")

    stats = graph.stats()
    print(f"[m3] graph: {stats}")

    kinds: dict[str, int] = {}
    for c in classify_graph(graph).values():
        kinds[c.kind.value] = kinds.get(c.kind.value, 0) + 1
    print(f"[m3] hop kinds: {kinds}")

    r = traverse(graph, seed, TraversalConfig(max_hops=2, max_nodes=200))
    print(f"[m3] from seed: visited={len(r.visited)} terminals={len(r.terminals)}")

    # traverse from the most active neighbour to exercise multi-hop walking
    start = max(
        (a for a in graph.addresses() if a != seed),
        key=lambda a: len(graph.out_edges(a)),
        default=seed,
    )
    r = traverse(graph, start, TraversalConfig(max_hops=2, max_nodes=200))
    print(f"[m3] from {start[:12]}…: visited={len(r.visited)} "
          f"terminals={len(r.terminals)}")
    for v in r.visited[:8]:
        print(f"  hop{v.hop} {v.address[:16]:18} via={v.via_kind} "
              f"conf={v.via_confidence} side={v.side_branch}")
    for t in r.terminals[:5]:
        print(f"  terminal {t.address[:16]:18} reason={t.reason}")

    assert r.visited, "traversal visited nothing"
    assert kinds, "no edges classified"
    await btc.close()


if __name__ == "__main__":
    asyncio.run(main())
