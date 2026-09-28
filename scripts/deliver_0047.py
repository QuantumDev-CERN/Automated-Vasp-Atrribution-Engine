"""One-off: complete SAHYOG delivery + filing for EVAL/2026/0047.

The feed-attributed report already exists (created 2026-09-28);
only the delivery/filing step is missing.
"""
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()

EVAL_ID = "EVAL/2026/0047"


async def main() -> int:
    from api.core.config import settings
    from engine.delivery import deliver_attribution
    from engine.store import init_store
    from engine.store.base import (
        FILING_DELIVERED, FILING_FAILED, FilingIn,
    )

    store = await init_store(settings)
    cases, _ = await store.list_cases(limit=100)
    case = next((c for c in cases if c.fir_number == EVAL_ID), None)
    if case is None:
        print("case not found")
        return 1
    # Find the report directly (get_report_by_case goes via latest job,
    # which may not be the job the report was created under).
    from sqlalchemy import select
    from engine.store import models
    async with store._sessions() as s:
        q = (select(models.ReportRecord)
             .where(models.ReportRecord.case_id == case.id)
             .order_by(models.ReportRecord.created_at.desc())
             .limit(1))
        rec = (await s.execute(q)).scalar_one_or_none()
    if rec is None:
        print("no report")
        return 1
    from engine.store.postgres import _report as _to_report_rec
    report = _to_report_rec(rec)

    # Check if a filing already exists.
    filings = await store.list_filings(limit=100)
    items = filings[0] if isinstance(filings, tuple) else filings
    existing = [f for f in items if str(f.report_id) == str(report.id)]
    if existing:
        print(f"[{EVAL_ID}] filing exists — skipping")
        return 0

    job = await store.create_job(case.id, case.suspect_address,
                                 case.chain)
    await store.set_job(job.id, "done")
    job_rec = await store.get_job(job.id)
    delivery = await deliver_attribution(
        report, case, job_rec,
        base_url=settings.sahyog_mock_url,
        secret=settings.engine_webhook_secret,
    )
    await store.set_webhook_status(
        report.id, "delivered" if delivery.ok else "failed")
    await store.record_filing(FilingIn(
        case_id=case.id, report_id=report.id,
        status=FILING_DELIVERED if delivery.ok else FILING_FAILED,
        ack_ref=delivery.ack_ref or "",
        error="" if delivery.ok else (delivery.error or ""),
        attempts=delivery.attempts,
    ))
    print(f"[{EVAL_ID}] delivered: webhook_ok={delivery.ok} "
          f"ack_ref={delivery.ack_ref}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
