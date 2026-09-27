"""arq worker (M7).

Runs the async trace pipeline: adapters -> graph -> classify -> traverse
-> score -> attribute -> report -> persist -> webhook to SAHYOG.

Run via docker-compose (`worker` service): arq worker.WorkerSettings
Run locally (needs redis + postgres, or falls back per settings):
    uv run arq worker.WorkerSettings
"""
import logging
from uuid import UUID

from arq.connections import RedisSettings

log = logging.getLogger(__name__)


def _redis_dsn() -> str:
    from api.core.config import settings
    return settings.redis_url


async def startup(ctx) -> None:
    from api.core.config import settings
    from engine.store import init_store

    ctx["settings"] = settings
    ctx["store"] = await init_store(settings)
    ctx["sanctions"] = _load_sanctions(settings)
    log.info("worker startup complete")


def _load_sanctions(settings):
    from engine.intel import SanctionsList
    path = settings.sanctions_table_path
    if path:
        log.info("sanctions: full table from %s", path)
        return SanctionsList.from_ofac_xml(path)
    log.info("sanctions: fixture sample")
    return SanctionsList.from_fixture()


async def shutdown(ctx) -> None:
    engine = getattr(ctx.get("store"), "engine", None)
    if engine is not None:
        await engine.dispose()


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
                            sanctions=sanctions)
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
        log.info("job %s done: report %s webhook_ok=%s",
                 job_id, report.id, delivery.ok)
        return {"report_id": str(report.id), "webhook_ok": delivery.ok}
    except Exception as exc:
        await store.set_job(jid, "failed",
                            error=f"{type(exc).__name__}: {exc}")
        log.exception("job %s failed", job_id)
        raise


class WorkerSettings:
    functions = [trace_wallet]
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 3
    redis_settings = RedisSettings.from_dsn(_redis_dsn())
