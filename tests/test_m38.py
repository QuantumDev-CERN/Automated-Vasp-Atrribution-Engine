"""M38 tests: workbench reads classifier data from the durable edge stamps.

The classifier stamps hop_kind / hop_confidence / hop_reason onto every
edge at trace time, for every case ever traced. graph/stats counts the
whole-graph edge stamps for the breakdown (not just the few path hops in
meta.hops); graph/path falls back to the edge stamps when meta.hops has
no record for an address. Kinds are normalized to the workbench's
display contract (sweep-candidate -> sweep, ...).
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


def _tx(tx_hash, src, dst, value="100"):
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)],
        block_time=datetime(2026, 9, 1, tzinfo=timezone.utc))


def _stamped_graph() -> TxGraph:
    """Graph with raw classifier stamps on the edges (as classify_graph
    writes them at trace time)."""
    g = TxGraph()
    g.add_tx(_tx("0xh1", "0xsub", "0xmid"))
    g.add_tx(_tx("0xh2", "0xmid", "0xterm", value="90"))
    stamps = {
        "0xh1": ("sweep-candidate", 0.9, "consolidation into one output"),
        "0xh2": ("bridge-lock", 0.75, "funds locked for bridging"),
    }
    for _s, _d, key, attrs in g.g.edges(keys=True, data=True):
        tx_hash = attrs.get("tx_hash")
        kind, conf, reason = stamps[tx_hash]
        attrs["hop_kind"] = kind
        attrs["hop_confidence"] = conf
        attrs["hop_reason"] = reason
    return g


class _NoGraphStore(MemoryGraphStore):
    """Simulates a store whose graph failed to load (meta only)."""

    async def load_case_subgraph(self, case_id: str):  # noqa: D102
        return None


def _api_app(store):
    from api.routers.graph import router as graph_router

    app = FastAPI()
    app.state.graph_store = store
    app.include_router(graph_router)
    return app


async def test_stats_breakdown_counts_all_edge_stamps():
    store = MemoryGraphStore()
    # Pre-M26 meta: subject/terminal only, no hops array.
    await store.save_case_subgraph(
        "old-case", _stamped_graph(),
        {"subject": "0xsub", "terminal": "0xterm"})
    client = TestClient(_api_app(store))

    r = client.get("/cases/old-case/graph/stats")
    assert r.status_code == 200
    body = r.json()
    # Raw enum values normalized to the frontend's display keys.
    assert body["classifier_breakdown"] == {"sweep": 1, "bridge": 1}
    # Activity still derives from edge block_times.
    assert len(body["daily_activity"]) == 1
    assert body["daily_activity"][0]["date"] == "2026-09-01"


async def test_stats_breakdown_falls_back_to_meta_hops_without_graph():
    store = _NoGraphStore()
    meta = {
        "subject": "0xsub", "terminal": "0xterm",
        "hops": [{"address": "0xmid", "kind": "sweep-candidate",
                  "confidence": 0.9}],
    }
    # Bypass save_case_subgraph (it needs a graph); persist meta only.
    store._cases["no-graph"] = {
        "graph": None, "stats": {"addresses": 0, "transfers": 0,
                                 "transactions": 0},
        "meta": dict(meta), "saved_at": "",
    }
    client = TestClient(_api_app(store))

    r = client.get("/cases/no-graph/graph/stats")
    assert r.status_code == 200
    assert r.json()["classifier_breakdown"] == {"sweep": 1}


async def test_path_confidence_falls_back_to_edge_stamps():
    store = MemoryGraphStore()
    await store.save_case_subgraph(
        "old-case", _stamped_graph(),
        {"subject": "0xsub", "terminal": "0xterm"})
    client = TestClient(_api_app(store))

    r = client.get("/cases/old-case/graph/path")
    assert r.status_code == 200
    hops = {h["address"]: h for h in r.json()["hops"]}
    assert hops["0xmid"]["kind"] == "sweep"
    assert hops["0xmid"]["confidence"] == 0.9
    assert hops["0xmid"]["reason"] == "consolidation into one output"
    assert hops["0xterm"]["kind"] == "bridge"
    assert hops["0xterm"]["confidence"] == 0.75


async def test_path_prefers_meta_hops_when_present():
    store = MemoryGraphStore()
    meta = {
        "subject": "0xsub", "terminal": "0xterm",
        "hops": [
            {"address": "0xmid", "kind": "direct",
             "confidence": 0.5, "note": "pipeline record"},
        ],
    }
    await store.save_case_subgraph("new-case", _stamped_graph(), meta)
    client = TestClient(_api_app(store))

    r = client.get("/cases/new-case/graph/path")
    hops = {h["address"]: h for h in r.json()["hops"]}
    assert hops["0xmid"]["kind"] == "direct"
    assert hops["0xmid"]["confidence"] == 0.5
    assert hops["0xmid"]["reason"] == "pipeline record"
    # Address with no meta.hops record still gets the edge stamp.
    assert hops["0xterm"]["kind"] == "bridge"
    assert hops["0xterm"]["confidence"] == 0.75
