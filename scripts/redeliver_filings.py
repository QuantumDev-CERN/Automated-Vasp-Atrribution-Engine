"""Re-deliver failed SAHYOG filings (M33).

Context: the M31 seed ran while the sahyog-mock compose service didn't
share the engine's .env, so the mock verified webhook HMACs against the
dev-default secret and rejected all 41 deliveries with 401. M32 gives
the mock the shared .env; this script re-runs delivery for every filing
still marked failed.

Semantics mirror the worker: one NEW filing row per delivery attempt
(the register is append-only and auditable), plus the report's webhook
status is updated on success. Idempotent — already-delivered filings
are never touched; re-running only retries the still-failed ones.

Usage:
    uv run python scripts/redeliver_filings.py            # redeliver all failed
    uv run python scripts/redeliver_filings.py --dry-run  # show what would run
    uv run python scripts/redeliver_filings.py --limit 5  # first 5 only
"""
import argparse
import asyncio
import os
import sys
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def redeliver(store, settings, limit=None, dry_run=False):
    """Returns (delivered, still_failed)."""
    from engine.delivery import webhook as webhook_mod
    from engine.store.base import (
        FILING_DELIVERED, FILING_FAILED, FilingIn,
    )

    filings, total = await store.list_filings(status=FILING_FAILED,
                                              limit=limit or 500)
    print(f"failed filings: {total} (processing {len(filings)})")
    delivered = 0
    still_failed = 0
    for f in filings:
        case = await store.get_case(f.case_id)
        report = await store.get_report(f.report_id)
        get_latest_job = getattr(store, "get_latest_job", None)
        job = await get_latest_job(f.case_id) if get_latest_job else None
        if case is None or report is None or job is None:
            print(f"  [SKIP] filing {f.id}: missing case/report/job")
            still_failed += 1
            continue
        if dry_run:
            print(f"  [dry-run] would redeliver filing {f.id} "
                  f"case={case.fir_number}")
            continue
        delivery = await webhook_mod.deliver_attribution(
            report, case, job,
            base_url=settings.sahyog_mock_url,
            secret=settings.engine_webhook_secret)
        await store.record_filing(FilingIn(
            case_id=case.id, report_id=report.id,
            status=FILING_DELIVERED if delivery.ok else FILING_FAILED,
            ack_ref=delivery.ack_ref or "",
            error="" if delivery.ok else (delivery.error or ""),
            attempts=delivery.attempts,
        ))
        if delivery.ok:
            await store.set_webhook_status(
                report.id, "delivered")
            delivered += 1
            print(f"  [OK] {case.fir_number} ack_ref={delivery.ack_ref}")
        else:
            still_failed += 1
            print(f"  [FAIL] {case.fir_number}: {delivery.error}")
    return delivered, still_failed


async def _amain(argv=None) -> int:
    from api.core.config import settings
    from engine.store import init_store
    from engine.store.memory import MemoryStore

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    store = await init_store(settings)
    if isinstance(store, MemoryStore):
        print("REFUSING: memory store is process-local — point "
              "DATABASE_URL at the seed database.")
        return 2
    delivered, still_failed = await redeliver(
        store, settings, limit=args.limit, dry_run=args.dry_run)
    print(f"done: {delivered} delivered, {still_failed} still failed")
    engine = getattr(store, "engine", None)
    if engine is not None:
        await engine.dispose()
    return 0 if still_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
