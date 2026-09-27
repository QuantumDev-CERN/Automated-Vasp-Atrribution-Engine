"""Traversal engine (master plan 4.5).

Breadth-first walk from a suspect address over the classified TxGraph,
applying a typology-specific strategy per hop kind:

  direct-transfer -> keep walking
  peel            -> follow the change branch first; the peeled (payment)
                     branch is queued separately as a side branch
  sweep-candidate -> STOP at the consolidation wallet: it is a likely
                     VASP-controlled hot wallet. Retroactively back-label
                     every input address of the sweep tx as "sweep-source"
                     (per-user deposit addresses of the same entity, plan 6).

Any M4 hop kind encountered (dex-swap, bridge-lock, ...) is treated as a
terminal "unhandled-hop" — traversal never silently walks through a hop
type it does not understand.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from ..classifier.hops import HopKind, classify_graph
from ..graph.builder import TxGraph, _COINBASE

_M4_KINDS = {
    HopKind.DEX_SWAP,
    HopKind.BRIDGE_LOCK,
    HopKind.SWAP_SERVICE,
    HopKind.MIXER_DEPOSIT,
}


@dataclass
class TraversalConfig:
    max_hops: int = 10
    max_nodes: int = 1000


@dataclass
class VisitedNode:
    address: str
    hop: int
    via_tx: Optional[str] = None
    via_kind: Optional[str] = None
    via_confidence: Optional[float] = None
    side_branch: bool = False  # peeled payment, not the change branch


@dataclass
class Terminal:
    address: str
    reason: str  # dead-end | sweep-consolidation | max-hops | max-nodes | unhandled-hop:<kind>


@dataclass
class TraversalResult:
    start: str
    visited: list[VisitedNode] = field(default_factory=list)
    terminals: list[Terminal] = field(default_factory=list)
    came_from: dict[str, tuple[str, str]] = field(default_factory=dict)
    # address -> parent address, tx_hash (path reconstruction for M6 reports)
    labels_applied: dict[str, list[str]] = field(default_factory=dict)


def _ensure_classified(graph: TxGraph) -> None:
    if graph.g.number_of_edges() == 0:
        return
    _s, _t, _k = next(iter(graph.g.edges(keys=True)))
    if "hop_kind" not in graph.g[_s][_t][_k]:
        classify_graph(graph)


def traverse(
    graph: TxGraph,
    start: str,
    config: TraversalConfig = TraversalConfig(),
) -> TraversalResult:
    _ensure_classified(graph)
    result = TraversalResult(start=start)
    if start not in graph.g:
        result.terminals.append(Terminal(start, "dead-end"))
        return result

    visited_hop: dict[str, int] = {start: 0}
    result.visited.append(VisitedNode(address=start, hop=0))
    queue: deque[tuple[str, int]] = deque([(start, 0)])

    while queue:
        address, hop = queue.popleft()
        if len(visited_hop) >= config.max_nodes:
            result.terminals.append(Terminal(address, "max-nodes"))
            break

        out_edges = graph.out_edges(address)
        if not out_edges:
            if hop > 0:  # start with no txs is not interesting; deeper dead-ends are
                result.terminals.append(Terminal(address, "dead-end"))
            continue

        # change branch first: peel-change edges jump the queue
        def _priority(e: tuple[str, str, dict]) -> int:
            return 0 if e[2].get("hop_kind") == HopKind.PEEL.value else 1

        for tgt, key, attrs in sorted(out_edges, key=_priority):
            kind = attrs.get("hop_kind", HopKind.DIRECT_TRANSFER.value)
            tx_hash = attrs.get("tx_hash", "")

            if kind in {k.value for k in _M4_KINDS}:
                result.terminals.append(
                    Terminal(tgt, f"unhandled-hop:{kind}")
                )
                continue

            if kind == HopKind.SWEEP_CANDIDATE.value:
                result.terminals.append(Terminal(tgt, "sweep-consolidation"))
                _back_label_sweep(graph, result, tx_hash)
                visited_hop.setdefault(tgt, hop + 1)
                continue

            if hop + 1 > config.max_hops:
                result.terminals.append(Terminal(tgt, "max-hops"))
                continue
            if tgt in visited_hop:
                continue

            side = bool(attrs.get("peel_payment"))
            visited_hop[tgt] = hop + 1
            result.came_from[tgt] = (address, tx_hash)
            result.visited.append(
                VisitedNode(
                    address=tgt,
                    hop=hop + 1,
                    via_tx=tx_hash,
                    via_kind=kind,
                    via_confidence=attrs.get("hop_confidence"),
                    side_branch=side,
                )
            )
            if kind == HopKind.PEEL.value:
                queue.appendleft((tgt, hop + 1))  # change branch first
            else:
                queue.append((tgt, hop + 1))

    return result


def _back_label_sweep(
    graph: TxGraph, result: TraversalResult, tx_hash: str
) -> None:
    """Retroactive back-labeling (plan 6): every input of the sweep tx is a
    per-user deposit address of the same entity — label them sweep-source."""
    tx = graph.tx(tx_hash)
    if tx is None:
        return
    for party in tx.inputs:
        if party.address == _COINBASE:
            continue
        graph.label(party.address, "sweep-source")
        result.labels_applied.setdefault(party.address, [])
        if "sweep-source" not in result.labels_applied[party.address]:
            result.labels_applied[party.address].append("sweep-source")
