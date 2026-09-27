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
  dex-swap        -> keep walking: same trader, new asset. The asset
                     change is recorded on the visited node.
  bridge-lock     -> STOP: value left the chain. The deposit is recorded
                     in result.bridge_deposits for M5+ cross-chain
                     correlation (engine/decoding/correlation.py scores
                     candidate withdrawals).
  mixer-deposit   -> STOP: funds entered the anonymity set. The depositor
                     is labeled "mixer-depositor".

swap-service is still an unhandled-hop terminal: detection needs a curated
address list (M5+).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from ..classifier.hops import HopKind, classify_graph
from ..graph.builder import TxGraph, _COINBASE

_UNHANDLED_KINDS = {HopKind.SWAP_SERVICE}


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
    note: Optional[str] = None  # e.g. "USDT->WETH" on dex-swap hops


@dataclass
class Terminal:
    address: str
    reason: str  # dead-end | sweep-consolidation | bridge-lock |
    # mixer-deposit | max-hops | max-nodes | unhandled-hop:<kind>


@dataclass
class BridgeDeposit:
    """Value locked into (or released from) a bridge — cross-chain lead."""

    address: str       # depositor on the lock side
    tx_hash: str
    chain: str
    bridge: str        # "stargate" | "wormhole" | ...
    direction: str     # "lock" | "release"
    asset_symbol: Optional[str]
    value: str         # smallest units


@dataclass
class TraversalResult:
    start: str
    visited: list[VisitedNode] = field(default_factory=list)
    terminals: list[Terminal] = field(default_factory=list)
    came_from: dict[str, tuple[str, str]] = field(default_factory=dict)
    # address -> parent address, tx_hash (path reconstruction for M6 reports)
    labels_applied: dict[str, list[str]] = field(default_factory=dict)
    bridge_deposits: list[BridgeDeposit] = field(default_factory=list)


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

            if kind in {k.value for k in _UNHANDLED_KINDS}:
                result.terminals.append(
                    Terminal(tgt, f"unhandled-hop:{kind}")
                )
                continue

            if kind == HopKind.SWEEP_CANDIDATE.value:
                result.terminals.append(Terminal(tgt, "sweep-consolidation"))
                _back_label_sweep(graph, result, tx_hash)
                visited_hop.setdefault(tgt, hop + 1)
                continue

            if kind == HopKind.BRIDGE_LOCK.value:
                result.terminals.append(Terminal(tgt, "bridge-lock"))
                _record_bridge_deposit(graph, result, address, tx_hash, attrs)
                visited_hop.setdefault(tgt, hop + 1)
                continue

            if kind == HopKind.MIXER_DEPOSIT.value:
                result.terminals.append(Terminal(tgt, "mixer-deposit"))
                graph.label(address, "mixer-depositor")
                result.labels_applied.setdefault(address, [])
                if "mixer-depositor" not in result.labels_applied[address]:
                    result.labels_applied[address].append("mixer-depositor")
                visited_hop.setdefault(tgt, hop + 1)
                continue

            note: Optional[str] = None
            if kind == HopKind.DEX_SWAP.value:
                # same trader, new asset — keep walking, record the change
                in_s = attrs.get("hop_in_symbol") or "?"
                out_s = attrs.get("hop_out_symbol") or "?"
                note = f"{in_s}->{out_s}"

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
                    note=note,
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
        # M16: a sweep input that is itself a recognized deposit proxy
        # (EIP-1167 / CREATE2 forwarder) is an exchange deposit address,
        # not an anonymous source — tag it specifically.
        if party.proxy_kind:
            graph.label(party.address, "deposit-proxy")
            if "deposit-proxy" not in result.labels_applied[party.address]:
                result.labels_applied[party.address].append("deposit-proxy")


def _record_bridge_deposit(
    graph: TxGraph,
    result: TraversalResult,
    src: str,
    tx_hash: str,
    attrs: dict,
) -> None:
    """Record a bridge lock/release as a cross-chain lead for M5+."""
    tx = graph.tx(tx_hash)
    if tx is None:
        return
    direction = attrs.get("hop_direction", "?")
    bridge = attrs.get("hop_bridge", "?")
    value, symbol = "0", tx.asset.symbol
    # the leg touching the bridge contract carries the locked amount
    for p in tx.inputs + tx.outputs:
        if direction == "lock" and p.address == src:
            value = p.value
            break
        if direction == "release" and p.address != src:
            value = p.value
            break
    result.bridge_deposits.append(
        BridgeDeposit(
            address=src,
            tx_hash=tx_hash,
            chain=tx.chain.value,
            bridge=bridge,
            direction=direction,
            asset_symbol=symbol,
            value=value,
        )
    )
