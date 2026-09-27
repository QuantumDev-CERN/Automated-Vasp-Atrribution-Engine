from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "vasp-attribution-engine"}


@router.get("/ready")
async def ready(request: Request) -> dict:
    """Dependency checks (M7): what the store and queue actually are."""
    checks: dict[str, str] = {}
    store = getattr(request.app.state, "store", None)
    queue = getattr(request.app.state, "queue", None)
    checks["store"] = type(store).__name__ if store else "not-initialized"
    checks["queue"] = type(queue).__name__ if queue else "not-initialized"
    checks["neo4j"] = "phase-2"  # cross-case knowledge graph, not MVP
    ok = store is not None and queue is not None
    return {"status": "ok" if ok else "degraded", "checks": checks}
