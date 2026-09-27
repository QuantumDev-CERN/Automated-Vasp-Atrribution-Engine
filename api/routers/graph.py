"""Graph + cross-case API (M8/M9/M11).

  GET /cases/{case_id}/graph/stats  — persisted subgraph stats (M8)
  GET /cases/{case_id}/graph        — visualization topology (M11)
  GET /cases/{case_id}/graph/path   — subject -> terminal hop path (M11)
  GET /cases/{case_id}/links        — cross-case links (M9)
  GET /intel/infrastructure/{tag}   — common-infrastructure pivot (M9)
"""
from fastapi import APIRouter, HTTPException, Query, Request

from engine.intel import (
    find_case_links,
    shared_infrastructure,
    syndicate_summary,
)

router = APIRouter(tags=["graph"])


def _graph_store(request: Request):
    store = request.app.state.graph_store
    if store is None:
        raise HTTPException(
            503, "graph store unavailable — no Neo4j and memory graph "
                 "disabled")
    return store


@router.get("/cases/{case_id}/graph/stats")
async def graph_stats(case_id: str, request: Request) -> dict:
    store = _graph_store(request)
    stats = await store.case_stats(case_id)
    if stats is None:
        raise HTTPException(404, "no persisted graph for case")
    return {"case_id": case_id, "backend": store.backend, **stats}


@router.get("/cases/{case_id}/graph")
async def graph_topology_api(
    case_id: str,
    request: Request,
    max_nodes: int = Query(500, ge=1, le=2000),
    max_edges: int = Query(2000, ge=1, le=10000),
    with_tags: bool = True,
) -> dict:
    """Fund-flow topology for the dashboard: bounded nodes/edges JSON
    with address labels, tags, and per-transfer detail (M11)."""
    from engine.graph.topology import graph_topology as build

    store = _graph_store(request)
    graph = await store.load_case_subgraph(case_id)
    if graph is None:
        raise HTTPException(404, "no persisted graph for case")
    topo = await build(graph, store if with_tags else None,
                       max_nodes=max_nodes, max_edges=max_edges)
    return {"case_id": case_id, **topo}


@router.get("/cases/{case_id}/graph/path")
async def graph_path(case_id: str, request: Request) -> dict:
    """Ordered subject -> terminal hop list with per-hop transfer detail."""
    from engine.graph.topology import trace_path

    store = _graph_store(request)
    meta = await store.case_meta(case_id)
    if not meta or not meta.get("subject") or not meta.get("terminal"):
        raise HTTPException(
            404, "no subject/terminal recorded for case — path unavailable")
    graph = await store.load_case_subgraph(case_id)
    if graph is None:
        raise HTTPException(404, "no persisted graph for case")
    path = trace_path(graph, meta["subject"], meta["terminal"])
    if path is None:
        raise HTTPException(404, "no path from subject to terminal in graph")
    return {"case_id": case_id, "subject": meta["subject"],
            "terminal": meta["terminal"], "hops": path}


@router.get("/cases/{case_id}/links")
async def case_links(case_id: str, request: Request,
                     min_overlap: int = 1) -> dict:
    store = request.app.state.graph_store
    links = await find_case_links(case_id, store, min_overlap=min_overlap)
    return {
        "case_id": case_id,
        "summary": syndicate_summary(links),
        "links": [
            {
                "case_id": lc.case_id,
                "overlap": lc.overlap,
                "shared_addresses": lc.shared_addresses,
                "shared_tags": lc.shared_tags,
            }
            for lc in links.links
        ],
    }


@router.get("/intel/infrastructure/{tag}")
async def infrastructure(tag: str, request: Request) -> dict:
    store = request.app.state.graph_store
    pivot = await shared_infrastructure(tag, store)
    return {"tag": tag, "addresses": pivot,
            "address_count": len(pivot)}
