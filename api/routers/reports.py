"""Investigation reports (M7). Read back a generated report with its
evidentiary certificate fields."""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{report_id}")
async def get_report(report_id: UUID, request: Request) -> dict:
    rec = await request.app.state.store.get_report(report_id)
    if rec is None:
        raise HTTPException(404, "report not found")
    return {
        "report_id": str(rec.id),
        "job_id": str(rec.job_id),
        "case_id": str(rec.case_id),
        "report_hash": rec.report_hash,
        "inputs_hash": rec.inputs_hash,
        "generated_at": rec.generated_at.isoformat(),
        "engine_version": rec.engine_version,
        "certificate_statement": rec.certificate_statement,
        "webhook_status": rec.webhook_status,
        "report_text": rec.report_text,
    }
