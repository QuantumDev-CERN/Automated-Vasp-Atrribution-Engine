from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "vasp-attribution-engine"}


@router.get("/ready")
async def ready(request: Request) -> dict:
    """Dependency checks (M7/M8): what the store, queue, graph actually are."""
    checks: dict[str, str] = {}
    store = getattr(request.app.state, "store", None)
    queue = getattr(request.app.state, "queue", None)
    graph = getattr(request.app.state, "graph_store", None)
    checks["store"] = type(store).__name__ if store else "not-initialized"
    checks["queue"] = type(queue).__name__ if queue else "not-initialized"
    checks["graph"] = type(graph).__name__ if graph else "not-initialized"
    ok = store is not None and queue is not None and graph is not None
    return {"status": "ok" if ok else "degraded", "checks": checks}
