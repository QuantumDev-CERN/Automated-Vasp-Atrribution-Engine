"""Common-input-ownership clustering (M17, master plan section 6).

The heuristic: every input of a UTXO transaction must be signed, so all
input addresses of one transaction are assumed to belong to a single
entity. Union-find merges those addresses transitively across the whole
transaction set (A+B in tx1, B+C in tx2 => {A, B, C}).

Two hard rules keep the heuristic honest:

  1. The CoinJoin guard (see coinjoin.py) runs FIRST. A flagged
     transaction's inputs are never merged, and the flag is recorded in
     ``ClusteringResult.excluded`` as auditable evidence.
  2. Only transactions with at least two distinct spend input addresses
     merge anything. Single-input transactions carry no co-input
     information, and account-model chains (one sender per transfer)
     naturally contribute nothing — the rule is behavioural, not a
     chain allow-list.

``cluster_id`` is the lexicographically smallest member address, so
clustering is deterministic across runs and the id is self-describing in
reports. Only clusters with >= 2 members are reported; lone addresses are
not clusters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..adapters.base import CanonicalTx
from .coinjoin import CoinJoinEvidence, detect_coinjoin

_COINBASE = "coinbase"  # pseudo-input label used by the Bitcoin adapter


class _UnionFind:
    __slots__ = ("parent", "rank")

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        parent = self.parent
        # path halving
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


@dataclass
class ClusteringResult:
    """Outcome of common-input clustering over a transaction set."""

    clusters: dict[str, str] = field(default_factory=dict)
    """address -> cluster_id, for clustered addresses only."""

    members: dict[str, frozenset[str]] = field(default_factory=dict)
    """cluster_id -> member addresses."""

    excluded: list[CoinJoinEvidence] = field(default_factory=list)
    """Transactions the CoinJoin guard kept out of the merge, with reasons."""

    txs_seen: int = 0
    txs_merged: int = 0

    def cluster_of(self, address: str) -> Optional[str]:
        """Cluster id for an address, or None if it stands alone."""
        return self.clusters.get(address)

    def cluster_size(self, address: str) -> int:
        cid = self.clusters.get(address)
        return len(self.members[cid]) if cid else 1


def cluster_coinput_transactions(
    txs: list[CanonicalTx],
    *,
    coinjoin_kwargs: Optional[dict] = None,
) -> ClusteringResult:
    """Cluster addresses by common input ownership.

    ``coinjoin_kwargs`` (optional) is forwarded to
    :func:`coinjoin.detect_coinjoin` to tune the guard thresholds.
    """
    coinjoin_kwargs = coinjoin_kwargs or {}
    uf = _UnionFind()
    result = ClusteringResult(txs_seen=len(txs))

    for tx in txs:
        evidence = detect_coinjoin(tx, **coinjoin_kwargs)
        if evidence.is_coinjoin:
            result.excluded.append(evidence)
            continue
        inputs = sorted(
            {p.address for p in tx.inputs if p.address != _COINBASE}
        )
        if len(inputs) < 2:
            continue
        for addr in inputs:
            uf.add(addr)
        first = inputs[0]
        for addr in inputs[1:]:
            uf.union(first, addr)
        result.txs_merged += 1

    groups: dict[str, set[str]] = {}
    for addr in uf.parent:
        groups.setdefault(uf.find(addr), set()).add(addr)

    for members in groups.values():
        if len(members) < 2:
            continue
        cid = min(members)  # deterministic, self-describing cluster id
        frozen = frozenset(members)
        result.members[cid] = frozen
        for addr in frozen:
            result.clusters[addr] = cid

    return result
