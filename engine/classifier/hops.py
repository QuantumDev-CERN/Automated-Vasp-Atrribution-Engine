"""Hop classification (master plan 4.4).

Tags every transfer edge with one of the plan's hop kinds:
  direct-transfer | peel | sweep-candidate   (M3 — implemented)
  dex-swap | bridge-lock | swap-service | mixer-deposit   (M4 — enum only)

Heuristics are deliberately conservative and always carry a reason string.
A wrong peel call sends the whole trace down the wrong branch, so when in
doubt we emit DIRECT_TRANSFER.

Peel (UTXO chains): 1 input, 2 outputs, one output much larger than the
other (value asymmetry) AND the large output is fresh (never seen elsewhere
in the observed graph). The large-output edge is the change branch; the
small-output edge is the peeled payment.

Sweep (any chain): many distinct inputs (>= SWEEP_MIN_INPUTS), 1-2 outputs.
The largest output is the consolidation wallet — typically a VASP hot
wallet sweeping per-user deposit addresses. The classic CEX deposit-sweep
pattern from plan section 2.

"Fresh" is relative to the observed (case-scoped) graph, not the chain —
documented limitation, consistent with plan 4.15 subgraph materialization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..adapters.base import CanonicalTx, Chain
from ..graph.builder import TxGraph, _COINBASE

UTXO_CHAINS = {Chain.BITCOIN.value}

SWEEP_MIN_INPUTS = 5      # distinct input addresses
SWEEP_MAX_OUTPUTS = 2
SWEEP_CHANGE_SHARE = 0.05  # smaller output below this share of total = change

PEEL_MAX_INPUTS = 1
PEEL_OUTPUTS = 2
PEEL_MIN_ASYMMETRY = 3.0   # large/small output value ratio
PEEL_MIN_CHANGE_SHARE = 0.6  # change output's share of total outputs


class HopKind(str, Enum):
    DIRECT_TRANSFER = "direct-transfer"
    PEEL = "peel"
    SWEEP_CANDIDATE = "sweep-candidate"
    # M4 kinds — defined now so the 4.4 contract is complete; not yet emitted.
    DEX_SWAP = "dex-swap"
    BRIDGE_LOCK = "bridge-lock"
    SWAP_SERVICE = "swap-service"
    MIXER_DEPOSIT = "mixer-deposit"


@dataclass
class HopClassification:
    kind: HopKind
    confidence: float  # 0..1
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def _real_inputs(tx: CanonicalTx) -> list:
    return [p for p in tx.inputs if p.address != _COINBASE]


def _values(outputs: list) -> list[int]:
    return [int(p.value) for p in outputs]


def classify_edge(
    graph: TxGraph, src: str, dst: str, key: str
) -> HopClassification:
    """Classify one transfer edge, using its tx + the surrounding graph."""
    edge = graph.g[src][dst][key]
    tx = graph.txs.get(edge["tx_hash"])
    if tx is None:  # should not happen; defensive
        return HopClassification(
            HopKind.DIRECT_TRANSFER, 0.5, "no source tx in graph", {}
        )

    sweep = _classify_sweep(tx, dst)
    if sweep is not None:
        return sweep
    peel = _classify_peel(graph, tx, dst)
    if peel is not None:
        return peel
    return HopClassification(
        HopKind.DIRECT_TRANSFER,
        0.5,
        "default: no peel/sweep pattern matched",
        {"tx_hash": tx.tx_hash},
    )


def _classify_sweep(tx: CanonicalTx, dst: str) -> Optional[HopClassification]:
    inputs = _real_inputs(tx)
    distinct_in = {p.address for p in inputs}
    if len(distinct_in) < SWEEP_MIN_INPUTS or len(tx.outputs) > SWEEP_MAX_OUTPUTS:
        return None
    outs = sorted(tx.outputs, key=lambda p: int(p.value), reverse=True)
    total = sum(_values(tx.outputs)) or 1
    consolidation = outs[0]
    share = int(consolidation.value) / total

    if dst == consolidation.address:
        conf = min(0.95, 0.75 + 0.02 * len(distinct_in))
        return HopClassification(
            HopKind.SWEEP_CANDIDATE,
            conf,
            f"sweep: {len(distinct_in)} inputs -> {len(tx.outputs)} output(s); "
            f"{dst[:12]} takes {share:.0%} of value",
            {
                "tx_hash": tx.tx_hash,
                "n_inputs": len(distinct_in),
                "n_outputs": len(tx.outputs),
                "consolidation_share": round(share, 3),
            },
        )
    # second output of a sweep tx: change if tiny, else plain direct
    small_share = int(tx.outputs[-1].value) / total if len(outs) > 1 else 0
    if small_share < SWEEP_CHANGE_SHARE:
        return HopClassification(
            HopKind.DIRECT_TRANSFER,
            0.7,
            f"sweep change output ({small_share:.1%} of value)",
            {"tx_hash": tx.tx_hash, "sweep_change": True},
        )
    return HopClassification(
        HopKind.DIRECT_TRANSFER,
        0.5,
        "sweep tx but not the consolidation output",
        {"tx_hash": tx.tx_hash},
    )


def _classify_peel(
    graph: TxGraph, tx: CanonicalTx, dst: str
) -> Optional[HopClassification]:
    if tx.chain.value not in UTXO_CHAINS:
        return None
    inputs = _real_inputs(tx)
    if len(inputs) != PEEL_MAX_INPUTS or len(tx.outputs) != PEEL_OUTPUTS:
        return None
    outs = sorted(tx.outputs, key=lambda p: int(p.value), reverse=True)
    large, small = outs[0], outs[1]
    large_v, small_v = int(large.value), int(small.value)
    total = large_v + small_v
    if total == 0:
        return None
    ratio = large_v / small_v if small_v else float("inf")
    share = large_v / total
    fresh = graph.g.in_degree(large.address) <= 1  # only this tx, in this graph

    if dst == large.address:
        if ratio >= PEEL_MIN_ASYMMETRY and share >= PEEL_MIN_CHANGE_SHARE and fresh:
            conf = min(0.95, 0.55 + 0.4 * share)
            return HopClassification(
                HopKind.PEEL,
                conf,
                f"peel change: 1-in/2-out, {ratio:.1f}x asymmetry, "
                f"{share:.0%} to fresh address {dst[:12]}",
                {
                    "tx_hash": tx.tx_hash,
                    "asymmetry_ratio": round(ratio, 2),
                    "change_share": round(share, 3),
                },
            )
        return None  # large output but not peel-shaped -> falls to direct
    # small output of a 1-in/2-out UTXO tx: the peeled payment
    if ratio >= PEEL_MIN_ASYMMETRY and share >= PEEL_MIN_CHANGE_SHARE and fresh:
        return HopClassification(
            HopKind.DIRECT_TRANSFER,
            0.65,
            f"peeled payment ({share:.0%} went to change)",
            {"tx_hash": tx.tx_hash, "peel_payment": True},
        )
    return None


def classify_graph(graph: TxGraph) -> dict[tuple[str, str, str], HopClassification]:
    """Classify every edge in the graph, annotating edges in place.

    Returns {(src, dst, key): HopClassification}.
    """
    out: dict[tuple[str, str, str], HopClassification] = {}
    for src, dst, key in graph.g.edges(keys=True):
        c = classify_edge(graph, src, dst, key)
        graph.g[src][dst][key]["hop_kind"] = c.kind.value
        graph.g[src][dst][key]["hop_confidence"] = c.confidence
        graph.g[src][dst][key]["hop_reason"] = c.reason
        graph.g[src][dst][key]["peel_payment"] = bool(
            c.details.get("peel_payment")
        )
        out[(src, dst, key)] = c
    return out
