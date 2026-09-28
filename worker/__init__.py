"""arq worker (M7).

Runs the async trace pipeline: adapters -> graph -> classify -> traverse
-> score -> attribute -> report -> persist -> webhook to SAHYOG.

Run via docker-compose (`worker` service): arq worker.WorkerSettings
Run locally (needs redis + postgres, or falls back per settings):
    uv run arq worker.WorkerSettings
"""
import logging
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings

log = logging.getLogger(__name__)


def _redis_dsn() -> str:
    from api.core.config import settings
    return settings.redis_url


async def startup(ctx) -> None:
    from api.core.config import settings
    from engine.graph import get_graph_store
    from engine.store import init_store

    ctx["settings"] = settings
    ctx["store"] = await init_store(settings)
    ctx["sanctions"] = _load_sanctions(settings)
    ctx["threat_feeds"] = _load_threat_feeds()
    ctx["graph_store"] = get_graph_store()  # M8: Neo4j or memory fallback
    log.info("worker startup complete")


def _load_sanctions(settings):
    from engine.intel import SanctionsList
    path = settings.sanctions_table_path
    if path:
        log.info("sanctions: full table from %s", path)
        return SanctionsList.from_ofac_xml(path)
    log.info("sanctions: fixture sample")
    return SanctionsList.from_fixture()


def _load_threat_feeds():
    """M26: newest local snapshots (scripts/refresh_threat_feeds.py).
    None when absent — the pipeline then skips feed checks instead of
    inventing them. Corrupt snapshots raise (fail loudly)."""
    from engine.intel.threat_feeds import load_snapshots

    try:
        feeds = load_snapshots()
    except Exception as exc:  # noqa: BLE001 — corrupt snapshot: loud
        log.error("threat feeds: snapshot corrupt, refusing to start "
                  "feed checks: %s", exc)
        raise
    if feeds is None:
        log.warning("threat feeds: no snapshot in data/threat_feeds/ — "
                    "feed checks disabled (run "
                    "scripts/refresh_threat_feeds.py)")
    else:
        log.info("threat feeds: %d records (%d skipped malformed)",
                 len(feeds.records), feeds.skipped)
    return feeds


async def shutdown(ctx) -> None:
    engine = getattr(ctx.get("store"), "engine", None)
    if engine is not None:
        await engine.dispose()
    gs = ctx.get("graph_store")
    if gs is not None:
        await gs.close()


async def trace_wallet(ctx, *, job_id: str, case_id: str, address: str,
                       chain: str) -> dict:
    """arq job: run the full trace pipeline for one wallet."""
    from engine.delivery import deliver_attribution
    from engine.jobs import PipelineDeps, make_adapter, run_trace_pipeline
    from engine.store.base import ReportIn
    from engine.vasp import CaseDetails

    store = ctx["store"]
    settings = ctx["settings"]
    sanctions = ctx["sanctions"]

    jid, cid = UUID(job_id), UUID(case_id)
    await store.set_job(jid, "running")
    try:
        case_rec = await store.get_case(cid)
        if case_rec is None:
            raise ValueError(f"case not found: {case_id}")
        case = CaseDetails(
            case_id=case_rec.fir_number or str(case_rec.id),
            agency="SAHYOG intake",
            officer=case_rec.officer_id,
            wallets=(case_rec.suspect_address,),
            tx_hashes=(),
            date_from="",
            date_to="",
            suspected_offence=case_rec.notes,
        )
        deps = PipelineDeps(adapter_factory=make_adapter,
                            sanctions=sanctions,
                            threat_feeds=ctx.get("threat_feeds"),
                            graph_store=ctx.get("graph_store"),
                            case_id=str(cid),
                            calibration=await store.get_calibration())
        result = await run_trace_pipeline(address, chain, case, deps)

        from datetime import datetime
        report = await store.save_report(ReportIn(
            job_id=jid, case_id=cid,
            report_text=result.report.text,
            report_hash=result.report.certificate.report_hash,
            inputs_hash=result.report.certificate.inputs_hash,
            generated_at=datetime.fromisoformat(
                result.report.certificate.generated_at),
            engine_version=result.report.certificate.engine_version,
            certificate_statement=result.report.certificate.statement,
            # M26: durable outcome summary for the console read APIs.
            risk_score=result.risk.total,
            risk_level=result.risk.level,
            confidence=result.attribution.overall,
            terminal_address=result.terminal_address,
            terminal_reason=result.terminal_reason,
            hop_count=len(result.path),
        ))
        await store.set_job(jid, "done")
        await store.set_case_status(cid, "attributed")

        job_rec = await store.get_job(jid)
        delivery = await deliver_attribution(
            report, case_rec, job_rec,
            base_url=settings.sahyog_mock_url,
            secret=settings.engine_webhook_secret)
        await store.set_webhook_status(
            report.id, "delivered" if delivery.ok else "failed")
        # M26: durable filing record for every delivery attempt — the
        # filings register reads this, not the mock's in-memory log.
        from engine.store.base import (
            FILING_DELIVERED, FILING_FAILED, FilingIn,
        )
        await store.record_filing(FilingIn(
            case_id=cid, report_id=report.id,
            status=FILING_DELIVERED if delivery.ok else FILING_FAILED,
            # M28: persist the receiver's acknowledgement reference.
            ack_ref=delivery.ack_ref or "",
            error="" if delivery.ok else (delivery.error or ""),
            attempts=delivery.attempts,
        ))
        log.info("job %s done: report %s webhook_ok=%s",
                 job_id, report.id, delivery.ok)
        return {"report_id": str(report.id), "webhook_ok": delivery.ok}
    except Exception as exc:
        await store.set_job(jid, "failed",
                            error=f"{type(exc).__name__}: {exc}")
        log.exception("job %s failed", job_id)
        raise


def _watch_minutes() -> set[int]:
    from api.core.config import settings
    step = max(1, settings.watch_poll_minutes)
    return set(range(0, 60, step))


async def watcher_tick(ctx) -> dict:
    """arq cron: poll every active watch, alert on new movements (M10)."""
    from engine.jobs.pipeline import make_adapter
    from engine.watch.watcher import process_watch

    store = ctx["store"]
    settings = ctx["settings"]
    default_url = (settings.sahyog_mock_url.rstrip("/")
                   + "/sahyog/webhook/watch-alert")
    watches = await store.list_watches(active_only=True)
    events_total = 0
    for watch in watches:
        try:
            out = await process_watch(
                store, watch, adapter_factory=make_adapter,
                alert_url=watch.alert_url or default_url,
                secret=settings.engine_webhook_secret)
        except Exception:
            log.exception("watch %s check failed", watch.id)
            continue
        events_total += len(out["events"])
        log.info("watch %s: %d new event(s)", watch.id, len(out["events"]))
    return {"watches_checked": len(watches), "events": events_total}


class WorkerSettings:
    functions = [trace_wallet]
    cron_jobs = [
        cron(watcher_tick, minute=_watch_minutes(), run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 3
    redis_settings = RedisSettings.from_dsn(_redis_dsn())
