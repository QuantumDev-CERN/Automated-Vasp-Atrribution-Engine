"""Job queue abstraction (M7).

ArqQueue is the real path (Redis, docker-compose `redis` service).
MemoryQueue is the in-process stand-in for tests and for running the
API without docker: it records enqueued jobs and runs them inline when
`drain()` is called, so tests exercise the same submit->run->result
flow without Redis.
"""
import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class Queue(Protocol):
    async def enqueue_trace(self, job_id: str, case_id: str, address: str,
                            chain: str) -> str:
        """Enqueue a trace; returns the queue-native job id."""
        ...

    async def close(self) -> None: ...


class ArqQueue:
    def __init__(self, redis_dsn: str) -> None:
        self._dsn = redis_dsn
        self._pool = None

    async def _pool_or_create(self):
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings
            self._pool = await create_pool(
                RedisSettings.from_dsn(self._dsn))
        return self._pool

    async def enqueue_trace(self, job_id: str, case_id: str, address: str,
                            chain: str) -> str:
        pool = await self._pool_or_create()
        job = await pool.enqueue_job(
            "trace_wallet", job_id=job_id, case_id=case_id,
            address=address, chain=chain)
        return job.job_id

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class MemoryQueue:
    """Test double: records jobs; drain() runs them inline."""

    def __init__(self) -> None:
        self.enqueued: list[dict] = []
        self._runner = None  # async (job_id, case_id, address, chain)

    def on_enqueue(self, runner) -> None:
        self._runner = runner

    async def enqueue_trace(self, job_id: str, case_id: str, address: str,
                            chain: str) -> str:
        self.enqueued.append({"job_id": job_id, "case_id": case_id,
                              "address": address, "chain": chain})
        return f"mem-{job_id}"

    async def drain(self) -> None:
        if self._runner is None:
            return
        for item in self.enqueued:
            await self._runner(**item)
        self.enqueued.clear()

    async def close(self) -> None:
        pass


async def init_queue(settings) -> Queue:
    """API-side queue init. "auto": try Redis, fall back to memory."""
    backend = settings.queue_backend
    if backend == "memory":
        log.info("queue: memory (configured)")
        return MemoryQueue()
    if backend in ("redis", "auto"):
        try:
            q = ArqQueue(settings.redis_url)
            pool = await q._pool_or_create()
            await asyncio.wait_for(pool.ping(), timeout=3.0)
            log.info("queue: redis")
            return q
        except Exception as exc:
            if backend == "redis":
                raise
            log.warning("redis unavailable (%s); using memory queue", exc)
            return MemoryQueue()
    raise ValueError(f"unknown queue_backend: {backend}")
