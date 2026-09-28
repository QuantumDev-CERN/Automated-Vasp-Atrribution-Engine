"""M19 tests: OTC/hawala terminus detection + persistent registry.

All offline: stub adapter with scripted per-address histories and the
in-memory graph store. Live behavior (detection against real indexer
data) is covered by scripts/smoke_m19.py.
"""
import pytest

from engine.adapters.base import (
    AdapterError,
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    ChainAdapter,
    FlowParty,
)
from engine.graph import MemoryGraphStore
from engine.intel.otc import (
    HISTORY_WINDOW,
    MIN_DISTINCT_SENDERS,
    OTC_TERMINUS_TAG,
    OtcAssessment,
    assess_otc_pattern,
    detect_otc_termini,
    registry_contains,
    registry_size,
)
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.scoring import score_attribution, score_risk
from engine.traversal.engine import VisitedNode
from engine.vasp import CaseDetails


def _tx(tx_hash, src, dst, value="100"):
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)])


def _collector_history(collector, n_senders=12, with_outbound=False):
    txs = [_tx(f"0xdep{i:02d}", f"0xsender{i:02d}", collector)
           for i in range(n_senders)]
    if with_outbound:
        txs.append(_tx("0xout1", collector, "0xsomewhere"))
    return txs


class ScriptedAdapter(ChainAdapter):
    """Per-address histories; records every get_transactions call."""
    chain = Chain.ETHEREUM

    def __init__(self, history):
        super().__init__()
        self._history = history
        self.calls = []

    async def get_transactions(self, address, limit=100):
        self.calls.append(address)
        return self._history.get(address, [])[:limit]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


class FailingAdapter(ScriptedAdapter):
    async def get_transactions(self, address, limit=100):
        raise AdapterError("indexer down")


def _case(cid="FIR/2026/001234"):
    return CaseDetails(
        case_id=cid, agency="Cyber Cell", officer="Insp. X",
        wallets=("0xsubject",), tx_hashes=("0xt1",),
        date_from="2026-01-01", date_to="2026-09-27",
        suspected_offence="test")


# ---------- assess_otc_pattern ----------

def test_pattern_fires_on_many_disparate_depositors_no_outbound():
    a = _collector_history("0xcollector")
    got = assess_otc_pattern("0xcollector", a, "ethereum")
    assert isinstance(got, OtcAssessment)
    assert got.is_terminus
    assert got.distinct_senders == 12
    assert got.inbound_count == 12
    assert got.outbound_count == 0


def test_pattern_needs_min_distinct_senders():
    a = _collector_history("0xcollector", n_senders=MIN_DISTINCT_SENDERS - 1)
    got = assess_otc_pattern("0xcollector", a, "ethereum")
    assert not got.is_terminus
    assert got.distinct_senders == MIN_DISTINCT_SENDERS - 1


def test_pattern_rejected_when_any_outbound_movement():
    a = _collector_history("0xcollector", with_outbound=True)
    got = assess_otc_pattern("0xcollector", a, "ethereum")
    assert not got.is_terminus
    assert got.outbound_count == 1


def test_self_transfers_are_movement_not_depositors():
    txs = [_tx(f"0xself{i}", "0xcollector", "0xcollector") for i in range(12)]
    got = assess_otc_pattern("0xcollector", txs, "ethereum")
    assert not got.is_terminus  # outbound > 0
    assert got.distinct_senders == 0  # self excluded from depositor set


def test_evm_sender_dedup_is_case_insensitive():
    txs = [_tx(f"0xdep{i}", "0xABCDEF" if i % 2 else "0xabcdef", "0xcollector")
           for i in range(12)]
    got = assess_otc_pattern("0xcollector", txs, "ethereum")
    assert got.distinct_senders == 1
    assert not got.is_terminus


def test_empty_history_is_not_a_terminus():
    got = assess_otc_pattern("0xcollector", [], "ethereum")
    assert not got.is_terminus


# ---------- detect_otc_termini ----------

@pytest.mark.asyncio
async def test_detect_returns_only_firing_addresses():
    adapter = ScriptedAdapter({
        "0xhot": _collector_history("0xhot"),
        "0xcold": _collector_history("0xcold", n_senders=3),
    })
    hits = await detect_otc_termini(adapter, "ethereum", ["0xhot", "0xcold"])
    assert list(hits) == ["0xhot"]
    assert hits["0xhot"].is_terminus


