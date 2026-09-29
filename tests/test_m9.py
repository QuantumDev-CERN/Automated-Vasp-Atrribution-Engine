"""M9 tests: cross-case knowledge graph + graph API endpoints.

All offline: MemoryGraphStore. Neo4j runs the same queries via the
shared GraphStore ABC (covered by scripts/smoke_m8_integration.py).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, FlowParty,
)
from engine.graph import MemoryGraphStore, TxGraph
from engine.intel import (
    find_case_links,
    shared_infrastructure,
    syndicate_summary,
)


def _tx(tx_hash: str, src: str, dst: str) -> CanonicalTx:
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value="100")],
        outputs=[FlowParty(address=dst, value="100")])


async def _two_linked_cases() -> MemoryGraphStore:
    """case-A: aaa->bbb->ccc ; case-B: ccc->ddd->eee (share 0xccc)."""
    store = MemoryGraphStore()
    g1 = TxGraph()
    g1.add_tx(_tx("0xh1", "0xaaa", "0xbbb"))
    g1.add_tx(_tx("0xh2", "0xbbb", "0xccc"))
    g2 = TxGraph()
    g2.add_tx(_tx("0xh3", "0xccc", "0xddd"))
    g2.add_tx(_tx("0xh4", "0xddd", "0xeee"))
    await store.save_case_subgraph("case-A", g1)
    await store.save_case_subgraph("case-B", g2)
    await store.tag_address("0xccc", "ethereum", "mixer-deposit",
                            source="traversal:case-A", case_id="case-A")
    return store


# ---------- find_case_links ----------

async def test_links_found_through_shared_address():
    store = await _two_linked_cases()
    links = await find_case_links("case-A", store)
    assert links.linked_case_ids == ["case-B"]
    lc = links.links[0]
    assert lc.overlap == 1
    assert lc.shared_addresses == ["0xccc"]
    assert lc.shared_tags["0xccc"] == ["mixer-deposit"]
    await store.close()


async def test_links_are_symmetric_and_ranked():
    store = await _two_linked_cases()
    g3 = TxGraph()
    g3.add_tx(_tx("0xh5", "0xbbb", "0xfff"))
    g3.add_tx(_tx("0xh6", "0xccc", "0xggg"))
    await store.save_case_subgraph("case-C", g3)
    links = await find_case_links("case-A", store)
    # case-C shares bbb AND ccc -> ranked first
    assert links.linked_case_ids == ["case-C", "case-B"]
    assert links.links[0].overlap == 2
    await store.close()


async def test_min_overlap_filters_weak_links():
    store = await _two_linked_cases()
    links = await find_case_links("case-A", store, min_overlap=2)
    assert links.links == []
    await store.close()


async def test_no_links_for_isolated_or_unknown_case():
    store = await _two_linked_cases()
    g = TxGraph()
    g.add_tx(_tx("0xh9", "0xzzz", "0xyyy"))
    await store.save_case_subgraph("case-Z", g)
    assert (await find_case_links("case-Z", store)).links == []
    assert (await find_case_links("nope", store)).links == []
    await store.close()


# ---------- shared_infrastructure ----------

async def test_infrastructure_pivot_groups_cases():
    store = await _two_linked_cases()
    pivot = await shared_infrastructure("mixer-deposit", store)
    assert pivot == {"0xccc": ["case-A", "case-B"]}
    assert await shared_infrastructure("nope", store) == {}
    await store.close()


# ---------- syndicate_summary ----------

async def test_summary_mentions_link_and_tag():
    store = await _two_linked_cases()
    text = syndicate_summary(await find_case_links("case-A", store))
    assert "case-B" in text and "mixer-deposit" in text
    alone = syndicate_summary(await find_case_links("case-Z", store))
    assert "no cross-case links" in alone
    await store.close()


# ---------- API ----------

def _api_app(store: MemoryGraphStore) -> FastAPI:
    from api.routers.graph import router as graph_router

    app = FastAPI()
    app.state.graph_store = store
    app.include_router(graph_router)
    return app


async def test_api_links_and_stats_and_infrastructure():
    store = await _two_linked_cases()
    client = TestClient(_api_app(store))

    r = client.get("/cases/case-A/graph/stats")
    assert r.status_code == 200
    assert r.json()["addresses"] == 3

    r = client.get("/cases/case-A/links")
    assert r.status_code == 200
    body = r.json()
    assert body["links"][0]["case_id"] == "case-B"
    assert "case-B" in body["summary"]

    r = client.get("/intel/infrastructure/mixer-deposit")
    assert r.status_code == 200
    assert r.json()["address_count"] == 1

    assert client.get("/cases/nope/graph/stats").status_code == 404
    await store.close()


# ---------- M41: batched cross-case primitives (N+1 -> 1 round trip) ----------

async def test_cases_for_addresses_matches_singles():
    store = await _two_linked_cases()
    aids = ["ethereum:0xaaa", "ethereum:0xccc", "ethereum:0xzzz"]
    batch = await store.cases_for_addresses(aids)
    assert batch == {
        "ethereum:0xaaa": ["case-A"],
        "ethereum:0xccc": ["case-A", "case-B"],
        "ethereum:0xzzz": [],
    }
    for aid in aids:
        chain, _, addr = aid.partition(":")
        assert batch[aid] == await store.cases_for_address(addr, chain)
    await store.close()


async def test_tags_for_addresses_matches_singles():
    store = await _two_linked_cases()
    aids = ["ethereum:0xccc", "ethereum:0xaaa"]
    batch = await store.tags_for_addresses(aids)
    assert [t["tag"] for t in batch["ethereum:0xccc"]] == ["mixer-deposit"]
    assert batch["ethereum:0xaaa"] == []
    for aid in aids:
        chain, _, addr = aid.partition(":")
        assert batch[aid] == await store.address_tags(addr, chain)
    await store.close()


async def test_batch_primitives_empty_input():
    store = MemoryGraphStore()
    assert await store.cases_for_addresses([]) == {}
    assert await store.tags_for_addresses([]) == {}
    await store.close()


async def test_find_case_links_dedupes_multi_chain_address():
    """Same address on two chains still links once (M41 parity)."""
    store = MemoryGraphStore()
    g1 = TxGraph()
    g1.add_tx(_tx("0xh1", "0xaaa", "0xccc"))
    g1.g.nodes["0xccc"]["chains"] = {"ethereum", "bitcoin"}
    await store.save_case_subgraph("case-A", g1)
    g2 = TxGraph()
    g2.add_tx(_tx("0xh2", "0xccc", "0xddd"))
    await store.save_case_subgraph("case-B", g2)
    links = await find_case_links("case-A", store)
    assert links.linked_case_ids == ["case-B"]
    assert links.links[0].shared_addresses == ["0xccc"]
    await store.close()
