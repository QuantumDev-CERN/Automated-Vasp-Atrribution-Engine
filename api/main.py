"""FastAPI entrypoint. Routers for cases/trace/vasps/reports land in M7."""
from fastapi import FastAPI

from api.core.logging import setup_logging
from api.routers.health import router as health_router

setup_logging()

app = FastAPI(
    title="VASP Attribution Engine",
    description="Automated attribution of unknown crypto wallets to nearest VASPs",
    version="0.1.0",
)
app.include_router(health_router)
