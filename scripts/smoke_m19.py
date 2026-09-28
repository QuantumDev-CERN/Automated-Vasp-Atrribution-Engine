"""M19 live smoke: OTC/hawala terminus detection + persistent registry.

Two parts:

1. Live negative control (bounded): run the real detection pass against
   a well-known high-activity address (the Uniswap V2 router, reused
   from the M4 smoke) via the production EvmAdapter. The router has
   massive inbound AND outbound flow, so the terminus pattern must NOT
   fire. This exercises the live adapter path (native + token history)
   without asserting on any curated OTC address — M19 deliberately
   ships no curated seed list (see engine/intel/otc.py), so there is no
   honest live positive control.

2. Synthetic end-to-end (offline, deterministic): a scripted adapter
   feeds a dead-end collector with 12 disparate depositors and no
   outbound movement. The pipeline must relabel the terminal to
   "otc-hawala-terminus", persist the registry tag, render the explicit
   report label, and short-circuit (zero indexer calls) when the same
   address is traced again under a new case.

Transient indexer/RPC errors SKIP the live section instead of failing
the gate.

Run: uv run python scripts/smoke_m19.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import (
    AdapterError,
    Asset,
    AssetKind,
    CanonicalTx,
    Chain,
    ChainAdapter,
    FlowParty,
)
from engine.adapters.evm import EvmAdapter
from engine.graph import MemoryGraphStore
from engine.intel.otc import (
    OTC_TERMINUS_TAG,
    detect_otc_termini,
    registry_contains,
    registry_size,
)
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.vasp import CaseDetails

# Uniswap V2 router — high fan-in AND high fan-out: must not be flagged.
_ROUTER = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"


class SectionSkip(Exception):
    pass


def load_env() -> None:
    p = Path(".env")
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _transient(message: str) -> bool:
    m = message.lower()
    return any(
        h in m
        for h in ("timeout", "timed out", "connecterror", "connection reset",
                  "temporarily unavailable", "rate limit", "429", "502",
                  "503", "504", "max retries")
    )


def _tx(tx_hash, src, dst, value="100"):
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)])


class ScriptedAdapter(ChainAdapter):
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


def _case(cid):
    return CaseDetails(
        case_id=cid, agency="Cyber Cell", officer="Insp. X",
        wallets=("0xsubject",), tx_hashes=("0xt1",),
        date_from="2026-01-01", date_to="2026-09-27",
        suspected_offence="smoke-m19")


async def section_live_negative_control() -> None:
    """The Uniswap V2 router must NOT match the terminus pattern."""
    key = os.environ.get("ETHERSCAN_API_KEY", "")
    if not key:
        raise SectionSkip("m19: ETHERSCAN_API_KEY missing from .env")
    adapter = EvmAdapter(Chain.ETHEREUM, api_key=key)
    try:
        hits = await detect_otc_termini(adapter, "ethereum", [_ROUTER])
    except AdapterError as exc:
        if _transient(str(exc)):
            raise SectionSkip(f"m19: transient indexer error: {exc}")
        raise
    assert hits == {}, f"router wrongly flagged as OTC terminus: {hits}"
    print(f"m19 live: router {_ROUTER[:12]}… correctly not flagged "
          f"(negative control)")


async def section_synthetic_end_to_end() -> None:
    collector = "0xcollector"
    history = {
        "0xsubject": [_tx("0xt1", "0xsubject", collector)],
        collector: [_tx(f"0xdep{i:02d}", f"0xsender{i:02d}", collector)
                    for i in range(12)],
    }
    adapter = ScriptedAdapter(history)
    store = MemoryGraphStore()

    # Trace 1: detection fires, tag persists.
    deps = PipelineDeps(adapter_factory=lambda c: adapter,
                        graph_store=store, case_id="smoke-1")
    r1 = await run_trace_pipeline("0xsubject", "ethereum", _case("smoke-1"),
                                  deps)
    assert r1.terminal_reason == OTC_TERMINUS_TAG, r1.terminal_reason
    assert await registry_contains(store, collector, "ethereum") is not None
    assert await registry_size(store) == 1
    assert "OTC/hawala terminus — no further on-chain trail expected" \
        in r1.report.text
    print("m19 synthetic: dead-end relabeled, registry tag persisted, "
          "report label rendered")

    # Trace 2: same address under a new case short-circuits the indexer.
    adapter.calls.clear()
    deps2 = PipelineDeps(adapter_factory=lambda c: adapter,
                         graph_store=store, case_id="smoke-2")
    r2 = await run_trace_pipeline(collector, "ethereum", _case("smoke-2"),
                                  deps2)
    assert r2.terminal_reason == OTC_TERMINUS_TAG, r2.terminal_reason
    assert adapter.calls == [], f"indexer touched: {adapter.calls}"
    assert "smoke-1" in r2.report.text, "cross-case brief should link case-1"
    print("m19 synthetic: known terminus short-circuited (0 indexer calls), "
          "prior case linked")


async def main() -> int:
    load_env()
    skipped: list[str] = []
    try:
        await section_live_negative_control()
    except SectionSkip as s:
        skipped.append(str(s))
        print(f"SKIP live section: {s}")
    await section_synthetic_end_to_end()
    if skipped:
        print(f"SMOKE M19 OK (live skipped: {len(skipped)})")
    else:
        print("SMOKE M19 OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
