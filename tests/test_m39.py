"""M39 tests: GET /cases/{case_id}/graph/transactions.

Paginated recent transfers, newest first, for the workbench's
transaction-activity list. Each item carries the classifier's edge
stamps (kind/confidence/reason) so the UI can describe transactions
in words without re-inference. Kinds are normalized to the
workbench's display contract (sweep-candidate -> sweep, ...).
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, FlowParty,
)
from engine.graph.builder import TxGraph
from engine.graph.memory_store import MemoryGraphStore

pytestmark = pytest.mark.asyncio


def _tx(tx_hash, src, dst, day, value="100"):
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)],
        block_time=datetime(2026, 9, day, tzinfo=timezone.utc))


def _stamped_graph() -> TxGraph:
    g = TxGraph()
    g.add_tx(_tx("0xh1", "0xsub", "0xmid", 1))
    g.add_tx(_tx("0xh2", "0xmid", "0xterm", day=3, value="90"))
    g.add_tx(_tx("0xh3", "0xterm", "0xout", day=2, value="80"))
    stamps = {
        "0xh1": ("sweep-candidate", 0.9, "consolidation into one output"),
        "0xh2": ("bridge-lock", 0.75, "funds locked for bridging"),
        "0xh3": ("peel", 0.6, "change returned to sender cluster"),
    }
    for _s, _d, _key, attrs in g.g.edges(keys=True, data=True):
        kind, conf, reason = stamps[attrs.get("tx_hash")]
        attrs["hop_kind"] = kind
        attrs["hop_confidence"] = conf
        attrs["hop_reason"] = reason
    return g


def _api_app(store):
    from api.routers.graph import router as graph_router

    app = FastAPI()
    app.state.graph_store = store
    app.include_router(graph_router)
    return app


async def _client():
    store = MemoryGraphStore()
    await store.save_case_subgraph(
        "case-1", _stamped_graph(),
        {"subject": "0xsub", "terminal": "0xout"})
    return TestClient(_api_app(store))


async def test_transactions_newest_first_with_pagination():
    client = await _client()
    r = client.get("/cases/case-1/graph/transactions?limit=2&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    # Newest first: day 3, then day 2.
    assert [i["tx_hash"] for i in body["items"]] == ["0xh2", "0xh3"]
    r2 = client.get("/cases/case-1/graph/transactions?limit=2&offset=2")
    assert [i["tx_hash"] for i in r2.json()["items"]] == ["0xh1"]


async def test_transactions_carry_classifier_stamps_normalized():
    client = await _client()
    r = client.get("/cases/case-1/graph/transactions?limit=1&offset=0")
    item = r.json()["items"][0]
    assert item["src"] == "0xmid"
    assert item["dst"] == "0xterm"
    assert item["block_time"].startswith("2026-09-03")
    # Raw enum values normalized to the frontend's display keys.
    assert item["kind"] == "bridge"
    assert item["confidence"] == 0.75
    assert item["reason"] == "funds locked for bridging"


async def test_transactions_default_limit_and_404_without_graph():
    client = await _client()
    r = client.get("/cases/case-1/graph/transactions")
    body = r.json()
    assert body["limit"] == 4
    assert body["total"] == 3
    assert len(body["items"]) == 3

    r = client.get("/cases/missing/graph/transactions")
    assert r.status_code == 404
