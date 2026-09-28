"""M22 tests: mixer correlation heuristics.

A mixer-deposit terminal is still a terminal (no deterministic unmixing —
the plan forbids it). M22 ranks *candidate* withdrawals from the same
pool with timing / anonymity-set / gas-funding heuristics, disclosed as
probabilistic leads. All offline.
"""
from datetime import datetime, timedelta, timezone

import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.graph.builder import TxGraph
from engine.intel.mixer_correlation import (
    anonymity_set_size,
    combined_score,
    correlate_mixer_withdrawals,
    depositor_funders,
    split_pool_txs,
    timing_score,
)
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.traversal.engine import MixerDeposit, traverse
from engine.vasp import CaseDetails

_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
POOL_1ETH = "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"  # 1 ETH pool
DENOM_1ETH = "1000000000000000000"

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


# On-chain-verified Tornado pool selectors (see mixer_correlation.py):
# 0xb214faa5 + value == denomination -> deposit
# 0x21a0adb6 + value == 0             -> withdraw
_REAL_SELECTORS = {"deposit": "0xb214faa5", "withdraw": "0x21a0adb6"}


def _sel(kind: str) -> str:
    return _REAL_SELECTORS[kind]


def _pool_tx(tx_hash, caller, when, kind):
    sel = _sel(kind)
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=_ETH,
        block_time=when,
        inputs=[FlowParty(address=caller, value="0")],
        outputs=[FlowParty(address=POOL_1ETH, value="0")],
        raw={"input": sel + "00" * 100})


# ---------- unit: split / scoring ----------

def test_split_pool_txs_by_selector():
    txs = [_pool_tx("d1", "0xA", T0, "deposit"),
           _pool_tx("w1", "0xB", T0, "withdraw"),
           CanonicalTx(tx_hash="x1", chain=Chain.ETHEREUM, asset=_ETH,
                       inputs=[FlowParty(address="0xC", value="0")],
                       outputs=[FlowParty(address=POOL_1ETH, value="0")],
                       raw={"input": "0xdeadbeef"})]
    deposits, withdrawals = split_pool_txs(txs)
    assert [t.tx_hash for t in deposits] == ["d1"]
    assert [t.tx_hash for t in withdrawals] == ["w1"]


def test_anonymity_set_size_window():
    ds = [_pool_tx("d1", "0xA", T0 - timedelta(hours=25), "deposit"),
          _pool_tx("d2", "0xB", T0 - timedelta(hours=23), "deposit"),
          _pool_tx("d3", "0xC", T0 + timedelta(hours=23), "deposit"),
          _pool_tx("d4", "0xD", T0 + timedelta(hours=25), "deposit")]
    assert anonymity_set_size(ds, T0, 24.0) == 2


def test_timing_score_decays():
    assert timing_score(0, 1440) == 1.0
    assert timing_score(60, 1440) > timing_score(600, 1440)
    assert timing_score(1440, 1440) < 0.06  # exp(-3)


def test_combined_score_falls_with_set_size():
    assert combined_score(0.8, 1) > combined_score(0.8, 100)


def test_depositor_funders_from_graph():
    g = TxGraph.build([
        CanonicalTx(tx_hash="f1", chain=Chain.ETHEREUM, asset=_ETH,
                    inputs=[FlowParty(address="0xFunder", value="1")],
                    outputs=[FlowParty(address="0xDepositor", value="1")]),
        CanonicalTx(tx_hash="f2", chain=Chain.ETHEREUM, asset=_ETH,
                    inputs=[FlowParty(address="0xDepositor", value="1")],
                    outputs=[FlowParty(address=POOL_1ETH, value="1")]),
    ])
    assert depositor_funders(g, "0xDepositor") == {"0xFunder"}


# ---------- correlate ----------

