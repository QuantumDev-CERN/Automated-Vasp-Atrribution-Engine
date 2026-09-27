"""Persistence layer (M7)."""
import logging

from .base import (
    CaseIn, CaseRec, JobRec, ReportIn, ReportRec, Store,
)
from .memory import MemoryStore

log = logging.getLogger(__name__)

__all__ = [
    "CaseIn", "CaseRec", "JobRec", "ReportIn", "ReportRec", "Store",
    "MemoryStore", "create_store", "init_store",
]


def create_store(dsn: str | None):
    """Real Postgres when a DSN is configured, in-memory otherwise."""
    if not dsn:
        return MemoryStore()
    from sqlalchemy.ext.asyncio import create_async_engine
    from .postgres import PostgresStore
    return PostgresStore(create_async_engine(dsn))


async def init_store(settings) -> Store:
    """Shared by the API and the worker.

    backend "auto": try Postgres, fall back to memory with a loud
    warning. "postgres": fail hard if unreachable. "memory": in-process.
    """
    from .models import Base

    backend = settings.store_backend
    if backend == "memory":
        log.info("store: memory (configured)")
        return MemoryStore()
    if backend in ("postgres", "auto"):
        try:
            from sqlalchemy.ext.asyncio import create_async_engine
            from .postgres import PostgresStore
            engine = create_async_engine(settings.postgres_dsn)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            log.info("store: postgres")
            return PostgresStore(engine)
        except Exception as exc:
            if backend == "postgres":
                raise
            log.warning("postgres unavailable (%s); using memory store",
                        exc)
            return MemoryStore()
    raise ValueError(f"unknown store_backend: {backend}")
