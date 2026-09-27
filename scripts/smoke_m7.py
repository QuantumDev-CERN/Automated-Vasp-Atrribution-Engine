"""M7 smoke: intake -> queue -> pipeline -> persist -> signed webhook.

Offline — MemoryStore, MemoryQueue, stub adapter, and the mock SAHYOG
server driven in-process via ASGI transport. Exercises the same seams
the docker deployment uses (store protocol, queue protocol, HMAC-signed
webhook with signature verification on the mock side).

The docker-backed path (real Redis + Postgres + arq worker) is covered
by scripts/smoke_m7_integration.py, gated on M7_INTEGRATION=1.
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

os.environ.setdefault("ENGINE_WEBHOOK_SECRET",
                      "dev-webhook-secret-change-me")

from api.core.config import settings  # noqa: E402
from engine.adapters.base import (  # noqa: E402
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.intel import SanctionsList  # noqa: E402
from engine.jobs import MemoryQueue, PipelineDeps, run_trace_pipeline  # noqa: E402
from engine.delivery import deliver_attribution  # noqa: E402
from engine.store import MemoryStore  # noqa: E402
from engine.store.base import CaseIn, ReportIn  # noqa: E402
from engine.vasp import CaseDetails  # noqa: E402
from integrations.sahyog_mock.mock_server import app as mock_app  # noqa: E402


class StubAdapter(ChainAdapter):
    chain = Chain.ETHEREUM

    async def get_transactions(self, address, limit=100):
        return []

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


async def main() -> int:
    store = MemoryStore()
    queue = MemoryQueue()
    sanctions = SanctionsList.from_fixture()

    # 1. intake
    case = await store.create_case(CaseIn(
        fir_number="FIR/2026/007777", suspect_address="0xsubject",
        chain="ethereum", officer_id="off-smoke", notes="smoke"))
    print(f"case: {case.id} ({case.status})")

    # 2. the job runner: what the arq worker does, inline
    async def runner(job_id: str, case_id: str, address: str, chain: str):
        jid, cid = UUID(job_id), UUID(case_id)
        await store.set_job(jid, "running")
        case_rec = await store.get_case(cid)
        details = CaseDetails(
            case_id=case_rec.fir_number, agency="SAHYOG intake",
            officer=case_rec.officer_id, wallets=(address,), tx_hashes=(),
            date_from="", date_to="", suspected_offence=case_rec.notes)
        deps = PipelineDeps(
            adapter_factory=lambda c: StubAdapter(), sanctions=sanctions)
        result = await run_trace_pipeline(address, chain, details, deps)
        from datetime import datetime
        report = await store.save_report(ReportIn(
            job_id=jid, case_id=cid, report_text=result.report.text,
            report_hash=result.report.certificate.report_hash,
            inputs_hash=result.report.certificate.inputs_hash,
            generated_at=datetime.fromisoformat(
                result.report.certificate.generated_at),
            engine_version=result.report.certificate.engine_version,
            certificate_statement=result.report.certificate.statement))
        await store.set_job(jid, "done")
        transport = httpx.ASGITransport(app=mock_app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://mock") as client:
            delivery = await deliver_attribution(
                report, case_rec, await store.get_job(jid),
                base_url="http://mock",
                secret=settings.engine_webhook_secret, client=client)
        await store.set_webhook_status(
            report.id, "delivered" if delivery.ok else "failed")
        await store.set_case_status(cid, "attributed")
        print(f"job {jid}: done, webhook_ok={delivery.ok}, "
              f"attempts={delivery.attempts}")

    queue.on_enqueue(runner)

    # 3. submit + drain (async intake model)
    job = await store.create_job(case.id, "0xsubject", "ethereum")
    qid = await queue.enqueue_trace(str(job.id), str(case.id),
                                    "0xsubject", "ethereum")
    await store.set_job(job.id, "queued", arq_job_id=qid)
    await queue.drain()

    # 4. verify: job done, report persisted, webhook received + verified
    got = await store.get_job(job.id)
    assert got.status == "done", got.status
    report = await store.get_report_by_job(job.id)
    assert report is not None and report.webhook_status == "delivered"
    assert (await store.get_case(case.id)).status == "attributed"

    transport = httpx.ASGITransport(app=mock_app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://mock") as client:
        r = await client.get("/sahyog/webhooks")
        data = r.json()
    assert data["count"] >= 1, data
    payload = data["webhooks"][-1]["payload"]
    assert payload["case_id"] == str(case.id)
    assert payload["report_hash"] == report.report_hash
    print(f"mock SAHYOG received {data['count']} webhook(s); "
          f"signature verified, idempotency key = report hash")
    print("M7 smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
