"""M8 tests: persistent graph store (memory backend) + pipeline wiring.

All offline: MemoryGraphStore. The real-Neo4j round trip is covered by
scripts/smoke_m8_integration.py, gated on a reachable bolt port.
"""
import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.graph import (
    MemoryGraphStore, TxGraph, get_graph_store, restore_graph,
    snapshot_graph,
)
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.vasp import CaseDetails


def _tx(tx_hash: str, src: str, dst: str, value: str = "100") -> CanonicalTx:
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)])


def _graph() -> TxGraph:
    g = TxGraph()
    g.add_tx(_tx("0xh1", "0xaaa", "0xbbb"))
    g.add_tx(_tx("0xh2", "0xbbb", "0xccc"))
    g.label("0xccc", "sweep-consolidation")
    return g


# ---------- snapshot round-trip ----------

def test_snapshot_restore_is_lossless():
    g = _graph()
    restored = restore_graph(snapshot_graph(g))
    assert restored.stats() == g.stats()
    assert restored.labels("0xccc") == {"sweep-consolidation"}
    assert set(restored.g.nodes["0xaaa"]["chains"]) == {"ethereum"}
    assert restored.tx("0xh1").tx_hash == "0xh1"
    edges = list(restored.g.edges(keys=True, data=True))
    assert len(edges) == 2
    assert all(d["tx_hash"] in ("0xh1", "0xh2") for _, _, _, d in edges)


# ---------- memory store ----------

async def test_save_load_round_trip():
    store = MemoryGraphStore()
    stats = await store.save_case_subgraph("case-1", _graph())
    assert stats == {"addresses": 3, "transfers": 2, "transactions": 2}
    assert await store.case_stats("case-1") == stats
    loaded = await store.load_case_subgraph("case-1")
    assert loaded is not None
    assert loaded.stats() == stats
    assert loaded.labels("0xccc") == {"sweep-consolidation"}
    assert await store.load_case_subgraph("nope") is None
    assert await store.case_stats("nope") is None
    assert store.backend == "memory"
    await store.close()


async def test_tags_carry_provenance():
    store = MemoryGraphStore()
    await store.tag_address("0xccc", "ethereum", "sweep-consolidation",
                            source="traversal:case-1", case_id="case-1")
    # idempotent on (tag, source)
    await store.tag_address("0xccc", "ethereum", "sweep-consolidation",
                            source="traversal:case-1", case_id="case-1")
    tags = await store.address_tags("0xccc", "ethereum")
    assert len(tags) == 1
    assert tags[0]["tag"] == "sweep-consolidation"
    assert tags[0]["source"] == "traversal:case-1"
    assert await store.address_tags("0xzzz", "ethereum") == []
    await store.close()


async def test_cases_for_address_links_two_cases():
    store = MemoryGraphStore()
    g1, g2 = _graph(), TxGraph()
    g2.add_tx(_tx("0xh9", "0xccc", "0xddd"))  # shares 0xccc with g1
    await store.save_case_subgraph("case-1", g1)
    await store.save_case_subgraph("case-2", g2)
    assert await store.cases_for_address("0xccc", "ethereum") == [
        "case-1", "case-2"]
    assert await store.cases_for_address("0xaaa", "ethereum") == ["case-1"]
    assert await store.cases_for_address("0xddd", "ethereum") == ["case-2"]
    await store.close()


async def test_addresses_with_tag():
    store = MemoryGraphStore()
    await store.save_case_subgraph("case-1", _graph())
    await store.tag_address("0xccc", "ethereum", "mixer-deposit",
                            source="manual:officer-x")
    found = await store.addresses_with_tag("mixer-deposit")
    assert found == [{"address": "0xccc", "chain": "ethereum"}]
    assert await store.addresses_with_tag("nope") == []
    await store.close()


# ---------- factory fallback ----------

def test_get_graph_store_falls_back_without_neo4j(monkeypatch):
    import api.core.config as cfg

    monkeypatch.setattr(cfg.settings, "neo4j_uri",
                        "bolt://127.0.0.1:1")  # nothing listens here
    store = get_graph_store()
    assert store.backend == "memory"


# ---------- pipeline wiring ----------

class StubAdapter(ChainAdapter):
    chain = Chain.ETHEREUM

    def __init__(self, txs):
        super().__init__()
        self._txs = txs

    async def get_transactions(self, address, limit=100):
        return [t for t in self._txs
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


def _case() -> CaseDetails:
    return CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xaaa",), tx_hashes=(), date_from="2026-01-01",
        date_to="2026-09-27", suspected_offence="test")


async def test_pipeline_persists_subgraph_and_terminal_tags():
    store = MemoryGraphStore()
    txs = [_tx("0xh1", "0xaaa", "0xbbb"), _tx("0xh2", "0xbbb", "0xccc")]
    deps = PipelineDeps(
        adapter_factory=lambda c: StubAdapter(txs),
        graph_store=store, case_id="case-9")
    result = await run_trace_pipeline("0xaaa", "ethereum", _case(), deps)
    assert result.terminal_address is not None

    stats = await store.case_stats("case-9")
    assert stats is not None and stats["addresses"] >= 2
    loaded = await store.load_case_subgraph("case-9")
    assert loaded is not None
    assert loaded.stats() == stats

    # terminal addresses got their traversal classification as tags
    tags = await store.address_tags(result.terminal_address, "ethereum")
    assert any(t["source"] == "traversal:case-9" for t in tags)
    await store.close()


async def test_pipeline_without_store_skips_persistence():
    deps = PipelineDeps(adapter_factory=lambda c: StubAdapter([]))
    result = await run_trace_pipeline("0xaaa", "ethereum", _case(), deps)
    assert result.terminal_reason == "dead-end"
