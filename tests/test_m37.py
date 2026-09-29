"""M37 tests: stats days param (lifetime option) + mixer pool info on topology."""
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, FlowParty,
)
from engine.graph.builder import TxGraph
from engine.graph.memory_store import MemoryGraphStore
from engine.graph.topology import graph_topology

NOW = datetime.now(timezone.utc)

# Real Tornado Cash 1 ETH pool (mainnet) — same registry the backend uses.
POOL_1ETH = "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"
DEPOSITOR = "0xA160cdAB225685dA1d56aa342Ad8841c3b53f292"


def _tx(tx_hash, src, dst, days_ago=0, value="1000000000000000000"):
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)],
        block_time=NOW - timedelta(days=days_ago))


def _graph() -> TxGraph:
    g = TxGraph()
    g.add_tx(_tx("0xh1", "0xsub", "0xmid", days_ago=5))
    g.add_tx(_tx("0xh2", "0xmid", DEPOSITOR, days_ago=40))
    g.add_tx(_tx("0xh3", DEPOSITOR, POOL_1ETH, days_ago=40))
    g.label(DEPOSITOR, "mixer-depositor")
    return g


def _api_app(store: MemoryGraphStore):
    from api.routers.graph import router as graph_router
    app = FastAPI()
    app.state.graph_store = store
    app.include_router(graph_router)
    return app


async def _seeded_client() -> TestClient:
    store = MemoryGraphStore()
    await store.save_case_subgraph(
        "case-m37", _graph(),
        {"subject": "0xsub", "terminal": DEPOSITOR, "chain": "ethereum"})
    return TestClient(_api_app(store))


async def test_stats_days_zero_is_lifetime():
    client = await _seeded_client()
    r = client.get("/cases/case-m37/graph/stats?days=0")
    assert r.status_code == 200
    body = r.json()
    assert body["activity_days"] == 0
    dates = {d["date"] for d in body["daily_activity"]}
    # both the 5-day-old and the 40-day-old activity are present
    assert len(dates) == 2


async def test_stats_default_30d_excludes_old_activity():
    client = await _seeded_client()
    r = client.get("/cases/case-m37/graph/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["activity_days"] == 30
    dates = {d["date"] for d in body["daily_activity"]}
    assert len(dates) == 1  # only the 5-day-old tx; the 40-day-old is out


async def test_stats_days_validation():
    client = await _seeded_client()
    assert client.get("/cases/case-m37/graph/stats?days=-1").status_code == 422
    assert client.get("/cases/case-m37/graph/stats?days=3651").status_code == 422
    assert client.get("/cases/case-m37/graph/stats?days=3650").status_code == 200


async def test_topology_mixer_depositor_gets_pool():
    store = MemoryGraphStore()
    await store.save_case_subgraph("case-m37", _graph(), {"subject": "0xsub"})
    loaded = await store.load_case_subgraph("case-m37")
    topo = await graph_topology(loaded, store)
    by_id = {n["id"]: n for n in topo["nodes"]}
    pool = by_id[DEPOSITOR].get("pool")
    assert pool is not None
    assert pool["name"] == "Tornado Cash"
    assert pool["denomination"] == "1 ETH"
    # non-depositor nodes get no pool key
    assert "pool" not in by_id["0xsub"]
