"""M8 integration smoke: real Neo4j (docker-compose service).

Gated: runs only when M8_INTEGRATION=1. If the bolt port is unreachable
it prints SKIP and exits 0 — the offline path is covered by
tests/test_m8.py (MemoryGraphStore).

Covers:
  1. Neo4jGraphStore round-trip: save a small TxGraph, load it back
     losslessly, stats match.
  2. Tag provenance: tag_address -> address_tags.
  3. Cross-case primitive: two cases sharing an address ->
     cases_for_address returns both.

Run:  M8_INTEGRATION=1 uv run python scripts/smoke_m8_integration.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKIP = ("SKIP: neo4j unavailable "
        "(set M8_INTEGRATION=1 with docker compose up neo4j)")


async def main() -> int:
    if os.environ.get("M8_INTEGRATION") != "1":
        print(SKIP + " [M8_INTEGRATION!=1]")
        return 0

    from api.core.config import settings
    from engine.adapters.base import (
        Asset, AssetKind, CanonicalTx, Chain, FlowParty,
    )
    from engine.graph import TxGraph
    from engine.graph.neo4j_store import Neo4jGraphStore

    try:
        store = Neo4jGraphStore(settings.neo4j_uri, settings.neo4j_user,
                                settings.neo4j_password)
        store.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"{SKIP} [{exc}]")
        return 0
    print(f"[neo4j] connected: {settings.neo4j_uri}")

    def tx(h, src, dst):
        asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM,
                      symbol="ETH")
        return CanonicalTx(
            tx_hash=h, chain=Chain.ETHEREUM, asset=asset,
            inputs=[FlowParty(address=src, value="100")],
            outputs=[FlowParty(address=dst, value="100")])

    g1 = TxGraph()
    g1.add_tx(tx("0xih1", "0xaaa", "0xbbb"))
    g1.add_tx(tx("0xih2", "0xbbb", "0xccc"))
    g1.label("0xccc", "sweep-consolidation")
    g2 = TxGraph()
    g2.add_tx(tx("0xih3", "0xccc", "0xddd"))

    stats = await store.save_case_subgraph("m8-int-1", g1)
    assert stats == {"addresses": 3, "transfers": 2, "transactions": 2}, stats
    assert await store.case_stats("m8-int-1") == stats
    loaded = await store.load_case_subgraph("m8-int-1")
    assert loaded is not None and loaded.stats() == stats
    assert loaded.labels("0xccc") == {"sweep-consolidation"}
    print("[neo4j] save/load round-trip OK")

    await store.tag_address("0xccc", "ethereum", "sweep-consolidation",
                            source="traversal:m8-int-1", case_id="m8-int-1")
    tags = await store.address_tags("0xccc", "ethereum")
    assert any(t["tag"] == "sweep-consolidation"
               and t["source"] == "traversal:m8-int-1" for t in tags), tags
    print("[neo4j] tag provenance OK")

    await store.save_case_subgraph("m8-int-2", g2)
    cases = await store.cases_for_address("0xccc", "ethereum")
    assert "m8-int-1" in cases and "m8-int-2" in cases, cases
    tagged = await store.addresses_with_tag("sweep-consolidation")
    assert {"address": "0xccc", "chain": "ethereum"} in tagged
    print("[neo4j] cross-case address index OK")

    # cleanup: don't pollute a shared dev database
    with store._driver.session() as s:
        s.run("MATCH (c:Case) WHERE c.id STARTS WITH 'm8-int-' "
              "DETACH DELETE c")
        s.run("MATCH (a:Address) WHERE NOT (a)<-[:INCLUDES]-(:Case) "
              "AND NOT (a)-[:TAGGED]->(:Tag) DETACH DELETE a")
    await store.close()
    print("M8 integration smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
