"""FastAPI entrypoint. Routers for cases/trace/jobs/reports (M7)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.core.audit import AuditMiddleware
from api.core.config import settings
from api.core.logging import setup_logging
from api.routers.admin import router as admin_router
from api.routers.cases import router as cases_router
from api.routers.feedback import router as feedback_router
from api.routers.filings import router as filings_router
from api.routers.graph import router as graph_router
from api.routers.health import router as health_router
from api.routers.jobs import router as jobs_router
from api.routers.reports import router as reports_router
from api.routers.watchlist import router as watchlist_router

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    from engine.graph import get_graph_store
    from engine.graph.memory_store import MemoryGraphStore
    from engine.jobs import init_queue
    from engine.store import init_store
    log = logging.getLogger("vasp.api")
    app.state.store = await init_store(settings)
    app.state.queue = await init_queue(settings)
    app.state.graph_store = get_graph_store()  # M8: Neo4j or memory
    # File-backed memory graph: reload seed/rebuild data if present.
    if isinstance(app.state.graph_store, MemoryGraphStore):
        loaded = MemoryGraphStore.load_from_file(
            "data/graph_store.json")
        if loaded is not None:
            app.state.graph_store = loaded
            log.info("[graph] loaded %d cases from data/graph_store.json",
                     len(loaded._cases))
        else:
            log.info("[graph] no data/graph_store.json found")
    yield
    await app.state.queue.close()
    await app.state.graph_store.close()
    engine = getattr(app.state.store, "engine", None)
    if engine is not None:
        await engine.dispose()


app = FastAPI(
    title="VASP Attribution Engine",
    description="Automated attribution of unknown crypto wallets to nearest VASPs",
    version="0.9.0",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(cases_router)
app.include_router(graph_router)
app.include_router(jobs_router)
app.include_router(reports_router)
app.include_router(filings_router)
app.include_router(watchlist_router)
app.include_router(admin_router)
app.include_router(feedback_router)

app.add_middleware(AuditMiddleware)  # M12: durable audit trail
