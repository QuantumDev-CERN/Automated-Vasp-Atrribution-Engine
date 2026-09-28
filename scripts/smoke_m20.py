"""M20 live smoke: CoinJoin as a traversal terminal.

Three parts:

1. Live BTC section (bounded): sample the tip block from mempool.space
   (public, no key — same pattern as the M17 smoke) and run the
   structural detector over real transactions. Negative control: trivial
   txs (< 5 outputs) are never flagged. Positive control when the block
   happens to contain one: a real flagged CoinJoin must classify as
   HopKind.COINJOIN through the production classifier — exercising the
   M20 wiring on real chain data.

2. Synthetic CoinJoin pipeline (offline, deterministic): a scripted
   adapter feeds a Wasabi-shaped round. The trace must stop with a
   "coinjoin" terminal, render the M20 report label, add the +40 risk
   signal, and NOT walk into the onward spend (no deterministic
   unmixing).

3. Mixer-deposit regression (offline): a synthetic deposit into a real
   Tornado pool address. Before the M20 traversal fix this crashed the
   pipeline with a KeyError in path reconstruction (latent since M4 —
   no pipeline test had ever exercised a non-dead-end terminal); it
   must now complete with a "mixer-deposit" terminal.

Transient indexer/RPC errors SKIP the live section instead of failing
the gate.

Run: uv run python scripts/smoke_m20.py
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.classifier.hops import HopKind, classify_graph
from engine.clustering import detect_coinjoin
from engine.graph.builder import TxGraph
from engine.jobs import PipelineDeps, run_trace_pipeline
from engine.vasp import CaseDetails

_BTC = Asset(kind=AssetKind.NATIVE, chain=Chain.BITCOIN, symbol="BTC",
             decimals=8)
_ETH = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
_MEMPOOL = "https://mempool.space/api"
_MAX_TXS = 25
# Tornado Cash 0.1 ETH pool — on-chain verified in M4, curated registry.
_TORNADO_01 = "0x12D66f87A04A9E220743712cE6d9bB1B5616B8Fc"


class SectionSkip(Exception):
    pass


def _transient(message: str) -> bool:
    m = message.lower()
    return any(
        h in m
        for h in ("timeout", "timed out", "connecterror", "connection reset",
                  "temporarily unavailable", "rate limit", "429", "502",
                  "503", "504", "max retries")
    )


def _client() -> httpx.AsyncClient:
    # Same egress posture as engine/adapters/base.py: this environment must
    # go through the proxy, and NO_PROXY's "[::1]" entry crashes httpx.
    ctx = ssl.create_default_context(
        cafile=os.environ.get("SSL_CERT_FILE") or None
    )
    return httpx.AsyncClient(
        timeout=30.0,
        trust_env=False,
        proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        verify=ctx,
    )


def _to_canonical(txj: dict) -> CanonicalTx | None:
    inputs: list[FlowParty] = []
    for vin in txj.get("vin", []):
        if vin.get("is_coinbase"):
            continue
        prev = vin.get("prevout") or {}
        addr = prev.get("scriptpubkey_address")
        if not addr:
            continue
        inputs.append(FlowParty(address=addr, value=str(prev.get("value", 0))))
    outputs: list[FlowParty] = []
    for vout in txj.get("vout", []):
        addr = vout.get("scriptpubkey_address")
        if not addr:
            continue  # OP_RETURN / unparseable: no value to group
        outputs.append(FlowParty(address=addr,
                                 value=str(vout.get("value", 0))))
    if not inputs and not outputs:
        return None
    return CanonicalTx(
        tx_hash=txj["txid"], chain=Chain.BITCOIN, block_time=None,
        inputs=inputs, outputs=outputs, asset=_BTC)


async def section_live() -> None:
    """Detector + classifier against real BTC tip-block transactions."""
    async with _client() as c:
        try:
            height = (await c.get(f"{_MEMPOOL}/blocks/tip/height")).json()
            bhash = (await c.get(f"{_MEMPOOL}/block-height/{height}")).text
            txids: list[str] = (
                await c.get(f"{_MEMPOOL}/block/{bhash}/txids")).json()
        except httpx.HTTPError as e:
            if _transient(str(e)):
                raise SectionSkip(f"m20: mempool.space transient: {e}")
            raise
        print(f"[m20] tip block {height}, sampling {_MAX_TXS} txs")
        txs: list[CanonicalTx] = []
        for txid in txids[1:_MAX_TXS + 1]:  # [0] is coinbase
            try:
                txj = (await c.get(f"{_MEMPOOL}/tx/{txid}")).json()
            except httpx.HTTPError as e:
                if _transient(str(e)):
                    raise SectionSkip(f"m20: mempool.space transient: {e}")
                raise
            ctx_ = _to_canonical(txj)
            if ctx_ is not None:
                txs.append(ctx_)
    print(f"[m20] live: canonical txs: {len(txs)}")

    # negative control: trivial txs can never trip the default thresholds
    trivial = [t for t in txs if len(t.outputs) < 5]
    assert all(not detect_coinjoin(t).is_coinjoin for t in trivial), \
        "false positive on trivial tx"
    print(f"[m20] live: trivial txs (< 5 outputs), none flagged: "
          f"{len(trivial)}")

    # positive control when the block contains a real CoinJoin: the M20
    # classifier must label its hops COINJOIN (never peel/sweep).
    flagged = [t for t in txs if detect_coinjoin(t).is_coinjoin]
    print(f"[m20] live: coinjoin-flagged: {len(flagged)}")
    if flagged:
        g = TxGraph.build(txs)
        out = classify_graph(g)
        for t in flagged:
            kinds = {c.kind for (s, d, k), c in out.items()
                     if k.startswith(t.tx_hash + ":")}
            assert kinds == {HopKind.COINJOIN}, \
                f"{t.tx_hash[:12]}… classified as {kinds}, want COINJOIN"
            assert HopKind.SWEEP_CANDIDATE not in kinds
            assert HopKind.PEEL not in kinds
        print(f"[m20] live: all {len(flagged)} real CoinJoins classified "
              f"COINJOIN through the production classifier")
    else:
        print("[m20] live: no CoinJoin in sample — positive control skipped")


class ScriptedAdapter(ChainAdapter):
    def __init__(self, chain, history):
        super().__init__()
        self.chain = chain
        self._history = history

    async def get_transactions(self, address, limit=100):
        return self._history.get(address, [])[:limit]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


def _btc_tx(tx_hash, inputs, outputs):
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.BITCOIN, asset=_BTC,
        inputs=[FlowParty(address=a, value=str(v)) for a, v in inputs],
        outputs=[FlowParty(address=a, value=str(v)) for a, v in outputs])


def _case(cid, wallets, tx_hashes):
    return CaseDetails(
        case_id=cid, agency="Cyber Cell", officer="Insp. X",
        wallets=wallets, tx_hashes=tx_hashes,
        date_from="2026-01-01", date_to="2026-09-28",
        suspected_offence="smoke-m20")


async def section_synthetic_coinjoin() -> None:
    """Deterministic pipeline: Wasabi-shaped round stops the trace."""
    inputs = [(f"in{i}", 11_000_000) for i in range(12)]
    outputs = ([(f"out{i}", 10_000_000) for i in range(7)]
               + [("chg0", 5_000_000), ("chg1", 3_000_000)])
    cj = _btc_tx("cj1", inputs, outputs)
    spend = _btc_tx("spend1", [("out0", 10_000_000)],
                    [("final", 9_900_000)])
    history: dict[str, list[CanonicalTx]] = {}
    for t in (cj, spend):
        for p in t.inputs + t.outputs:
            history.setdefault(p.address, []).append(t)
    deps = PipelineDeps(
        adapter_factory=lambda c: ScriptedAdapter(Chain.BITCOIN, history))
    result = await run_trace_pipeline(
        "in0", "bitcoin", _case("FIR/2026/002001", ("in0",), ("cj1",)),
        deps)
    assert result.terminal_reason == "coinjoin", result.terminal_reason
    assert any(s.name == "coinjoin" and s.points == 40
               for s in result.risk.signals)
    assert "CoinJoin — funds entered a collaborative anonymity set" \
        in result.report.text
    assert not any(n.address == "final" for n in result.path), \
        "walked past the CoinJoin — deterministic unmixing claimed"
    print("[m20] synthetic: coinjoin terminal, +40 risk, report label, "
          "no unmixing — OK")


async def section_mixer_regression() -> None:
    """The M4-latent KeyError: a mixer-deposit terminal used to crash
    path reconstruction. Must now complete."""
    dep = CanonicalTx(
        tx_hash="mx1", chain=Chain.ETHEREUM, asset=_ETH,
        inputs=[FlowParty(address="0xsubject",
                          value="100000000000000000")],
        outputs=[FlowParty(address=_TORNADO_01,
                           value="100000000000000000")])
    history = {"0xsubject": [dep], _TORNADO_01: [dep]}
    deps = PipelineDeps(
        adapter_factory=lambda c: ScriptedAdapter(Chain.ETHEREUM, history))
    result = await run_trace_pipeline(
        "0xsubject", "ethereum",
        _case("FIR/2026/002002", ("0xsubject",), ("mx1",)), deps)
    assert result.terminal_reason == "mixer-deposit", result.terminal_reason
    assert result.path[-1].address == _TORNADO_01
    assert any(s.name == "mixer-deposit" for s in result.risk.signals)
    print("[m20] regression: mixer-deposit terminal completes, path ends "
          "at the pool — OK")


async def main() -> None:
    try:
        await section_live()
    except SectionSkip as s:
        print(f"[m20] SKIP live section: {s}")
    await section_synthetic_coinjoin()
    await section_mixer_regression()
    print("smoke_m20 OK")


if __name__ == "__main__":
    asyncio.run(main())
