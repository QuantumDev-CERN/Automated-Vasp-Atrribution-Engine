"""FastAPI entrypoint. Routers for cases/trace/jobs/reports (M7)."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.core.config import settings
from api.core.logging import setup_logging
from api.routers.cases import router as cases_router
from api.routers.health import router as health_router
from api.routers.jobs import router as jobs_router
from api.routers.reports import router as reports_router

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from engine.graph import get_graph_store
    from engine.jobs import init_queue
    from engine.store import init_store
    app.state.store = await init_store(settings)
    app.state.queue = await init_queue(settings)
    app.state.graph_store = get_graph_store()  # M8: Neo4j or memory
    yield
    await app.state.queue.close()
    await app.state.graph_store.close()
    engine = getattr(app.state.store, "engine", None)
    if engine is not None:
        await engine.dispose()


app = FastAPI(
    title="VASP Attribution Engine",
    description="Automated attribution of unknown crypto wallets to nearest VASPs",
    version="0.7.0",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(cases_router)
app.include_router(jobs_router)
app.include_router(reports_router)
