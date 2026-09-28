"""M23 tests: structuring / smurfing risk signals.

Structuring shapes (fan-out burst, fan-in burst, sub-threshold
cluster) become additive risk signals. All offline.
"""
from datetime import datetime, timedelta, timezone

import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.intel.structuring import (
    analyze_structuring,
)
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.scoring.risk import score_risk
from engine.vasp import CaseDetails

_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _tx(h, frm, to, eth_value, when):
    wei = str(int(eth_value * 10 ** 18))
    return CanonicalTx(
        tx_hash=h, chain=Chain.ETHEREUM, asset=_ETH, block_time=when,
        inputs=[FlowParty(address=frm, value=wei)],
        outputs=[FlowParty(address=to, value=wei)])


def _burst(addr, n, eth_value, direction, start=T0, step_hours=2):
    txs = []
    for i in range(n):
        when = start + timedelta(hours=i * step_hours)
        peer = f"0xPeer{i:02d}"
        frm, to = (addr, peer) if direction == "out" else (peer, addr)
        jitter = 1 + (0.01 if i % 2 else -0.01)
        txs.append(_tx(f"h{i:02d}", frm, to, eth_value * jitter, when))
    return txs


# ---------- detectors ----------

def test_fan_out_burst_detected():
    txs = _burst("0xSubject", 10, 1.0, "out")
    findings = analyze_structuring(txs, "0xSubject")
    fan = [f for f in findings if f.pattern == "fan-out-burst"]
    assert len(fan) == 1
    assert fan[0].count == 10
    assert fan[0].points == 15
    assert fan[0].cv <= 0.30


def test_fan_in_burst_detected():
    txs = _burst("0xSubject", 9, 0.5, "in")
    findings = analyze_structuring(txs, "0xSubject")
    fan = [f for f in findings if f.pattern == "fan-in-burst"]
    assert len(fan) == 1
    assert fan[0].count == 9


def test_sub_threshold_cluster_detected():
    txs = _burst("0xSubject", 6, 9.9, "in", step_hours=3)
    findings = analyze_structuring(txs, "0xSubject")
    sub = [f for f in findings if f.pattern == "sub-threshold"]
    assert len(sub) == 1
    assert sub[0].round_number == 10
    assert sub[0].points == 10


def test_no_findings_for_random_activity():
    txs = [_tx(f"r{i}", f"0xP{i}", "0xSubject", v,
               T0 + timedelta(days=i * 9))
           for i, v in enumerate([0.13, 4.7, 0.02, 88.0, 1.111])]
    assert analyze_structuring(txs, "0xSubject") == []


def test_no_finding_below_count_threshold():
    txs = _burst("0xSubject", 5, 1.0, "out")  # 5 < 8
    assert not [f for f in analyze_structuring(txs, "0xSubject")
                if f.pattern == "fan-out-burst"]


def test_high_variance_amounts_not_a_burst():
    txs = [_tx(f"v{i}", "0xSubject", f"0xP{i}", v, T0 + timedelta(hours=i))
           for i, v in enumerate(
               [0.1, 5.0, 0.02, 50.0, 1.0, 0.7, 33.0, 2.2, 0.05, 9.9])]
    assert not [f for f in analyze_structuring(txs, "0xSubject")
                if f.pattern == "fan-out-burst"]


def test_sub_threshold_needs_round_proximity():
    # 6 similar txs at 0.5 ETH: a burst, but NOT just-below-a-round.
    txs = _burst("0xSubject", 6, 0.5, "in", step_hours=3)
    findings = analyze_structuring(txs, "0xSubject")
    assert not [f for f in findings if f.pattern == "sub-threshold"]


# ---------- risk integration ----------

def test_score_risk_structuring_signals():
    txs = _burst("0xSubject", 8, 0.99, "in", step_hours=2)
    findings = analyze_structuring(txs, "0xSubject")
    assert findings  # fan-in (+ sub-threshold, 0.99 < 1)
    risk = score_risk([], structuring_findings=tuple(findings))
    names = [s.name for s in risk.signals]
    assert "structuring-fan-in" in names
    assert "structuring-sub-threshold" in names
    assert risk.total == sum(s.points for s in risk.signals) <= 100


# ---------- pipeline ----------

class StubAdapter(ChainAdapter):
    def __init__(self, by_address):
        super().__init__()
        self.chain = Chain.ETHEREUM
        self._by_address = by_address
        self.calls: list[str] = []

    async def get_transactions(self, address, limit=100):
        self.calls.append(address)
        return [t for t in self._by_address.get(address, [])
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


def _case():
    return CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xSubject",), tx_hashes=("s1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")


@pytest.mark.asyncio
async def test_pipeline_terminal_structuring_becomes_risk_signal():
    subject_tx = _tx("s1", "0xSubject", "0xTerminal", 0.99, T0)
    # 7 mules + subject = 8 distinct depositors: below the M19 OTC
    # relabel threshold (10), so the terminal stays a dead-end and this
    # test isolates M23 behavior.
    terminal_in = ([_tx(f"t{i}", f"0xMule{i:02d}", "0xTerminal", 0.99,
                        T0 - timedelta(hours=i * 2)) for i in range(7)]
                   + [subject_tx])
    adapter = StubAdapter({"0xSubject": [subject_tx],
                           "0xTerminal": terminal_in})
    deps = PipelineDeps(adapter_factory=lambda c: adapter)
    result = await run_trace_pipeline("0xSubject", "ethereum", _case(), deps)

    assert result.terminal_reason == "dead-end"
    names = [s.name for s in result.risk.signals]
    assert "structuring-fan-in" in names
    assert "structuring-fan-in (+15)" in result.report.text
    assert len(result.structuring_findings) >= 1
    # The terminal's history was actually consulted.
    assert "0xTerminal" in adapter.calls


@pytest.mark.asyncio
async def test_pipeline_skips_terminal_history_for_mixer_pool():
    pool = "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"  # 1 ETH Tornado
    deposit = CanonicalTx(
        tx_hash="d1", chain=Chain.ETHEREUM, asset=_ETH, block_time=T0,
        inputs=[FlowParty(address="0xSubject", value="1000000000000000000")],
        outputs=[FlowParty(address=pool, value="1000000000000000000")],
        raw={"input": "0xb214faa5"})
    adapter = StubAdapter({"0xSubject": [deposit], pool: [deposit]})
    deps = PipelineDeps(adapter_factory=lambda c: adapter)
    result = await run_trace_pipeline("0xSubject", "ethereum", _case(), deps)

    assert result.terminal_reason == "mixer-deposit"
    # The pool's history IS fetched by M22 correlation (withdrawal
    # candidates) — but structuring must not touch it: no findings.
    assert result.structuring_findings == ()
    assert not [s for s in result.risk.signals
                if s.name.startswith("structuring-")]
