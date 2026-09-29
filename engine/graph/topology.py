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


def _denominate(raw_value: str, decimals: Any) -> str:
    """Human-denominated value string for display.

    Base units (satoshis, wei) are unreadable next to a coin symbol
    ("16397529 BTC"). When the asset's decimals are known, divide into
    coin units; otherwise return the raw value unchanged.
    """
    try:
        units = int(raw_value)
    except (TypeError, ValueError):
        return raw_value
    try:
        d = int(decimals)
    except (TypeError, ValueError):
        return raw_value
    if d < 0:
        return raw_value
    from decimal import Decimal
    coin = Decimal(units) / (Decimal(10) ** d)
    # Plain fixed-point notation, no scientific exponents.
    return format(coin, "f")


async def graph_topology(
    graph: TxGraph,
    store=None,
    *,
    max_nodes: int = 500,
    max_edges: int = 2000,
    pin_addrs: "set[str] | None" = None,
) -> dict[str, Any]:
    """Bounded {nodes, edges} JSON. Nodes ranked by degree so truncation
    keeps the most connected addresses, not arbitrary ones.

    M36: `pin_addrs` (e.g. the attribution subject -> terminal path) always
    survives truncation, and edges between pinned addresses are emitted
    first, so the workbench path can never be silently dropped from a
    truncated topology response."""
    g = graph.g
    pins = {a for a in (pin_addrs or set()) if a in g.nodes}
    ranked = sorted(g.nodes, key=lambda n: g.degree(n), reverse=True)
    kept = list(pins) + [n for n in ranked if n not in pins][:max_nodes]
    kept = kept[:max(max_nodes, len(pins))]
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
    def _edge_row(src, dst, a):
        raw_value = str(a.get("value"))
        return {
            "src": src,
            "dst": dst,
            "tx_hash": a.get("tx_hash"),
            "value": raw_value,
            "value_denominated": _denominate(raw_value, a.get("asset_decimals")),
            "asset_kind": a.get("asset_kind"),
            "asset_symbol": a.get("asset_symbol"),
            "asset_contract": a.get("asset_contract"),
            "asset_decimals": a.get("asset_decimals"),
            "block_time": a.get("block_time"),
            "block_number": a.get("block_number"),
        }
    # Pinned (path) edges first so max_edges can never cut the attribution path.
    for src, dst, _key, a in g.edges(keys=True, data=True):
        if src not in kept_set or dst not in kept_set:
            continue
        if src not in pins or dst not in pins:
            continue
        edges.append(_edge_row(src, dst, a))
        if len(edges) >= max_edges:
            break
    for src, dst, _key, a in g.edges(keys=True, data=True):
        if src not in kept_set or dst not in kept_set:
            continue
        if src in pins and dst in pins:
            continue  # already emitted above

        edges.append(_edge_row(src, dst, a))
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
            raw_hop_value = str(attrs.get("value"))
            hop.update({
                "via_tx": attrs.get("tx_hash"),
                "value": raw_hop_value,
                "value_denominated": _denominate(raw_hop_value, attrs.get("asset_decimals")),
                "asset_symbol": attrs.get("asset_symbol"),
                "asset_contract": attrs.get("asset_contract"),
                "block_time": attrs.get("block_time"),
            })
        hops.append(hop)
    return hops
