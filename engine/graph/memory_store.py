"""M8: in-memory GraphStore — same API as Neo4j, no docker needed."""
from __future__ import annotations

from typing import Any, Optional

from .builder import TxGraph
from .store import GraphStore, _utcnow_iso, restore_graph, snapshot_graph


class MemoryGraphStore(GraphStore):
    def __init__(self) -> None:
        # case_id -> {"graph": snapshot dict, "stats": {...}, "meta": {...}}
        self._cases: dict[str, dict[str, Any]] = {}
        # (chain, address) -> [{"tag":..., "source":..., "case_id":...}]
        self._tags: dict[tuple[str, str], list[dict[str, str]]] = {}
        # (chain, address) -> set(case_id)
        self._addr_cases: dict[tuple[str, str], set[str]] = {}

    # ------------------------------------------------------------------ write

    async def save_case_subgraph(
        self,
        case_id: str,
        graph: TxGraph,
        meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, int]:
        snap = snapshot_graph(graph)
        stats = graph.stats()
        self._cases[case_id] = {
            "graph": snap,
            "stats": stats,
            "meta": meta or {},
            "saved_at": _utcnow_iso(),
        }
        for addr in graph.addresses():
            node = graph.g.nodes[addr]
            chains = node.get("chains") or set()
            for chain in chains:
                self._addr_cases.setdefault((chain, addr), set()).add(case_id)
        return stats

    async def tag_address(
        self,
        address: str,
        chain: str,
        tag: str,
        *,
        source: str,
        case_id: Optional[str] = None,
    ) -> None:
        key = (chain, address)
        entry = {"tag": tag, "source": source,
                 "case_id": case_id or "", "at": _utcnow_iso()}
        existing = self._tags.setdefault(key, [])
        if not any(e["tag"] == tag and e["source"] == source
                   for e in existing):
            existing.append(entry)

    # ------------------------------------------------------------------ read

    async def load_case_subgraph(self, case_id: str) -> Optional[TxGraph]:
        rec = self._cases.get(case_id)
        if rec is None:
            return None
        return restore_graph(rec["graph"])

    async def case_stats(self, case_id: str) -> Optional[dict[str, int]]:
        rec = self._cases.get(case_id)
        return dict(rec["stats"]) if rec else None

    async def address_tags(
        self, address: str, chain: str
    ) -> list[dict[str, str]]:
        return [dict(e) for e in self._tags.get((chain, address), [])]

    async def cases_for_address(
        self, address: str, chain: str
    ) -> list[str]:
        return sorted(self._addr_cases.get((chain, address), set()))

    async def addresses_with_tag(self, tag: str) -> list[dict[str, str]]:
        out = []
        for (chain, address), entries in self._tags.items():
            if any(e["tag"] == tag for e in entries):
                out.append({"address": address, "chain": chain})
        return sorted(out, key=lambda d: (d["chain"], d["address"]))

    # ------------------------------------------------------------------ life

    async def close(self) -> None:
        self._cases.clear()
        self._tags.clear()
        self._addr_cases.clear()

    @property
    def backend(self) -> str:
        return "memory"