@pytest.mark.asyncio
async def test_detect_skips_failed_indexer_calls():
    hits = await detect_otc_termini(FailingAdapter({}), "ethereum", ["0xhot"])
    assert hits == {}


# ---------- scoring ----------

def test_risk_signal_fires_on_otc_terminus():
    risk = score_risk([], terminal_reason=OTC_TERMINUS_TAG)
    names = [s.name for s in risk.signals]
    assert "otc-hawala-terminus" in names
    sig = next(s for s in risk.signals if s.name == "otc-hawala-terminus")
    assert sig.points == 30
    assert risk.total == 30


def test_confidence_note_explains_terminus():
    score = score_attribution([VisitedNode(address="0xsubject", hop=0)],
                              terminal_reason=OTC_TERMINUS_TAG)
    assert any("no further on-chain trail is expected" in n
               for n in score.notes)


# ---------- pipeline: detection + persistence + short-circuit ----------

@pytest.mark.asyncio
async def test_pipeline_relabels_dead_end_and_persists_registry():
    collector = "0xcollector"
    history = {
        "0xsubject": [_tx("0xt1", "0xsubject", collector)],
        collector: _collector_history(collector),
    }
    adapter = ScriptedAdapter(history)
    store = MemoryGraphStore()
    deps = PipelineDeps(adapter_factory=lambda c: adapter,
                        graph_store=store, case_id="case-1")
    result = await run_trace_pipeline("0xsubject", "ethereum", _case("case-1"),
                                      deps)

    assert result.terminal_reason == OTC_TERMINUS_TAG
    assert result.terminal_address == collector
    # registry persisted the confirmed terminus
    entry = await registry_contains(store, collector, "ethereum")
    assert entry is not None and entry["tag"] == OTC_TERMINUS_TAG
    assert await registry_size(store) == 1
    # risk + report reflect the terminus, not a failed trace
    assert any(s.name == "otc-hawala-terminus" for s in result.risk.signals)
    assert "OTC/hawala terminus — no further on-chain trail expected" \
        in result.report.text
    assert "dead-end" not in result.report.text.split("Trail stopped:")[1] \
        .split("\n")[0]


@pytest.mark.asyncio
async def test_pipeline_short_circuits_known_terminus():
    collector = "0xcollector"
    store = MemoryGraphStore()
    await store.tag_address(collector, "ethereum", OTC_TERMINUS_TAG,
                            source="traversal:case-1", case_id="case-1")
    adapter = ScriptedAdapter({})
    deps = PipelineDeps(adapter_factory=lambda c: adapter,
                        graph_store=store, case_id="case-2")
    result = await run_trace_pipeline(collector, "ethereum", _case("case-2"),
                                      deps)

    assert result.terminal_reason == OTC_TERMINUS_TAG
    # no expansion happened: the indexer was never touched
    assert adapter.calls == []
    assert result.risk.total == 30  # terminus risk signal still fires


@pytest.mark.asyncio
async def test_pipeline_leaves_active_terminal_alone():
    collector = "0xcollector"
    history = {
        "0xsubject": [_tx("0xt1", "0xsubject", collector)],
        collector: _collector_history(collector, with_outbound=True),
    }
    adapter = ScriptedAdapter(history)
    store = MemoryGraphStore()
    deps = PipelineDeps(adapter_factory=lambda c: adapter,
                        graph_store=store, case_id="case-1")
    result = await run_trace_pipeline("0xsubject", "ethereum", _case("case-1"),
                                      deps)

    assert result.terminal_reason == "dead-end"
    assert await registry_contains(store, collector, "ethereum") is None
    assert await registry_size(store) == 0


@pytest.mark.asyncio
async def test_non_dead_end_terminal_is_never_relabelled():
    # A sweep-consolidation terminal already has a better explanation;
    # the OTC pass must not touch it even if fan-in is high.
    from engine.traversal.engine import Terminal, TraversalResult
    adapter = ScriptedAdapter({"0xcollector": _collector_history("0xcollector")})
    result = TraversalResult(
        start="0xsubject",
        visited=[VisitedNode(address="0xsubject", hop=0)],
        terminals=[Terminal(address="0xcollector",
                            reason="sweep-consolidation")])
    from engine.jobs.pipeline import _relabel_otc_termini
    await _relabel_otc_termini(result, adapter, "ethereum")
    assert result.terminals[0].reason == "sweep-consolidation"
    assert adapter.calls == []  # no history fetch for non-candidates
