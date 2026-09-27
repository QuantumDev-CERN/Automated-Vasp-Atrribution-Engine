"""Async trace jobs (M7). POST /jobs/trace returns a job ID at once;
the worker resolves it and pushes the result to SAHYOG via webhook.
Poll GET /jobs/{job_id} for status (the master reference's async model:
answers can arrive later; the webhook is the push channel)."""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/jobs", tags=["jobs"])


class TraceRequest(BaseModel):
    case_id: UUID
    address: str | None = None  # defaults to the case's suspect_address
    chain: str | None = None    # defaults to the case's chain


@router.post("/trace", status_code=202)
async def start_trace(payload: TraceRequest, request: Request) -> dict:
    store = request.app.state.store
    case = await store.get_case(payload.case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    address = payload.address or case.suspect_address
    chain = payload.chain or case.chain
    job = await store.create_job(case.id, address, chain)
    queue_job_id = await request.app.state.queue.enqueue_trace(
        str(job.id), str(case.id), address, chain)
    await store.set_job(job.id, "queued", arq_job_id=queue_job_id)
    return {"job_id": str(job.id), "status": "queued"}


@router.get("/{job_id}")
async def get_job(job_id: UUID, request: Request) -> dict:
    store = request.app.state.store
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    report = await store.get_report_by_job(job.id)
    return {
        "job_id": str(job.id),
        "case_id": str(job.case_id),
        "address": job.address,
        "chain": job.chain,
        "status": job.status,
        "error": job.error,
        "report_id": str(report.id) if report else None,
        "webhook_status": report.webhook_status if report else None,
    }
