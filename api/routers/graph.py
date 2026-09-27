"""Graph + cross-case API (M8/M9).

  GET /cases/{case_id}/graph/stats  — persisted subgraph stats (M8)
  GET /cases/{case_id}/links        — cross-case links (M9)
  GET /intel/infrastructure/{tag}   — common-infrastructure pivot (M9)
"""
from fastapi import APIRouter, HTTPException, Request

from engine.intel import (
    find_case_links,
    shared_infrastructure,
    syndicate_summary,
)

router = APIRouter(tags=["graph"])


@router.get("/cases/{case_id}/graph/stats")
async def graph_stats(case_id: str, request: Request) -> dict:
    store = request.app.state.graph_store
    stats = await store.case_stats(case_id)
    if stats is None:
        raise HTTPException(404, "no persisted graph for case")
    return {"case_id": case_id, "backend": store.backend, **stats}


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
