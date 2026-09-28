"""Resilience tests for graph expansion (the silent-empty-trace fix).

Regression coverage for the failure mode where a transient indexer
error during the subject's expansion was swallowed by a bare
``except Exception: continue``, producing a confident empty dead-end
report from zero graph data.

New behavior:
- each address expansion is retried (bounded, with backoff);
- if the SUBJECT still fails, the trace raises ExpansionError instead
  of fabricating a dead-end — the seed retries the whole trace;
- a non-subject address that keeps failing is skipped with a warning
  and the trace continues on the partial graph.
"""
import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.jobs import ExpansionError, PipelineDeps, run_trace_pipeline
from engine.vasp import CaseDetails


def _tx(tx_hash: str, src: str, dst: str) -> CanonicalTx:
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value="100")],
        outputs=[FlowParty(address=dst, value="100")])


class FlakyAdapter(ChainAdapter):
    """Fails get_transactions for chosen addresses a set number of times.

    fail_map: {address: remaining failures}. Addresses absent from the
    map (or exhausted) serve txs normally.
    """
    chain = Chain.ETHEREUM

    def __init__(self, txs: list[CanonicalTx],
                 fail_map: dict[str, int] | None = None):
        super().__init__()
        self._txs = txs
        self._fail_map = dict(fail_map or {})

    async def get_transactions(self, address: str,
                               limit: int = 100) -> list[CanonicalTx]:
        remaining = self._fail_map.get(address, 0)
        if remaining > 0:
            self._fail_map[address] = remaining - 1
            raise RuntimeError("simulated indexer 429")
        return [t for t in self._txs
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address: str,
                                  limit: int = 100) -> list[CanonicalTx]:
        return []

    async def health_check(self) -> bool:
        return True


def _case() -> CaseDetails:
    return CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xsubject",), tx_hashes=("0xt1",),
        date_from="2026-01-01", date_to="2026-09-27",
        suspected_offence="test")


@pytest.mark.asyncio
async def test_subject_expansion_failure_raises_not_fake_dead_end():
    """Subject that never expands -> ExpansionError, never a 1.0 dead-end."""
    adapter = FlakyAdapter([], fail_map={"0xsubject": 999})
    deps = PipelineDeps(adapter_factory=lambda c: adapter)
    with pytest.raises(ExpansionError):
        await run_trace_pipeline("0xsubject", "ethereum", _case(), deps)


@pytest.mark.asyncio
async def test_subject_transient_failure_recovers_via_retry():
    """Subject failing twice then succeeding -> full trace, no exception."""
    txs = [_tx("0xt1", "0xsubject", "0xmid"),
           _tx("0xt2", "0xmid", "0xfinal")]
    adapter = FlakyAdapter(txs, fail_map={"0xsubject": 2})
    deps = PipelineDeps(adapter_factory=lambda c: adapter)
    result = await run_trace_pipeline("0xsubject", "ethereum", _case(), deps)
    assert [n.address for n in result.path] == [
        "0xsubject", "0xmid", "0xfinal"]
    assert result.terminal_address == "0xfinal"


@pytest.mark.asyncio
async def test_non_subject_failure_continues_with_partial_graph():
    """A mid-traversal address that keeps failing is skipped; the trace
    still completes on the partial graph instead of dying."""
    txs = [_tx("0xt1", "0xsubject", "0xmid"),
           _tx("0xt2", "0xmid", "0xfinal")]
    adapter = FlakyAdapter(txs, fail_map={"0xmid": 999})
    deps = PipelineDeps(adapter_factory=lambda c: adapter)
    result = await run_trace_pipeline("0xsubject", "ethereum", _case(), deps)
    # subject expanded (edge subject->mid known); mid never expanded,
    # so the trail honestly ends at mid — with real graph data behind it.
    assert [n.address for n in result.path] == ["0xsubject", "0xmid"]
    assert result.terminal_reason == "dead-end"
    assert result.terminal_address == "0xmid"
