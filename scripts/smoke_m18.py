"""M18 live smoke: swap-service hot-wallet registry re-verification.

Two parts:

1. Registry re-verification (live, bounded): every curated address is
   re-checked on-chain — eth_getCode == 0x (EOA) and a transaction count
   is fetched. This catches label rot, key changes, and typos. It does
   NOT re-prove ownership: the explorer public label cited in the
   registry is the attribution claim; the chain check proves the
   address exists, is an EOA, and is (or was) active.

2. Synthetic end-to-end (offline, deterministic): a deposit edge into a
   registry address classifies as swap-service, traversal stops with a
   "swap-service" terminal, the SwapDeposit is recorded, and the risk
   scorer raises the +25 signal.

The smoke never asserts on live *traffic* hitting the registry — a
quiet sample is not a failure. Transient indexer/RPC errors SKIP the
live section instead of failing the gate.

Run: uv run python scripts/smoke_m18.py
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import Asset, AssetKind, CanonicalTx, Chain, FlowParty
from engine.classifier.hops import HopKind
from engine.graph.builder import TxGraph
from engine.knowledge.swap_services import SWAP_SERVICE_ADDRESSES
from engine.scoring.risk import score_risk
from engine.traversal.engine import traverse

_V2 = "https://api.etherscan.io/v2/api"
_V2_CHAIN = {"ethereum": 1}
_BSC_RPC = "https://bsc-dataseed.binance.org/"


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


def _client() -> httpx.AsyncClient:
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    return httpx.AsyncClient(
        timeout=60.0,
        trust_env=False,
        proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        verify=ctx,
    )


async def _v2(c: httpx.AsyncClient, chainid: int, action: str, addr: str) -> str:
    r = await c.get(_V2, params={
        "chainid": chainid, "module": "proxy", "action": action,
        "address": addr, "tag": "latest",
        "apikey": os.environ.get("ETHERSCAN_API_KEY", ""),
    })
    j = r.json()
    res = j.get("result")
    if not isinstance(res, str):
        raise SectionSkip(f"etherscan v2 {action} for {addr[:12]}…: {res!r}"[:100])
    return res


async def _rpc(c: httpx.AsyncClient, method: str, addr: str) -> str:
    r = await c.post(_BSC_RPC, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": [addr, "latest"]})
    res = r.json().get("result")
    if not isinstance(res, str):
        raise SectionSkip(f"bsc rpc {method} for {addr[:12]}…: {res!r}"[:100])
    return res


async def section_registry_reverify() -> None:
    """Every curated address: still an EOA, still has chain activity."""
    if not os.environ.get("ETHERSCAN_API_KEY"):
        raise SectionSkip("m18: ETHERSCAN_API_KEY missing from .env")
    async with _client() as c:
        n = 0
        for (chain, addr), info in sorted(SWAP_SERVICE_ADDRESSES.items()):
            try:
                if chain == "ethereum":
                    code = await _v2(c, 1, "eth_getCode", addr)
                    await asyncio.sleep(1.2)  # stay under the free-tier rate cap
                    txc = await _v2(c, 1, "eth_getTransactionCount", addr)
                    await asyncio.sleep(1.2)
                elif chain == "bsc":
                    code = await _rpc(c, "eth_getCode", addr)
                    txc = await _rpc(c, "eth_getTransactionCount", addr)
                    await asyncio.sleep(0.5)
                else:
                    raise SectionSkip(f"m18: no live checker for chain {chain}")
            except SectionSkip:
                raise
            except Exception as e:
                if _transient(str(e) + type(e).__name__):
                    raise SectionSkip(f"m18: transient probing {info.name} "
                                      f"{addr[:12]}…: {type(e).__name__}")
                raise
            assert code == "0x", (
                f"m18: {info.name} {addr} now has code — no longer an EOA?!")
            ntx = int(txc, 16)
            # The ChangeNOW 18 EOA is receive-only on Ethereum by design.
            if not (chain == "ethereum" and
                    addr == "0xe2d60cfe3cf8b2079c7df0144c5b28c03469775c"):
                assert ntx > 0, (
                    f"m18: {info.name} {addr} shows zero outbound txs")
            print(f"  ok: {info.name:10s} {chain:8s} {info.role:10s} "
                  f"{addr[:12]}… txcount={ntx}")
            n += 1
    print(f"[m18] re-verified {n} registry addresses on-chain")


def section_synthetic_end_to_end() -> None:
    """Deposit -> registry address: classify, traverse, score."""
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM,
                  symbol="ETH", decimals=18)
    user = "0xSmokeUser00000000000000000000000000000001"
    svc = "0xA96Be652A08D9905F15B7FbE2255708709BeCD09"  # ChangeNOW HW2
    tx = CanonicalTx(
        tx_hash="0xsmoke18", chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=user, value=str(10**18))],
        outputs=[FlowParty(address=svc, value=str(10**18))],
    )
    g = TxGraph.build([tx])
    r = traverse(g, user)
    # terminal hops are not walked past, so the kind shows on the edge /
    # terminal, not in visited
    assert any(t.reason == "swap-service" for t in r.terminals)
    assert not any(t.reason.startswith("unhandled-hop") for t in r.terminals)
    assert len(r.swap_deposits) == 1
    d = r.swap_deposits[0]
    assert d.service == "changenow" and d.address == user
    risk = score_risk(r.visited, terminal_reason="swap-service")
    assert any(s.name == "swap-service" and s.points == 25
               for s in risk.signals)
    print("[m18] synthetic deposit -> changenow: terminal + SwapDeposit + "
          "risk signal all OK")


async def main() -> None:
    load_env()
    section_synthetic_end_to_end()  # offline, always runs
    try:
        await section_registry_reverify()
    except SectionSkip as e:
        print(f"SKIP: {e}")
    print("[m18] smoke done")


if __name__ == "__main__":
    asyncio.run(main())
