"""M7 integration smoke: real Postgres + Redis (docker-compose services).

Gated: runs only when M7_INTEGRATION=1. If either service is unreachable
it prints SKIP and exits 0 — the offline path is covered by
scripts/smoke_m7.py and tests/test_m7.py.

Covers:
  1. PostgresStore round-trip (case/job/report/webhook_status).
  2. ArqQueue enqueue against real Redis.
  3. The real worker.trace_wallet job function against the real store
     (stub adapter, so no indexer keys needed), then report readable
     back from Postgres.

Run:  M7_INTEGRATION=1 uv run python scripts/smoke_m7_integration.py
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKIP = "SKIP: integration services unavailable (set M7_INTEGRATION=1 with " \
       "docker compose up postgres redis)"


def load_env() -> None:
    """Seed os.environ from .env (setdefault: a real exported var wins).

    The M7_INTEGRATION gate below reads os.environ, while pydantic Settings
    only reads .env into the settings object — without this, flags set in
    .env are invisible to the gate.
    """
    env_file = Path(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


async def _services_up(dsn: str, redis_url: str,
                       attempts: int = 6, delay_s: float = 5.0) -> bool:
    """Probe postgres + redis, retrying while docker services boot."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            from sqlalchemy.ext.asyncio import create_async_engine
            engine = create_async_engine(dsn)
            async with engine.connect():
                pass
            await engine.dispose()
            import redis.asyncio as redis
            client = redis.from_url(redis_url, socket_timeout=3)
            await client.ping()
            await client.aclose()
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < attempts - 1:
                print(f"[m7] services not ready (attempt {attempt + 1}/"
                      f"{attempts}), waiting {delay_s:g}s ...")
                await asyncio.sleep(delay_s)
    print(f"{SKIP} [{last}]")
    return False


async def main() -> int:
    load_env()
    if os.environ.get("M7_INTEGRATION") != "1":
        print(SKIP)
        return 0

    from api.core.config import settings
    if not await _services_up(settings.postgres_dsn, settings.redis_url):
        return 0

    from sqlalchemy.ext.asyncio import create_async_engine
    from engine.store.models import Base
    from engine.store.postgres import PostgresStore
    from engine.store.base import CaseIn
    from engine.jobs import ArqQueue
    from engine.intel import SanctionsList

    # 1. Postgres round-trip
    engine = create_async_engine(settings.postgres_dsn)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    store = PostgresStore(engine)
    case = await store.create_case(CaseIn(
        fir_number="FIR/2026/008888", suspect_address="0xsubject",
        chain="ethereum", officer_id="off-int", notes="integration"))
    job = await store.create_job(case.id, "0xsubject", "ethereum")
    await store.set_job(job.id, "running", arq_job_id="arq-int-1")
    got = await store.get_job(job.id)
    assert got.status == "running" and got.arq_job_id == "arq-int-1"
    print(f"postgres: case {case.id}, job {job.id} round-trip OK")

    # 2. Redis enqueue
    queue = ArqQueue(settings.redis_url)
    qid = await queue.enqueue_trace(str(job.id), str(case.id),
                                    "0xsubject", "ethereum")
    assert qid, "no queue job id returned"
    print(f"redis: enqueued as {qid}")
    await queue.close()

    # 3. real worker function, stub adapter (no indexer keys needed)
    from engine.adapters.base import ChainAdapter, Chain

    class StubAdapter(ChainAdapter):
        chain = Chain.ETHEREUM

        async def get_transactions(self, address, limit=100):
            return []

        async def get_token_transfers(self, address, limit=100):
            return []

        async def health_check(self):
            return True

    import worker as worker_mod
    from unittest.mock import patch
    from engine.delivery import DeliveryResult

    async def fake_deliver(*a, **k):
        return DeliveryResult(True, 1, 200)

    ctx = {"store": store, "settings": settings,
           "sanctions": SanctionsList.from_fixture()}
    with patch("engine.jobs.make_adapter",
               lambda chain: StubAdapter()), \
         patch("engine.delivery.deliver_attribution",
               side_effect=fake_deliver):
        out = await worker_mod.trace_wallet(
            ctx, job_id=str(job.id), case_id=str(case.id),
            address="0xsubject", chain="ethereum")
    assert out["webhook_ok"] is True
    done = await store.get_job(job.id)
    assert done.status == "done", done.status
    report = await store.get_report_by_job(job.id)
    assert report is not None
    assert report.webhook_status == "delivered"
    assert (await store.get_case(case.id)).status == "attributed"
    print(f"worker.trace_wallet: done, report {report.id} in postgres")
    print("M7 integration smoke OK")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
