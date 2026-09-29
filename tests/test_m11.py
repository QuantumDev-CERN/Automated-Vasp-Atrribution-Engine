"""M11 tests: topology JSON, trace path, graph API endpoints."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, FlowParty,
)
from engine.graph.builder import TxGraph
from engine.graph.memory_store import MemoryGraphStore
from engine.graph.topology import graph_topology, trace_path


def _tx(tx_hash, src, dst, value="100"):
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)],
        block_time=datetime(2026, 9, 1, tzinfo=timezone.utc))


def _graph() -> TxGraph:
    g = TxGraph()
    g.add_tx(_tx("0xh1", "0xsub", "0xmid"))
    g.add_tx(_tx("0xh2", "0xmid", "0xterm", value="90"))
    g.add_tx(_tx("0xh3", "0xmid", "0xother"))
    g.label("0xterm", "vasp:CoinDCX")
    return g


async def test_topology_nodes_edges_and_tags():
    store = MemoryGraphStore()
    g = _graph()
    meta = {"subject": "0xsub", "terminal": "0xterm", "chain": "ethereum"}
    await store.save_case_subgraph("case-1", g, meta)
    await store.tag_address("0xterm", "ethereum", "vasp:coindcx",
                            source="unit-test")

    loaded = await store.load_case_subgraph("case-1")
    topo = await graph_topology(loaded, store)
    assert topo["total_addresses"] == 4
    assert topo["total_transfers"] == 3
    assert topo["truncated"] is False

    by_id = {n["id"]: n for n in topo["nodes"]}
    assert by_id["0xterm"]["labels"] == ["vasp:CoinDCX"]
    assert by_id["0xterm"]["tags"] == ["vasp:coindcx"]
    assert by_id["0xsub"]["chains"] == ["ethereum"]

    edges = [e for e in topo["edges"] if e["tx_hash"] == "0xh2"]
    assert edges[0]["src"] == "0xmid" and edges[0]["dst"] == "0xterm"
    assert edges[0]["asset_symbol"] == "ETH"


async def test_topology_truncation_keeps_best_connected():
    g = TxGraph()
    hub = "0xhub"
    for i in range(10):
        g.add_tx(_tx(f"0xh{i}", hub, f"0xleaf{i}"))
    g.add_tx(_tx("0xh9x", "0xhub", "0xhub2"))
    topo = await graph_topology(g, max_nodes=3, max_edges=100)
    assert topo["truncated"] is True
    assert len(topo["nodes"]) == 3
    assert topo["nodes"][0]["id"] == "0xhub"  # highest degree first


async def test_trace_path_hops():
    path = trace_path(_graph(), "0xsub", "0xterm")
    assert path is not None
    assert [h["address"] for h in path] == ["0xsub", "0xmid", "0xterm"]
    assert path[0]["hop"] == 0 and "via_tx" not in path[0]
    assert path[1]["via_tx"] == "0xh1"
    assert path[2]["via_tx"] == "0xh2"
    assert path[2]["value"] == "90"

    assert trace_path(_graph(), "0xsub", "0xnowhere") is None


def _api_app(store: MemoryGraphStore):
    from api.routers.graph import router as graph_router

    app = FastAPI()
    app.state.graph_store = store
    app.include_router(graph_router)
    return app


async def test_api_topology_and_path():
    store = MemoryGraphStore()
    await store.save_case_subgraph("case-1", _graph(),
                                   {"subject": "0xsub", "terminal": "0xterm"})
    client = TestClient(_api_app(store))

    r = client.get("/cases/case-1/graph")
    assert r.status_code == 200
    body = r.json()
    assert body["total_addresses"] == 4
    assert len(body["edges"]) == 3

    r = client.get("/cases/case-1/graph?max_nodes=2")
    assert r.json()["truncated"] is True

    r = client.get("/cases/case-1/graph/path")
    assert r.status_code == 200
    assert [h["address"] for h in r.json()["hops"]] == ["0xsub", "0xmid", "0xterm"]


async def test_api_404s():
    store = MemoryGraphStore()
    await store.save_case_subgraph("no-meta", _graph())  # no subject/terminal
    client = TestClient(_api_app(store))
    assert client.get("/cases/missing/graph").status_code == 404
    assert client.get("/cases/missing/graph/path").status_code == 404
    assert client.get("/cases/no-meta/graph/path").status_code == 404


async def test_case_meta_round_trip_memory():
    store = MemoryGraphStore()
    await store.save_case_subgraph("c", _graph(),
                                   {"subject": "0xsub", "terminal": "0xterm"})
    meta = await store.case_meta("c")
    assert meta["subject"] == "0xsub"
    assert await store.case_meta("absent") is None


async def test_topology_denominates_base_units():
    """Satoshis/wei must not be served raw next to a coin symbol."""
    from engine.graph.topology import _denominate
    assert _denominate("16397529", 8) == "0.16397529"
    assert _denominate("1800000000000000000", 18) == "1.8"
    assert _denominate("100", None) == "100"  # unknown decimals: unchanged
    assert _denominate("not-a-number", 8) == "not-a-number"
