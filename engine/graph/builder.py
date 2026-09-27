"""Directed fund-flow graph built from canonical transactions.

Nodes are addresses. Edges are value transfers. A UTXO transaction expands
bipartitely — every input address gets an edge to every output address of the
same tx — because input/output linkage inside one tx is exactly what the hop
classifier exists to resolve (peel vs payment). Account-model chains
(1-in/1-out transfers) produce one edge per transfer.

Every edge carries its source tx_hash, so the classifier and traversal
engine can always get back to the full CanonicalTx (kept in ``TxGraph.txs``).

Case-scoped by design (master plan 4.15): materialize subgraphs only around
flagged addresses, never whole chains.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

import networkx as nx

from ..adapters.base import CanonicalTx, ChainAdapter

if TYPE_CHECKING:  # M17: clustering result type for annotate_clusters
    from ..clustering import ClusteringResult

_COINBASE = "coinbase"  # pseudo-input label used by the Bitcoin adapter


class TxGraph:
    """Typed wrapper around a networkx MultiDiGraph of address -> address."""

    def __init__(self) -> None:
        self.g = nx.MultiDiGraph()
        self.txs: dict[str, CanonicalTx] = {}

    # ------------------------------------------------------------------ build

    def add_tx(self, tx: CanonicalTx) -> int:
        """Add one canonical tx. Returns the number of edges created."""
        self.txs[tx.tx_hash] = tx
        inputs = [p for p in tx.inputs if p.address != _COINBASE]
        for party in inputs:
            self._touch_node(party.address, tx)
        for party in tx.outputs:
            self._touch_node(party.address, tx)

        created = 0
        for i, src in enumerate(inputs):
            for j, dst in enumerate(tx.outputs):
                self.g.add_edge(
                    src.address,
                    dst.address,
                    key=f"{tx.tx_hash}:{i}->{j}",
                    tx_hash=tx.tx_hash,
                    chain=tx.chain.value,
                    asset_kind=tx.asset.kind.value,
                    asset_symbol=tx.asset.symbol,
                    asset_contract=tx.asset.contract,
                    asset_decimals=tx.asset.decimals,
                    value=int(dst.value),  # output value; see module docstring
                    block_time=tx.block_time.isoformat() if tx.block_time else None,
                    block_number=tx.block_number,
                    fee=tx.fee,
                )
                created += 1
        return created

    @classmethod
    def build(cls, txs: list[CanonicalTx]) -> "TxGraph":
        graph = cls()
        for tx in txs:
            graph.add_tx(tx)
        return graph

    def _touch_node(self, address: str, tx: CanonicalTx) -> None:
        if address not in self.g:
            self.g.add_node(
                address, chains=set(), first_seen=None, labels=set()
            )
        node = self.g.nodes[address]
        node["chains"].add(tx.chain.value)
        if tx.block_time and (
            node["first_seen"] is None or tx.block_time < node["first_seen"]
        ):
            node["first_seen"] = tx.block_time

    # ------------------------------------------------------------------ read

    def addresses(self) -> list[str]:
        return list(self.g.nodes)

    def out_edges(self, address: str) -> list[tuple[str, str, dict[str, Any]]]:
        """(target, edge_key, attrs) for every transfer out of address."""
        if address not in self.g:
            return []
        return [
            (tgt, key, dict(attrs))
            for _, tgt, key, attrs in self.g.out_edges(address, keys=True, data=True)
        ]

    def in_edges(self, address: str) -> list[tuple[str, str, dict[str, Any]]]:
        if address not in self.g:
            return []
        return [
            (src, key, dict(attrs))
            for src, _, key, attrs in self.g.in_edges(address, keys=True, data=True)
        ]

    def tx(self, tx_hash: str) -> Optional[CanonicalTx]:
        return self.txs.get(tx_hash)

    def label(self, address: str, label: str) -> None:
        if address in self.g:
            self.g.nodes[address]["labels"].add(label)

    def labels(self, address: str) -> set[str]:
        if address not in self.g:
            return set()
        return set(self.g.nodes[address]["labels"])

    def first_seen(self, address: str) -> Optional[datetime]:
        if address not in self.g:
            return None
        return self.g.nodes[address]["first_seen"]

    def stats(self) -> dict[str, int]:
        return {
            "addresses": self.g.number_of_nodes(),
            "transfers": self.g.number_of_edges(),
            "transactions": len(self.txs),
        }

    def annotate_clusters(self, result: ClusteringResult) -> int:
        """Attach M17 common-input cluster ids to graph nodes.

        Nodes whose address belongs to a cluster get a ``cluster_id``
        attribute (the cluster's deterministic id); unclustered nodes are
        left untouched. Returns the number of nodes annotated.
        """
        annotated = 0
        for address, cid in result.clusters.items():
            if address in self.g:
                self.g.nodes[address]["cluster_id"] = cid
                annotated += 1
        return annotated

    def cluster_id(self, address: str) -> Optional[str]:
        """Cluster id previously attached by annotate_clusters, if any."""
        if address not in self.g:
            return None
        return self.g.nodes[address].get("cluster_id")


async def expand_address(
    graph: TxGraph,
    adapter: ChainAdapter,
    address: str,
    limit: int = 25,
    include_tokens: bool = True,
) -> int:
    """Fetch txs involving address and add them to the graph.

    Returns the number of transactions added. Both native and token
    transfers are pulled when the adapter supports tokens.
    """
    txs = await adapter.get_transactions(address, limit=limit)
    if include_tokens:
        txs = txs + await adapter.get_token_transfers(address, limit=limit)
    for tx in txs:
        graph.add_tx(tx)
    return len(txs)
