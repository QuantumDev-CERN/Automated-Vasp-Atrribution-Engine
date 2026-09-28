"""M20 tests: CoinJoin detection as a traversal terminal.

M17 built the structural detector (used as a clustering guard); M20
wires it into the classifier so the walk STOPS at a CoinJoin — no
deterministic unmixing, per the master plan. All offline.
"""
import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.classifier.hops import HopKind, classify_graph
from engine.graph.builder import TxGraph
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.scoring import score_attribution, score_risk
from engine.traversal.engine import VisitedNode, traverse
from engine.vasp import CaseDetails

_BTC = Asset(kind=AssetKind.NATIVE, chain=Chain.BITCOIN, symbol="BTC")


def _tx(tx_hash, inputs, outputs):
    """inputs/outputs: list of (address, value_int)."""
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.BITCOIN, asset=_BTC,
        inputs=[FlowParty(address=a, value=str(v)) for a, v in inputs],
        outputs=[FlowParty(address=a, value=str(v)) for a, v in outputs])


def _coinjoin_tx():
    # 12 distinct inputs, 7 identical 0.1-BTC outputs + 2 change outputs:
    # the classic Wasabi-round shape.
    inputs = [(f"in{i}", 11_000_000) for i in range(12)]
    outputs = ([(f"out{i}", 10_000_000) for i in range(7)]
               + [("chg0", 5_000_000), ("chg1", 3_000_000)])
    return _tx("cj1", inputs, outputs)


def _kinds_of(graph, tx_hash):
    out = classify_graph(graph)
    return {c.kind for (s, d, k), c in out.items()
            if k.startswith(tx_hash + ":")}


# ---------- classifier ----------

def test_coinjoin_shape_classified_coinjoin():
    g = TxGraph.build([_coinjoin_tx()])
    assert _kinds_of(g, "cj1") == {HopKind.COINJOIN}


def test_coinjoin_beats_peel_and_sweep_heuristics():
    # Many-in/many-out must never be misread as a custodial sweep or a
    # peel chain — both would be false-attribution failures.
    g = TxGraph.build([_coinjoin_tx()])
    kinds = _kinds_of(g, "cj1")
    assert HopKind.SWEEP_CANDIDATE not in kinds
    assert HopKind.PEEL not in kinds


def test_ordinary_payment_not_coinjoin():
    g = TxGraph.build([_tx("p1", [("alice", 100)], [("bob", 60), ("alice", 40)])])
    assert HopKind.COINJOIN not in _kinds_of(g, "p1")


def test_sweep_shape_not_coinjoin():
    sweep = _tx("sw1", [(f"dep{i}", 10) for i in range(8)], [("hot", 79)])
    g = TxGraph.build([sweep])
    kinds = _kinds_of(g, "sw1")
    assert HopKind.COINJOIN not in kinds
    assert HopKind.SWEEP_CANDIDATE in kinds


def test_coinjoin_classification_carries_evidence():
    g = TxGraph.build([_coinjoin_tx()])
    out = classify_graph(g)
    c = next(c for (s, d, k), c in out.items() if k.startswith("cj1:"))
    assert c.confidence == 0.80
    assert c.details["n_inputs"] == 12
    assert c.details["max_equal_outputs"] == 7
    assert "coinjoin" in c.reason


# ---------- traversal ----------

def test_traversal_stops_at_coinjoin_without_unmixing():
    # out0 is spent onward — the walk must NOT follow it.
    g = TxGraph.build([
        _coinjoin_tx(),
        _tx("spend1", [("out0", 10_000_000)], [("final", 9_900_000)]),
    ])
    r = traverse(g, "in0")
    assert r.terminals, "expected coinjoin terminals"
    assert {t.reason for t in r.terminals} == {"coinjoin"}
    visited = {v.address for v in r.visited}
    assert "final" not in visited  # no deterministic unmixing
    # terminals ARE reached (and recorded as visited) — just not walked past
    assert visited == {"in0"} | {f"out{i}" for i in range(7)} | {"chg0", "chg1"}
    assert r.labels_applied.get("in0") == ["coinjoin-depositor"]


# ---------- scoring ----------

def test_risk_signal_fires_on_coinjoin_terminal():
    risk = score_risk([], terminal_reason="coinjoin")
    sig = next(s for s in risk.signals if s.name == "coinjoin")
    assert sig.points == 40
    assert risk.total == 40


def test_risk_signal_fires_on_coinjoin_hop_kind():
    visited = [VisitedNode(address="a", hop=0),
               VisitedNode(address="b", hop=1, via_kind="coinjoin")]
    risk = score_risk(visited)
    assert any(s.name == "coinjoin" for s in risk.signals)


def test_confidence_note_discloses_probabilistic():
    score = score_attribution([VisitedNode(address="a", hop=0)],
                              terminal_reason="coinjoin")
    assert any("probabilistic, not deterministic" in n for n in score.notes)


def test_coinjoin_hop_discount_is_half():
    visited = [VisitedNode(address="a", hop=0),
               VisitedNode(address="b", hop=1, via_kind="coinjoin",
                           via_confidence=0.8)]
    score = score_attribution(visited)
    assert score.hops[0].discount == 0.50


# ---------- pipeline ----------

class StubAdapter(ChainAdapter):
    chain = Chain.BITCOIN

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


def _case(cid="FIR/2026/001234"):
    return CaseDetails(
        case_id=cid, agency="Cyber Cell", officer="Insp. X",
        wallets=("in0",), tx_hashes=("cj1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")


@pytest.mark.asyncio
async def test_pipeline_coinjoin_terminal_end_to_end():
    txs = [_coinjoin_tx(),
           _tx("spend1", [("out0", 10_000_000)], [("final", 9_900_000)])]
    deps = PipelineDeps(adapter_factory=lambda c: StubAdapter(txs))
    result = await run_trace_pipeline("in0", "bitcoin", _case(), deps)

    assert result.terminal_reason == "coinjoin"
    assert any(s.name == "coinjoin" and s.points == 40
               for s in result.risk.signals)
    assert "CoinJoin — funds entered a collaborative anonymity set" \
        in result.report.text
    assert any("probabilistic, not deterministic" in n
               for n in result.attribution.notes)
