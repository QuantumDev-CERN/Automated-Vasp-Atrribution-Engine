"""Graph + cross-case API (M8/M9/M11).

  GET /cases/{case_id}/graph/stats  — persisted subgraph stats (M8)
  GET /cases/{case_id}/graph        — visualization topology (M11)
  GET /cases/{case_id}/graph/path   — subject -> terminal hop path (M11)
  GET /cases/{case_id}/links        — cross-case links (M9)
  GET /intel/infrastructure/{tag}   — common-infrastructure pivot (M9)
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.auth import (
    assert_case_access, get_current_user, require_cap,
)
from engine.intel import (
    find_case_links,
    shared_infrastructure,
    syndicate_summary,
)
from engine.store.base import ApiUserRec

router = APIRouter(tags=["graph"],
                   dependencies=[Depends(require_cap("read"))])


def _graph_store(request: Request):
    store = request.app.state.graph_store
    if store is None:
        raise HTTPException(
            503, "graph store unavailable — no Neo4j and memory graph "
                 "disabled")
    return store


async def _assert_graph_case_access(
    request: Request, user: ApiUserRec, case_id: str,
) -> None:
    """Graph endpoints key off the graph store, but case scoping still
    applies when the case exists in the record store."""
    try:
        from uuid import UUID

        case = await request.app.state.store.get_case(UUID(case_id))
    except Exception:
        return
    if case is not None:
        assert_case_access(user, case)


@router.get("/cases/{case_id}/graph")
async def graph_topology_api(
    case_id: str,
    request: Request,
    user: ApiUserRec = Depends(get_current_user),
    max_nodes: int = Query(500, ge=1, le=2000),
    max_edges: int = Query(2000, ge=1, le=10000),
    with_tags: bool = True,
) -> dict:
    """Fund-flow topology for the dashboard: bounded nodes/edges JSON
    with address labels, tags, and per-transfer detail (M11)."""
    from engine.graph.topology import graph_topology as build, trace_path

    await _assert_graph_case_access(request, user, case_id)
    store = _graph_store(request)
    graph = await store.load_case_subgraph(case_id)
    if graph is None:
        raise HTTPException(404, "no persisted graph for case")
    # M36: pin the attribution path so degree-based truncation can never
    # drop the subject -> terminal nodes or their edges from this response.
    pins: set[str] = set()
    meta = await store.case_meta(case_id)
    if meta and meta.get("subject") and meta.get("terminal"):
        try:
            path = trace_path(graph, meta["subject"], meta["terminal"])
        except Exception:
            path = None
        if path:
            pins = {h.get("address") for h in path if h.get("address")}
    topo = await build(graph, store if with_tags else None,
                       max_nodes=max_nodes, max_edges=max_edges,
                       pin_addrs=pins)
    return {"case_id": case_id, **topo}


@router.get("/cases/{case_id}/graph/stats")
async def graph_stats(case_id: str, request: Request,
                      user: ApiUserRec = Depends(get_current_user),
                      days: int = Query(30, ge=0, le=3650)) -> dict:
    await _assert_graph_case_access(request, user, case_id)
    store = _graph_store(request)
    stats = await store.case_stats(case_id)
    if stats is None:
        raise HTTPException(404, "no persisted graph for case")
    out = {"case_id": case_id, "backend": store.backend, **stats}
    # M26: classifier breakdown from the persisted hop path + per-day
    # activity for the workbench chart. Both derive from data the
    # pipeline persisted — nothing is re-inferred here.
    # M37: `days` scopes the activity window (0 = lifetime, no cutoff).
    meta = await store.case_meta(case_id) or {}
    hops = meta.get("hops") or []
    breakdown: dict[str, int] = {}
    for h in hops:
        kind = h.get("kind") or "unknown"
        breakdown[kind] = breakdown.get(kind, 0) + 1
    out["classifier_breakdown"] = breakdown
    out["daily_activity"] = await _daily_activity(store, case_id, days=days)
    out["activity_days"] = days
    return out


async def _daily_activity(store, case_id: str,
                          days: int = 30) -> list[dict]:
    """Per-day transaction/transfer counts for the last `days` days,
    from the persisted graph's edge block_times (M26).
    M37: days=0 means lifetime — no start cutoff."""
    from datetime import datetime, timedelta, timezone

    graph = await store.load_case_subgraph(case_id)
    if graph is None:
        return []
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).date() if days > 0 else None
    buckets: dict[str, dict[str, int | set]] = {}
    for _src, _dst, _key, attrs in graph.g.edges(keys=True, data=True):
        bt = attrs.get("block_time")
        if not bt:
            continue
        try:
            day = datetime.fromisoformat(bt).date().isoformat()
        except ValueError:
            continue
        if start is not None and day < start.isoformat():
            continue
        b = buckets.setdefault(day, {"txs": set(), "transfers": 0})
        b["txs"].add(attrs.get("tx_hash"))
        b["transfers"] += 1
    return [{"date": day,
             "transactions": len(b["txs"]),
             "transfers": b["transfers"]}
            for day, b in sorted(buckets.items())]


@router.get("/cases/{case_id}/graph/path")
async def graph_path(case_id: str, request: Request,
                     user: ApiUserRec = Depends(get_current_user)) -> dict:
    """Ordered subject -> terminal hop list with per-hop transfer detail."""
    from engine.graph.topology import trace_path

    await _assert_graph_case_access(request, user, case_id)
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
    # M26: join the pipeline's persisted hop classifications (kind +
    # confidence) onto the topological path by address.
    kinds = {h.get("address"): h for h in (meta.get("hops") or [])}
    enriched = []
    for hop in path:
        cls = kinds.get(hop.get("address"), {})
        enriched.append({**hop,
                         "kind": cls.get("kind"),
                         "confidence": cls.get("confidence"),
                         # M36: surface the pipeline's persisted per-hop
                         # classifier note (honest provenance for the
                         # workbench selected-node panel).
                         "reason": cls.get("note")})
    return {"case_id": case_id, "subject": meta["subject"],
            "terminal": meta["terminal"],
            "terminal_reason": meta.get("terminal_reason"),
            "hops": enriched}


@router.get("/cases/{case_id}/links")
async def case_links(case_id: str, request: Request,
                     user: ApiUserRec = Depends(get_current_user),
                     min_overlap: int = 1) -> dict:
    await _assert_graph_case_access(request, user, case_id)
    store = request.app.state.graph_store
    links = await find_case_links(case_id, store, min_overlap=min_overlap)
    visible = []
    for lc in links.links:
        try:
            from uuid import UUID

            other = await request.app.state.store.get_case(UUID(lc.case_id))
        except Exception:
            other = None
        if other is not None and not user.can_access(other.jurisdiction):
            continue  # M12: never leak a case outside the caller's scope
        visible.append(lc)
    return {
        "case_id": case_id,
        "summary": syndicate_summary(
            type(links)(case_id=links.case_id, links=visible)),
        "links": [
            {
                "case_id": lc.case_id,
                "overlap": lc.overlap,
                "shared_addresses": lc.shared_addresses,
                "shared_tags": lc.shared_tags,
            }
            for lc in visible
        ],
    }


@router.get("/intel/infrastructure/{tag}")
async def infrastructure(tag: str, request: Request) -> dict:
    store = request.app.state.graph_store
    pivot = await shared_infrastructure(tag, store)
    return {"tag": tag, "addresses": pivot,
            "address_count": len(pivot)}


#: traversal-classification tags ranked by the global feed. These are
#: the terminal reasons the engine itself assigns (engine/traversal)
#: plus the M19 OTC terminus — the same vocabulary tag_address()
#: persists, so the feed can only ever show tags the engine produced.
RANKED_TAGS = (
    "mixer-deposit",
    "coinjoin",
    "sweep-consolidation",
    "swap-service",
    "bridge-lock",
    "otc-hawala-terminus",
    "dead-end",
)


@router.get("/intel/links/ranked")
async def ranked_entities(request: Request,
                          user: ApiUserRec = Depends(get_current_user),
                          limit: int = Query(50, ge=1, le=200),
                          ) -> dict:
    """Global ranked intelligence feed (M26): every traversal tag ranked
    by how many addresses carry it, with per-tag case overlap. Only
    tags the engine itself assigned are eligible — nothing is
    invented to fill the feed."""
    from uuid import UUID

    store = request.app.state.graph_store
    if store is None:
        raise HTTPException(
            503, "graph store unavailable — no Neo4j and memory graph "
                 "disabled")
    ranked = []
    for tag in RANKED_TAGS:
        addrs = await store.addresses_with_tag(tag)
        if not addrs:
            continue
        case_ids: set[str] = set()
        for a in addrs[:200]:  # bounded: case overlap is a pivot, not a scan
            for cid in await store.cases_for_address(a["address"],
                                                     a["chain"]):
                case_ids.add(cid)
        # M12: case counts never leak cases outside the caller's scope.
        visible_cases = []
        for cid in sorted(case_ids):
            try:
                case = await request.app.state.store.get_case(UUID(cid))
            except Exception:
                case = None
            if case is not None and user.can_access(case.jurisdiction):
                visible_cases.append(cid)
        ranked.append({
            "tag": tag,
            "address_count": len(addrs),
            "case_count": len(visible_cases),
            "cases": visible_cases[:25],
            "addresses": addrs[:25],
        })
    ranked.sort(key=lambda r: r["address_count"], reverse=True)
    return {"ranked": ranked[:limit], "total_tags": len(ranked)}
