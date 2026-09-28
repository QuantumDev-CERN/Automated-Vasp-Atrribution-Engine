"""M22 live smoke: mixer correlation heuristics.

Two parts:

1. Live section (needs ETHERSCAN_API_KEY): pull recent history of the
   1 ETH Tornado Cash pool, split deposits/withdrawals by selector, and
   run the full correlate_mixer_withdrawals() against the real
   EvmAdapter on the pool's latest deposit. Prints the top candidate
   withdrawals (timing / anonymity-set / gas-funding self-link).
   Tornado Cash is OFAC-sanctioned; reading its public pool history is
   evidence-gathering, and every candidate is labeled a lead, never an
   attribution.

2. Synthetic pipeline (offline, deterministic): a scripted adapter
   feeds a mixer-deposit terminal plus pool history with a known
   withdrawal 45 min later. The trace must keep its mixer-deposit
   terminal (no unmixing claimed), record the candidate, and render
   the probabilistic report section.

Transient indexer errors SKIP the live section instead of failing
the gate.

Run: uv run python scripts/smoke_m22.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.adapters.evm import EvmAdapter
from engine.graph.builder import TxGraph
from engine.intel.mixer_correlation import (
    correlate_mixer_withdrawals,
    split_pool_txs,
)
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.traversal.engine import MixerDeposit
from engine.vasp import CaseDetails

POOL_1ETH = "0x47CE0C6eD5B0Ce3d3A51fdb1C52DC66a7c3c2936"
DENOM_1ETH = "1000000000000000000"
_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")


class SectionSkip(Exception):
    pass


def load_env() -> None:
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _transient(message: str) -> bool:
    m = message.lower()
    return any(
        h in m
        for h in ("429", "502", "503", "504", "timeout", "timed out",
                  "connecterror", "connection reset", "temporarily unavailable",
                  "rate limit")
    )


async def section_live_pool_scan() -> None:
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        raise SectionSkip("m22: ETHERSCAN_API_KEY missing from .env")
    adapter = EvmAdapter(Chain.ETHEREUM, api_key=key)
    try:
        pool_txs = await adapter.get_transactions(POOL_1ETH, limit=300)
    except Exception as e:  # noqa: BLE001
        raise SectionSkip(f"m22: pool txlist failed: {e}")
    deposits, withdrawals = split_pool_txs(pool_txs)
    print(f"[live] pool txs: {len(pool_txs)} "
          f"({len(deposits)} deposits, {len(withdrawals)} withdrawals)")
    if not deposits or not withdrawals:
        raise SectionSkip("m22: pool history lacks deposits/withdrawals "
                          "(live-data drift)")
    latest = max((d for d in deposits if d.block_time),
                 key=lambda d: d.block_time, default=None)
    if latest is None:
        raise SectionSkip("m22: no timestamped deposit found")
    depositor = latest.inputs[0].address if latest.inputs else ""
    graph = TxGraph.build([latest])
    dep = MixerDeposit(
        address=depositor, tx_hash=latest.tx_hash, chain="ethereum",
        mixer="tornado-cash", pool=POOL_1ETH, denomination=DENOM_1ETH,
        block_time=latest.block_time)
    cands = await correlate_mixer_withdrawals(
        dep, graph, adapter, window_hours=24.0, top_n=5)
    print(f"[live] {len(cands)} candidate withdrawal(s) in the 24h after "
          f"deposit {latest.tx_hash[:12]}…")
    for c in cands[:3]:
        print(f"[live]   {c.withdrawal_tx[:12]}… caller {c.caller[:12]}… "
              f"{c.minutes_after_deposit:.0f} min after, score "
              f"{c.combined_score}, set {c.anonymity_set}, "
              f"self-link={bool(c.gas_self_link)}")
    print("[live] candidates are leads, not attribution — "
          "see report section 11")


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


async def section_synthetic_pipeline() -> None:
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)

    def pool_tx(h, caller, when, kind):
        sel = ("0xb214faa5" if kind == "deposit" else "0x21a0adb6")
        return CanonicalTx(
            tx_hash=h, chain=Chain.ETHEREUM, asset=_ETH, block_time=when,
            inputs=[FlowParty(address=caller, value="0")],
            outputs=[FlowParty(address=POOL_1ETH, value="0")],
            raw={"input": sel + "00" * 100})

    deposit_tx = CanonicalTx(
        tx_hash="dep1", chain=Chain.ETHEREUM, asset=_ETH, block_time=t0,
        inputs=[FlowParty(address="0xDepositor", value=DENOM_1ETH)],
        outputs=[FlowParty(address=POOL_1ETH, value=DENOM_1ETH)],
        raw={"input": "0xb214faa5"})
    pool_txs = [pool_tx("dep1", "0xDepositor", t0, "deposit"),
                pool_tx("w1", "0xCaller1", t0 + timedelta(minutes=45),
                        "withdraw")]
    by_address = {"0xDepositor": [deposit_tx],
                  POOL_1ETH: pool_txs, "0xCaller1": []}
    deps = PipelineDeps(adapter_factory=lambda c: StubAdapter(by_address))
    case = CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xDepositor",), tx_hashes=("dep1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")
    result = await run_trace_pipeline("0xDepositor", "ethereum", case, deps)
    assert result.terminal_reason == "mixer-deposit", result.terminal_reason
    assert len(result.mixer_correlation) == 1
    assert "MIXER CORRELATION (PROBABILISTIC" in result.report.text
    print("[synthetic] mixer-deposit terminal kept; 1 candidate ranked; "
          "report section 11 rendered")


async def main() -> int:
    load_env()
    ok = True
    try:
        await section_live_pool_scan()
        print("PASS: live Tornado pool correlation scan")
    except SectionSkip as s:
        print(f"SKIP: {s}")
    except Exception as e:  # noqa: BLE001
        if _transient(str(e)):
            print(f"SKIP (transient): {e}")
        else:
            print(f"FAIL: {e}")
            ok = False
    try:
        await section_synthetic_pipeline()
        print("PASS: synthetic mixer-correlation pipeline")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