class StubAdapter(ChainAdapter):
    def __init__(self, by_address):
        super().__init__()
        self.chain = Chain.ETHEREUM
        self._by_address = by_address

    async def get_transactions(self, address, limit=100):
        return [t for t in self._by_address.get(address, [])
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


def _deposit_record():
    return MixerDeposit(
        address="0xDepositor", tx_hash="dep1", chain="ethereum",
        mixer="tornado-cash", pool=POOL_1ETH, denomination=DENOM_1ETH,
        block_time=T0)


def _graph_with_funder():
    return TxGraph.build([
        CanonicalTx(tx_hash="f1", chain=Chain.ETHEREUM, asset=_ETH,
                    block_time=T0 - timedelta(days=1),
                    inputs=[FlowParty(address="0xFunder", value="2")],
                    outputs=[FlowParty(address="0xDepositor", value="2")]),
    ])


@pytest.mark.asyncio
async def test_correlate_ranks_timing_and_finds_self_link():
    pool_txs = [
        _pool_tx("dep1", "0xDepositor", T0, "deposit"),
        _pool_tx("d2", "0xOther", T0 - timedelta(hours=2), "deposit"),
        _pool_tx("w_early", "0xRelayer1", T0 - timedelta(hours=1), "withdraw"),
        _pool_tx("w1", "0xCaller1", T0 + timedelta(minutes=30), "withdraw"),
        _pool_tx("w2", "0xCaller2", T0 + timedelta(hours=5), "withdraw"),
        _pool_tx("w_late", "0xCaller3", T0 + timedelta(hours=30), "withdraw"),
    ]
    caller1_txs = [
        CanonicalTx(tx_hash="c1", chain=Chain.ETHEREUM, asset=_ETH,
                    block_time=T0,
                    inputs=[FlowParty(address="0xFunder", value="5")],
                    outputs=[FlowParty(address="0xCaller1", value="5")]),
    ]
    adapter = StubAdapter({POOL_1ETH: pool_txs, "0xCaller1": caller1_txs,
                           "0xCaller2": []})
    cands = await correlate_mixer_withdrawals(
        _deposit_record(), _graph_with_funder(), adapter)

    # w_early (before deposit) and w_late (outside 24h) excluded.
    assert [c.withdrawal_tx for c in cands] == ["w1", "w2"]
    assert cands[0].minutes_after_deposit == 30.0
    assert cands[0].anonymity_set == 2  # dep1 + d2 within +/-24h
    # 0xCaller1 shares 0xFunder with the depositor -> self-link.
    assert cands[0].gas_self_link == ("0xFunder",)
    assert "SHARED FUNDER" in cands[0].note
    assert cands[1].gas_self_link == ()
    assert cands[0].combined_score >= cands[1].combined_score


@pytest.mark.asyncio
async def test_correlate_empty_without_timestamp():
    dep = MixerDeposit(
        address="0xDepositor", tx_hash="dep1", chain="ethereum",
        mixer="tornado-cash", pool=POOL_1ETH, denomination=DENOM_1ETH,
        block_time=None)
    adapter = StubAdapter({})
    assert await correlate_mixer_withdrawals(
        dep, _graph_with_funder(), adapter) == []


@pytest.mark.asyncio
async def test_correlate_empty_when_no_withdrawals_in_window():
    pool_txs = [_pool_tx("dep1", "0xDepositor", T0, "deposit")]
    adapter = StubAdapter({POOL_1ETH: pool_txs})
    assert await correlate_mixer_withdrawals(
        _deposit_record(), _graph_with_funder(), adapter) == []


# ---------- pipeline ----------

def _case(cid="FIR/2026/001234"):
    return CaseDetails(
        case_id=cid, agency="Cyber Cell", officer="Insp. X",
        wallets=("0xDepositor",), tx_hashes=("dep1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")


def _deposit_tx():
    return CanonicalTx(
        tx_hash="dep1", chain=Chain.ETHEREUM, asset=_ETH, block_time=T0,
        inputs=[FlowParty(address="0xDepositor", value=DENOM_1ETH)],
        outputs=[FlowParty(address=POOL_1ETH, value=DENOM_1ETH)],
        raw={"input": _sel("deposit")})


@pytest.mark.asyncio
async def test_pipeline_mixer_terminal_gets_correlation():
    pool_txs = [_pool_tx("dep1", "0xDepositor", T0, "deposit"),
                _pool_tx("w1", "0xCaller1", T0 + timedelta(minutes=45),
                         "withdraw")]
    by_address = {
        "0xDepositor": [_deposit_tx()],
        POOL_1ETH: pool_txs,
        "0xCaller1": [],
    }
    deps = PipelineDeps(
        adapter_factory=lambda c: StubAdapter(by_address))
    result = await run_trace_pipeline("0xDepositor", "ethereum", _case(), deps)

    assert result.terminal_reason == "mixer-deposit"
    assert len(result.mixer_correlation) == 1
    cand = result.mixer_correlation[0]
    assert cand.withdrawal_tx == "w1"
    assert cand.minutes_after_deposit == 45.0
    assert "11. MIXER CORRELATION (PROBABILISTIC — NOT ATTRIBUTION)" \
        in result.report.text
    assert "w1"[:8] in result.report.text
    # Confidence untouched: still the probabilistic mixer note.
    assert any("probabilistic, not deterministic" in n
               for n in result.attribution.notes)
    # The +40 mixer risk signal stands alone — no attribution signal added.
    assert not any("mixer-withdrawal" in s.name
                   for s in result.risk.signals)


@pytest.mark.asyncio
async def test_pipeline_correlation_failure_does_not_fail_trace():
    def factory(c):
        if c == "ethereum":
            return StubAdapter({"0xDepositor": [_deposit_tx()]})
        raise RuntimeError("indexer down")

    deps = PipelineDeps(adapter_factory=factory)
    result = await run_trace_pipeline("0xDepositor", "ethereum", _case(), deps)
    assert result.terminal_reason == "mixer-deposit"
    assert result.mixer_correlation == ()


def test_traversal_records_mixer_deposit():
    g = TxGraph.build([_deposit_tx()])
    result = traverse(g, "0xDepositor")
    assert len(result.mixer_deposits) == 1
    md = result.mixer_deposits[0]
    assert md.pool == POOL_1ETH
    assert md.mixer == "tornado-cash"
    assert md.denomination == DENOM_1ETH
    assert md.block_time == T0
