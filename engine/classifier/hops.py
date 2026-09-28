"""Hop classification (master plan 4.4).

Tags every transfer edge with one of the plan's hop kinds:
  direct-transfer | peel | sweep-candidate   (M3)
  dex-swap | bridge-lock | mixer-deposit     (M4)
  swap-service                            (M18)

Order of checks: explicit DEX annotation first, then deterministic
registry hits (bridge/mixer/swap-service contracts), then the M3
heuristics. A registry hit or a decoded swap always wins over a
heuristic guess.

Heuristics are deliberately conservative and always carry a reason string.
A wrong peel call sends the whole trace down the wrong branch, so when in
doubt we emit DIRECT_TRANSFER.

Peel (UTXO chains): 1 input, 2 outputs, one output much larger than the
other (value asymmetry) AND the large output is fresh (never seen elsewhere
in the observed graph) AND — when the adapter knows both script types —
the large output's script type matches the input's (a wallet generates
change with the same script type as its inputs). A known mismatch vetoes
the peel call: when in doubt we emit DIRECT_TRANSFER.

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
from ..clustering.coinjoin import detect_coinjoin
from ..decoding.bridges import parse_bridge_destination
from ..graph.builder import TxGraph, _COINBASE
from ..knowledge.bridges import bridge_for
from ..knowledge.mixers import mixer_for
from ..knowledge.swap_services import swap_service_for

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
    DEX_SWAP = "dex-swap"
    BRIDGE_LOCK = "bridge-lock"
    MIXER_DEPOSIT = "mixer-deposit"
    # swap-service — detected via the curated M18 hot-wallet registry.
    SWAP_SERVICE = "swap-service"
    # coinjoin — structurally detected collaborative CoinJoin (M20, using
    # the M17 detector). Traversal stops: no deterministic unmixing.
    COINJOIN = "coinjoin"


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

    m4 = _classify_m4(tx, src, dst)
    if m4 is not None:
        return m4
    coinjoin = _classify_coinjoin(tx)
    if coinjoin is not None:
        return coinjoin
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


def _classify_m4(
    tx: CanonicalTx, src: str, dst: str
) -> Optional[HopClassification]:
    """Deterministic M4/M18 kinds: decoded swaps and registry hits.

    Runs before the M3 heuristics — an explicit decode or a known-contract
    counterparty always wins over a heuristic guess.
    """
    # DEX swap legs: any edge touching the trader in a decoded swap tx.
    # Both legs (trader->router, router->trader) are the same economic hop:
    # asset A became asset B at this trader.
    s = tx.dex_swap
    if s is not None and (src == s.trader or dst == s.trader):
        in_sym = s.in_symbol or s.in_contract or "?"
        out_sym = s.out_symbol or s.out_contract or "?"
        return HopClassification(
            HopKind.DEX_SWAP,
            s.confidence,
            f"dex-swap via {s.dex or 'unknown dex'}: "
            f"{in_sym} -> {out_sym} ({s.method})",
            {
                "tx_hash": tx.tx_hash,
                "dex": s.dex,
                "router": s.router,
                "in_symbol": s.in_symbol,
                "in_contract": s.in_contract,
                "in_value": s.in_value,
                "out_symbol": s.out_symbol,
                "out_contract": s.out_contract,
                "out_value": s.out_value,
                "method": s.method,
            },
        )

    # Bridge: counterparty is a known bridge contract.
    for addr, direction in ((dst, "lock"), (src, "release")):
        b = bridge_for(tx.chain, addr)
        if b is not None:
            details: dict[str, Any] = {
                "tx_hash": tx.tx_hash,
                "bridge": b.name,
                "direction": direction,
                "contract": b.contract,
            }
            reason = (f"bridge {direction} via {b.name} "
                      f"({addr[:12]}… on {tx.chain.value})")
            if direction == "lock":
                # M21: explicit destination from the lock calldata when
                # the bridge's ABI is decodable (Wormhole transferTokens).
                dest = parse_bridge_destination(tx)
                if dest is not None and dest.dest_address:
                    details["dest_chain"] = dest.dest_chain
                    details["dest_address"] = dest.dest_address
                    hop_to = (f"{dest.dest_chain}:{dest.dest_address[:12]}…"
                              if dest.dest_chain else
                              f"chain-id-{dest.wormhole_chain_id}:"
                              f"{dest.dest_address[:12]}…")
                    reason += f" → {hop_to}"
            return HopClassification(
                HopKind.BRIDGE_LOCK,
                0.9,
                reason,
                details,
            )

    # Mixer: funds sent INTO a known pool = entering the anonymity set.
    m = mixer_for(tx.chain, dst)
    if m is not None:
        return HopClassification(
            HopKind.MIXER_DEPOSIT,
            0.95,
            f"mixer deposit: {m.name} {m.denomination} pool "
            f"({dst[:12]}… on {tx.chain.value})",
            {
                "tx_hash": tx.tx_hash,
                "mixer": m.name,
                "denomination": m.denomination,
                "pool": m.pool,
            },
        )

    # Swap service: funds sent INTO a known service address = entering
    # a custodial swap. Like the mixer check, only the deposit direction
    # classifies — the service's own onward movements are out of scope.
    s = swap_service_for(tx.chain, dst)
    if s is not None:
        return HopClassification(
            HopKind.SWAP_SERVICE,
            s.confidence,
            f"swap-service deposit: {s.name} ({s.role}; {s.label}) "
            f"({dst[:12]}… on {tx.chain.value})",
            {
                "tx_hash": tx.tx_hash,
                "swap_service": s.name,
                "role": s.role,
                "label": s.label,
            },
        )
    return None


def _classify_coinjoin(tx: CanonicalTx) -> Optional[HopClassification]:
    """M20: structurally detected CoinJoin (Wasabi/Whirlpool/JoinMarket
    shape, via the M17 detector). Runs before the sweep/peel heuristics:
    a CoinJoin's many-input/many-output shape must never be misread as a
    custodial sweep (false VASP attribution) or a peel chain, and the
    walk must stop here — no deterministic unmixing, per the master plan.
    """
    evidence = detect_coinjoin(tx)
    if not evidence.is_coinjoin:
        return None
    return HopClassification(
        HopKind.COINJOIN,
        0.80,
        "coinjoin: " + "; ".join(evidence.reasons),
        {
            "tx_hash": tx.tx_hash,
            "n_inputs": evidence.input_count,
            "n_outputs": evidence.output_count,
            "max_equal_outputs": evidence.max_equal_outputs,
            "distinct_output_values": evidence.distinct_output_values,
        },
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
    shaped = (ratio >= PEEL_MIN_ASYMMETRY
              and share >= PEEL_MIN_CHANGE_SHARE
              and fresh)

    # Script-type match (master plan section 2): a wallet mints change with
    # the same script type as the spent input. Unknown on either side is
    # neutral (adapters may not provide it); a known mismatch vetoes peel.
    in_type = inputs[0].script_type
    out_type = large.script_type
    script_match = bool(in_type and out_type and in_type == out_type)
    if shaped and in_type and out_type and not script_match:
        return HopClassification(
            HopKind.DIRECT_TRANSFER,
            0.6,
            f"not peel-shaped: script-type mismatch "
            f"(input {in_type} vs change {out_type})",
            {
                "tx_hash": tx.tx_hash,
                "script_match": False,
                "input_script_type": in_type,
                "output_script_type": out_type,
            },
        )

    if dst == large.address:
        if shaped:
            conf = min(0.95, 0.55 + 0.4 * share + (0.05 if script_match else 0.0))
            reason = (f"peel change: 1-in/2-out, {ratio:.1f}x asymmetry, "
                      f"{share:.0%} to fresh address {dst[:12]}")
            if script_match:
                reason += f", script-type match ({in_type})"
            return HopClassification(
                HopKind.PEEL,
                conf,
                reason,
                {
                    "tx_hash": tx.tx_hash,
                    "asymmetry_ratio": round(ratio, 2),
                    "change_share": round(share, 3),
                    "script_match": script_match,
                    "script_type": in_type if script_match else None,
                },
            )
        return None  # large output but not peel-shaped -> falls to direct
    # small output of a 1-in/2-out UTXO tx: the peeled payment
    if shaped:
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
        # M4/M18 details ride on the edge so traversal/reporting can use
        # them without re-deriving (bridge name/direction, mixer, dex
        # in/out, swap-service name/role).
        for k in ("bridge", "direction", "mixer", "denomination",
                  "dex", "router", "in_symbol", "out_symbol",
                  "swap_service", "role", "dest_chain", "dest_address"):
            if c.details.get(k) is not None:
                graph.g[src][dst][key][f"hop_{k}"] = c.details[k]
        out[(src, dst, key)] = c
    return out
