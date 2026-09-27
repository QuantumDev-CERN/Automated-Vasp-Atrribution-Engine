from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "vasp-attribution-engine"}


@router.get("/ready")
async def ready() -> dict:
    # M7: check postgres/redis/neo4j connectivity here
    return {"status": "ok", "checks": {"db": "not-wired-yet"}}
