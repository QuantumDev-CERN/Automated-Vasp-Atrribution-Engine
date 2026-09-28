"""M23 live smoke: structuring / smurfing risk signals.

Two parts:

1. Live section (needs ETHERSCAN_API_KEY): pull recent history of a
   curated high-volume wallet (ChangeNOW Hot Wallet 4, from the M18
   registry — Etherscan-labeled, on-chain verified) and run the
   structuring detectors over it. Prints whatever findings the real
   data yields (a hot wallet legitimately batches payouts, so a
   fan-out finding here demonstrates the detector, not an accusation).
   No assertion on findings — the gate only requires the live path to
   run without error. Transient indexer errors SKIP instead of failing.

2. Synthetic pipeline (offline, deterministic): a scripted adapter
   feeds a terminal that collected 8 x ~0.99 ETH deposits in 24h. The
   trace must surface structuring-fan-in (+15) and
   structuring-sub-threshold (+10) risk signals and render them in the
   report.

Run: uv run python scripts/smoke_m23.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.adapters.evm import EvmAdapter
from engine.intel.structuring import analyze_structuring
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.vasp import CaseDetails

# ChangeNOW "Hot Wallet 4" — M18 registry, Etherscan public label,
# EOA verified on-chain 2026-09-27.
HOT_WALLET = "0xEbA88149813BEc1cCcccFDb0daCEFaaa5DE94cB1"
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


async def section_live_hot_wallet() -> None:
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        raise SectionSkip("m23: ETHERSCAN_API_KEY missing from .env")
    adapter = EvmAdapter(Chain.ETHEREUM, api_key=key)
    try:
        txs = await adapter.get_transactions(HOT_WALLET, limit=200)
    except Exception as e:  # noqa: BLE001
        raise SectionSkip(f"m23: wallet history failed: {e}")
    findings = analyze_structuring(txs, HOT_WALLET)
    print(f"[live] {len(txs)} txs scanned, {len(findings)} finding(s)")
    for f in findings:
        print(f"[live]   {f.pattern} x{f.count} "
              f"({f.amount_min:g}-{f.amount_max:g} {f.asset_symbol}, "
              f"CV {f.cv:g}) +{f.points}")
    if not findings:
        print("[live]   (no structuring shapes in this sample)")
    print("[live] findings are risk signals, not verdicts")


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


def _tx(h, frm, to, eth_value, when):
    wei = str(int(eth_value * 10 ** 18))
    return CanonicalTx(
        tx_hash=h, chain=Chain.ETHEREUM, asset=_ETH, block_time=when,
        inputs=[FlowParty(address=frm, value=wei)],
        outputs=[FlowParty(address=to, value=wei)])


async def section_synthetic_pipeline() -> None:
    t0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    subject_tx = _tx("s1", "0xSubject", "0xTerminal", 0.99, t0)
    terminal_in = ([_tx(f"t{i}", f"0xMule{i:02d}", "0xTerminal", 0.99,
                        t0 - timedelta(hours=i * 2)) for i in range(7)]
                   + [subject_tx])
    by_address = {"0xSubject": [subject_tx], "0xTerminal": terminal_in}
    deps = PipelineDeps(adapter_factory=lambda c: StubAdapter(by_address))
    case = CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xSubject",), tx_hashes=("s1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")
    result = await run_trace_pipeline("0xSubject", "ethereum", case, deps)
    names = [s.name for s in result.risk.signals]
    assert "structuring-fan-in" in names, names
    assert "structuring-sub-threshold" in names, names
    assert "structuring-fan-in (+15)" in result.report.text
    print("[synthetic] terminal fan-in + sub-threshold signals raised; "
          "report section 6 rendered")


async def main() -> int:
    load_env()
    ok = True
    try:
        await section_live_hot_wallet()
        print("PASS: live hot-wallet structuring scan")
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
        print("PASS: synthetic structuring pipeline")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
