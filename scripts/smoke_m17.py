"""M17 live smoke: CoinJoin guard + co-input clustering on real BTC data.

Bounded on purpose: takes the tip block, fetches up to 25 full transactions
from mempool.space (public, no key), and runs the M17 pipeline over them:

  - detect_coinjoin over every tx (structural, no curated addresses)
  - cluster_coinput_transactions over the set
  - determinism: clustering twice yields identical results
  - guard effectiveness: inputs of any flagged tx appear in NO cluster
  - no false positives on trivial txs (< 5 outputs can never trip the
    default thresholds — asserted as a structural invariant)

A CoinJoin-shaped tx may or may not appear in a 25-tx sample; the smoke
does not assert finding one (that would be flaky). The positive control
lives in tests/test_m17.py.

Run: uv run python scripts/smoke_m17.py
"""
import asyncio
import os
import ssl
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.adapters.base import Asset, AssetKind, CanonicalTx, Chain, FlowParty
from engine.clustering import cluster_coinput_transactions, detect_coinjoin
from engine.graph import TxGraph

_BTC = Asset(kind=AssetKind.NATIVE, chain=Chain.BITCOIN, symbol="BTC", decimals=8)
_MEMPOOL = "https://mempool.space/api"
_MAX_TXS = 25


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
        outputs.append(FlowParty(address=addr, value=str(vout.get("value", 0))))
    if not inputs and not outputs:
        return None
    return CanonicalTx(
        tx_hash=txj["txid"],
        chain=Chain.BITCOIN,
        block_time=None,
        inputs=inputs,
        outputs=outputs,
        asset=_BTC,
    )


async def _smoke() -> None:
    async with _client() as c:
        height = (await c.get(f"{_MEMPOOL}/blocks/tip/height")).json()
        bhash = (await c.get(f"{_MEMPOOL}/block-height/{height}")).text
        txids: list[str] = (
            await c.get(f"{_MEMPOOL}/block/{bhash}/txids")
        ).json()
        print(f"[m17] tip block {height}, {len(txids)} txs; sampling {_MAX_TXS}")

        txs: list[CanonicalTx] = []
        for txid in txids[1:_MAX_TXS + 1]:  # [0] is coinbase
            txj = (await c.get(f"{_MEMPOOL}/tx/{txid}")).json()
            ctx_ = _to_canonical(txj)
            if ctx_ is not None:
                txs.append(ctx_)
        print(f"[m17] canonical txs: {len(txs)}")

    # --- detection over real txs
    flagged = [t for t in txs if detect_coinjoin(t).is_coinjoin]
    print(f"[m17] coinjoin-flagged: {len(flagged)}")
    for t in flagged[:5]:
        ev = detect_coinjoin(t)
        print(f"[m17]   {t.tx_hash[:12]}… in={ev.input_count} "
              f"out={ev.output_count} maxeq={ev.max_equal_outputs} "
              f"reasons={ev.reasons[0][:60]}…")

    # structural invariant: < 5 outputs can never trip default thresholds
    trivial = [t for t in txs if len(t.outputs) < 5]
    assert all(not detect_coinjoin(t).is_coinjoin for t in trivial), \
        "false positive on trivial tx"
    print(f"[m17] trivial txs (< 5 outputs), none flagged: {len(trivial)}")

    # --- clustering + determinism + guard effectiveness
    r1 = cluster_coinput_transactions(txs)
    r2 = cluster_coinput_transactions(txs)
    assert r1.clusters == r2.clusters, "clustering not deterministic"
    assert r1.members.keys() == r2.members.keys()
    for ev in r1.excluded:
        tx = next(t for t in txs if t.tx_hash == ev.tx_hash)
        for p in tx.inputs:
            assert p.address not in r1.clusters, \
                f"guard leak: {p.address[:12]}… merged despite CoinJoin flag"
    sizes = sorted((len(m) for m in r1.members.values()), reverse=True)
    print(f"[m17] clusters: {len(r1.members)}, "
          f"largest={sizes[0] if sizes else 0}, "
          f"txs_merged={r1.txs_merged}/{r1.txs_seen}")

    # --- graph annotation seam on the same live data
    g = TxGraph.build(txs)
    n = g.annotate_clusters(r1)
    assert n == len(r1.clusters)
    print(f"[m17] graph nodes annotated with cluster_id: {n}")


async def main() -> None:
    try:
        await _smoke()
    except httpx.HTTPError as e:
        msg = str(e).lower()
        if any(h in msg for h in ("429", "502", "503", "504", "timeout",
                                  "timed out", "connecterror",
                                  "connection reset", "temporarily unavailable")):
            print(f"[m17] SKIP — mempool.space throttled/unreachable: {e}")
            return
        raise
    print("\nSMOKE M17 OK — CoinJoin guard + co-input clustering on live BTC.")


if __name__ == "__main__":
    asyncio.run(main())
