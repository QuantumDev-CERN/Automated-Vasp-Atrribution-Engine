"""M21 live smoke: bridge calldata destination parsing + continuation.

Two parts:

1. Live section (needs ETHERSCAN_API_KEY): pull the recent txlist of the
   Wormhole TokenBridge contract on Ethereum, find a real transferTokens
   lock, run it through EvmAdapter._normalize_native (exercising the M21
   full-calldata retention on real data), and decode the explicit
   destination with parse_bridge_destination. Negative control: a
   non-transferTokens call to the same contract (e.g. completeTransfer)
   must decode to None.

2. Synthetic continuation (offline, deterministic): a scripted
   two-chain pipeline — an Ethereum lock with Wormhole calldata naming
   a BSC recipient, then a BSC-side trace from that recipient. The
   primary trace must keep its bridge-lock terminal while recording the
   cross-chain continuation.

Transient indexer errors SKIP the live section instead of failing
the gate.

Run: uv run python scripts/smoke_m21.py
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Crypto.Hash import keccak

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.adapters.evm import EvmAdapter
from engine.decoding.bridges import parse_bridge_destination
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.vasp import CaseDetails

_V2 = "https://api.etherscan.io/v2/api"
WORMHOLE_ETH = "0x3ee18B2214AFF97000D974cf647E7C347E8fa585"

_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
_BNB = Asset(kind=AssetKind.NATIVE, chain=Chain.BSC, symbol="BNB")


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


def _selector() -> str:
    k = keccak.new(digest_bits=256)
    k.update(b"transferTokens(address,uint256,uint16,bytes32,uint256,uint32)")
    return "0x" + k.digest()[:4].hex()


async def section_live_decode() -> None:
    """Real transferTokens lock -> explicit destination."""
    if not os.environ.get("ETHERSCAN_API_KEY"):
        raise SectionSkip("m21: ETHERSCAN_API_KEY missing from .env")
    ctx = ssl.create_default_context(
        cafile=os.environ.get("SSL_CERT_FILE") or None)
    async with httpx.AsyncClient(
            timeout=60.0, trust_env=False,
            proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
            verify=ctx) as c:
        r = await c.get(_V2, params={
            "chainid": 1, "module": "account", "action": "txlist",
            "address": WORMHOLE_ETH, "startblock": 0, "endblock": 99999999,
            "page": 1, "offset": 50, "sort": "desc",
            "apikey": os.environ.get("ETHERSCAN_API_KEY", ""),
        })
        j = r.json()
        txs = j.get("result")
        if not isinstance(txs, list) or not txs:
            raise SectionSkip(f"m21: txlist for wormhole: {str(txs)[:80]}")
        sel = _selector()
        lock = next((t for t in txs
                     if (t.get("input") or "").lower().startswith(sel)), None)
        if lock is None:
            raise SectionSkip("m21: no transferTokens in the last 50 "
                              "wormhole txs (live-data drift)")
        adapter = EvmAdapter(Chain.ETHEREUM)
        canon = adapter._normalize_native(lock)
        # M21 retention: the full calldata must have survived normalization
        # because the counterparty is a known bridge.
        assert canon.raw["input"] == (lock.get("input") or ""), \
            "bridge tx calldata was truncated"
        dest = parse_bridge_destination(canon)
        assert dest is not None, "real transferTokens failed to decode"
        print(f"[live] wormhole lock {canon.tx_hash[:12]}… -> "
              f"{dest.dest_chain}:{dest.dest_address[:12]}… "
              f"(wormhole chain id {dest.wormhole_chain_id})")
        # Negative control: another call into the same contract that is
        # NOT transferTokens must not decode.
        other = next((t for t in txs
                      if not (t.get("input") or "0x").lower()
                      .startswith(sel)
                      and len(t.get("input") or "") >= 10), None)
        if other is not None:
            neg = parse_bridge_destination(adapter._normalize_native(other))
            assert neg is None, "non-transferTokens call decoded (!)"
            print(f"[live] negative control ok: "
                  f"{(other.get('input') or '')[:10]}… -> None")


class StubAdapter(ChainAdapter):
    def __init__(self, chain, txs):
        super().__init__()
        self.chain = chain
        self._txs = txs

    async def get_transactions(self, address, limit=100):
        return [t for t in self._txs
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


def _word(b: bytes) -> bytes:
    return b.rjust(32, b"\x00")


def _wormhole_calldata(recipient_chain: int, recipient: bytes) -> str:
    k = keccak.new(digest_bits=256)
    k.update(b"transferTokens(address,uint256,uint16,bytes32,uint256,uint32)")
    sel = k.digest()[:4]
    token = _word(bytes.fromhex("c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"))
    amount = _word((10**18).to_bytes(32, "big")[-32:])
    chain_w = _word(recipient_chain.to_bytes(2, "big"))
    return "0x" + (sel + token + amount + chain_w + recipient
                   + _word(b"\x00") + _word((1).to_bytes(4, "big"))).hex()


async def section_synthetic_continuation() -> None:
    dest = "0x" + "ab" * 20
    lock = CanonicalTx(
        tx_hash="lock1", chain=Chain.ETHEREUM, asset=_ETH,
        inputs=[FlowParty(address="0xDepositor", value="0")],
        outputs=[FlowParty(address=WORMHOLE_ETH, value="0")],
        raw={"input": _wormhole_calldata(
            4, b"\x00" * 12 + bytes.fromhex(dest[2:]))})
    bsc_leg = CanonicalTx(
        tx_hash="b1", chain=Chain.BSC, asset=_BNB,
        inputs=[FlowParty(address=dest.lower(), value="100")],
        outputs=[FlowParty(address="0xFinal", value="100")])
    stubs = {"ethereum": StubAdapter(Chain.ETHEREUM, [lock]),
             "bsc": StubAdapter(Chain.BSC, [bsc_leg])}
    deps = PipelineDeps(adapter_factory=lambda c: stubs[c])
    case = CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xDepositor",), tx_hashes=("lock1",),
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="test")
    result = await run_trace_pipeline("0xDepositor", "ethereum", case, deps)
    assert result.terminal_reason == "bridge-lock"
    assert len(result.cross_chain) == 1
    cont = result.cross_chain[0]
    assert cont.dest_chain == "bsc" and cont.terminal_reason == "dead-end"
    assert "CROSS-CHAIN CONTINUATION" in result.report.text
    print(f"[synthetic] eth lock -> bsc:{dest[:12]}… -> "
          f"{cont.terminal_reason} ({cont.terminal_address[:12]}…)")


async def main() -> int:
    load_env()
    ok = True
    try:
        await section_live_decode()
        print("PASS: live wormhole destination decode")
    except SectionSkip as s:
        print(f"SKIP: {s}")
    except Exception as e:  # noqa: BLE001
        if _transient(str(e)):
            print(f"SKIP (transient): {e}")
        else:
            print(f"FAIL: {e}")
            ok = False
    try:
        await section_synthetic_continuation()
        print("PASS: synthetic two-chain continuation")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
