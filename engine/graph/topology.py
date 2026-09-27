"""M11: fund-flow topology for visualization.

graph_topology() turns a persisted TxGraph into bounded nodes/edges JSON
a dashboard can render directly. trace_path() extracts the subject ->
terminal path as an ordered hop list with per-hop transfer detail.

Both are pure functions over TxGraph (+ optional tag enrichment from
the GraphStore); the API router wires them to HTTP.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Optional

from .builder import TxGraph


async def graph_topology(
    graph: TxGraph,
    store=None,
    *,
    max_nodes: int = 500,
    max_edges: int = 2000,
) -> dict[str, Any]:
    """Bounded {nodes, edges} JSON. Nodes ranked by degree so truncation
    keeps the most connected addresses, not arbitrary ones."""
    g = graph.g
    ranked = sorted(g.nodes, key=lambda n: g.degree(n), reverse=True)
    kept = ranked[:max_nodes]
    kept_set = set(kept)

    nodes: list[dict[str, Any]] = []
    for addr in kept:
        nd = g.nodes[addr]
        first = nd.get("first_seen")
        node: dict[str, Any] = {
            "id": addr,
            "chains": sorted(nd.get("chains") or []),
            "labels": sorted(nd.get("labels") or []),
            "first_seen": first.isoformat() if first else None,
            "degree": g.degree(addr),
        }
        if store is not None:
            tags: list[str] = []
            for chain in node["chains"] or ["unknown"]:
                tags.extend(
                    t["tag"] for t in await store.address_tags(addr, chain))
            node["tags"] = sorted(set(tags))
        nodes.append(node)

    edges: list[dict[str, Any]] = []
    for src, dst, _key, a in g.edges(keys=True, data=True):
        if src not in kept_set or dst not in kept_set:
            continue
        edges.append({
            "src": src,
            "dst": dst,
            "tx_hash": a.get("tx_hash"),
            "value": str(a.get("value")),
            "asset_kind": a.get("asset_kind"),
            "asset_symbol": a.get("asset_symbol"),
            "asset_contract": a.get("asset_contract"),
            "block_time": a.get("block_time"),
            "block_number": a.get("block_number"),
        })
        if len(edges) >= max_edges:
            break

    return {
        "nodes": nodes,
        "edges": edges,
        "truncated": len(ranked) > max_nodes or g.number_of_edges() > len(edges),
        "total_addresses": g.number_of_nodes(),
        "total_transfers": g.number_of_edges(),
    }


def trace_path(
    graph: TxGraph,
    subject: str,
    terminal: str,
) -> Optional[list[dict[str, Any]]]:
    """Shortest subject -> terminal path as ordered hops.

    Each hop: {hop, address, via_tx, value, asset, block_time}.
    None when no path exists.
    """
    g = graph.g
    if subject not in g or terminal not in g:
        return None
    prev: dict[str, tuple[str, dict[str, Any]]] = {}
    queue: deque[str] = deque([subject])
    seen = {subject}
    while queue:
        cur = queue.popleft()
        if cur == terminal:
            break
        for _, nxt, _key, attrs in g.out_edges(cur, keys=True, data=True):
            if nxt not in seen:
                seen.add(nxt)
                prev[nxt] = (cur, dict(attrs))
                queue.append(nxt)
    if terminal != subject and terminal not in prev:
        return None

    # rebuild address chain, then attach edge detail
    chain = [terminal]
    while chain[-1] != subject:
        chain.append(prev[chain[-1]][0])
    chain.reverse()

    hops: list[dict[str, Any]] = []
    for i, addr in enumerate(chain):
        hop: dict[str, Any] = {"hop": i, "address": addr}
        if i > 0:
            _parent, attrs = prev[addr]
            hop.update({
                "via_tx": attrs.get("tx_hash"),
                "value": str(attrs.get("value")),
                "asset_symbol": attrs.get("asset_symbol"),
                "asset_contract": attrs.get("asset_contract"),
                "block_time": attrs.get("block_time"),
            })
        hops.append(hop)
    return hops
