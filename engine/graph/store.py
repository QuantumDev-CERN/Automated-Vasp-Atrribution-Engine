"""M8: persistent graph storage.

Every traced case's subgraph is materialized into a graph store instead of
dying in memory when the job ends. Two implementations behind one ABC:

  Neo4jGraphStore  — real Neo4j (docker compose service), the M9 cross-case
                     engine queries this.
  MemoryGraphStore — in-memory stand-in: same API, no docker needed. Unit
                     tests and the offline gate run against this.

Both persist:
  * the queryable skeleton — Address nodes, SENT edges (one per
    address->address transfer, carrying tx/asset/value/block props),
    Case-[:INCLUDES]->Address membership, and Address-[:TAGGED]->Tag
    provenance labels;
  * a full-fidelity snapshot of the TxGraph (Pydantic-serialized) so
    load_case_subgraph() rebuilds the exact in-memory graph.

get_graph_store() prefers Neo4j and falls back to memory when the bolt
port is unreachable — same auto-fallback philosophy as the M7 stores.
"""
from __future__ import annotations

import abc
from datetime import datetime, timezone
from typing import Any, Optional

from .builder import TxGraph


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot_graph(graph: TxGraph) -> dict[str, Any]:
    """Lossless TxGraph -> JSON-safe dict (Pydantic does the heavy lifting)."""
    nodes = {}
    for addr in graph.addresses():
        node = graph.g.nodes[addr]
        first = node.get("first_seen")
        nodes[addr] = {
            "chains": sorted(node.get("chains") or set()),
            "first_seen": first.isoformat() if first else None,
            "labels": sorted(node.get("labels") or set()),
        }
    edges = [
        {"src": src, "dst": dst, "key": key, "attrs": _jsonable(attrs)}
        for src, dst, key, attrs in graph.g.edges(keys=True, data=True)
    ]
    txs = {
        h: tx.model_dump(mode="json") for h, tx in graph.txs.items()
    }
    return {"nodes": nodes, "edges": edges, "txs": txs,
            "saved_at": _utcnow_iso()}


def _jsonable(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if isinstance(v, set):
            out[k] = sorted(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def restore_graph(snap: dict[str, Any]) -> TxGraph:
    """snapshot_graph() inverse — rebuilds the exact TxGraph."""
    from ..adapters.base import CanonicalTx

    graph = TxGraph()
    for h, txd in snap.get("txs", {}).items():
        graph.txs[h] = CanonicalTx.model_validate(txd)
    for addr, nd in snap.get("nodes", {}).items():
        graph.g.add_node(addr)
        node = graph.g.nodes[addr]
        node["chains"] = set(nd.get("chains") or [])
        node["labels"] = set(nd.get("labels") or [])
        fs = nd.get("first_seen")
        node["first_seen"] = (
            datetime.fromisoformat(fs) if fs else None)
    for e in snap.get("edges", []):
        graph.g.add_edge(e["src"], e["dst"], key=e["key"],
                         **e.get("attrs", {}))
    return graph


class GraphStore(abc.ABC):
    """Persistent per-case subgraph storage + cross-case address index."""

    # ------------------------------------------------------------------ write

    @abc.abstractmethod
    async def save_case_subgraph(
        self,
        case_id: str,
        graph: TxGraph,
        meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, int]:
        """Persist one case's traced subgraph. Returns stats
        {addresses, transfers, transactions}."""

    @abc.abstractmethod
    async def tag_address(
        self,
        address: str,
        chain: str,
        tag: str,
        *,
        source: str,
        case_id: Optional[str] = None,
    ) -> None:
        """Attach a provenance label to an address, e.g.
        tag="mixer-deposit", source="traversal:case-123". Tags are the
        persistent vocabulary M9's cross-case correlation queries."""

    # ------------------------------------------------------------------ read

    @abc.abstractmethod
    async def load_case_subgraph(self, case_id: str) -> Optional[TxGraph]:
        """Rebuild the exact TxGraph saved for a case, or None."""

    @abc.abstractmethod
    async def case_stats(self, case_id: str) -> Optional[dict[str, int]]:
        """{addresses, transfers, transactions} for a saved case."""

    @abc.abstractmethod
    async def case_meta(self, case_id: str) -> Optional[dict[str, Any]]:
        """The meta dict passed to save_case_subgraph (subject, chain,
        terminal, ...). None when the case was never saved."""

    @abc.abstractmethod
    async def address_tags(self, address: str, chain: str) -> list[dict[str, str]]:
        """[{tag, source}] provenance labels on an address."""

    @abc.abstractmethod
    async def cases_for_address(self, address: str, chain: str) -> list[str]:
        """Case ids whose subgraph includes this address — the primitive
        M9's syndicate correlation is built on."""

    @abc.abstractmethod
    async def addresses_with_tag(self, tag: str) -> list[dict[str, str]]:
        """[{address, chain}] every address carrying a tag, any case."""

    # ------------------------------------------------------------------ life

    @abc.abstractmethod
    async def close(self) -> None:
        """Release connections; no-op for memory."""

    @property
    @abc.abstractmethod
    def backend(self) -> str:
        """'neo4j' | 'memory' — surfaced on /ready."""


def get_graph_store() -> GraphStore:
    """Neo4j when the bolt port answers, else the memory stand-in."""
    from api.core.config import settings

    try:
        from .neo4j_store import Neo4jGraphStore

        store = Neo4jGraphStore(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )
        store.ping()
        print(f"[graph] Neo4j store: {settings.neo4j_uri}")
        return store
    except Exception as exc:  # noqa: BLE001 — fallback is the point
        from .memory_store import MemoryGraphStore

        print(f"[graph] Neo4j unavailable ({exc}); using memory store")
        return MemoryGraphStore()
