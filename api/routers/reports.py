"""Investigation reports (M7). Read back a generated report with its
evidentiary certificate fields."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from api.core.auth import assert_case_access, get_current_user, require_cap
from engine.store.base import ApiUserRec

router = APIRouter(prefix="/reports", tags=["reports"],
                   dependencies=[Depends(require_cap("read"))])


@router.get("/{report_id}")
async def get_report(report_id: UUID, request: Request,
                     user: ApiUserRec = Depends(get_current_user)) -> dict:
    rec = await request.app.state.store.get_report(report_id)
    if rec is None:
        raise HTTPException(404, "report not found")
    case = await request.app.state.store.get_case(rec.case_id)
    if case is not None:
        assert_case_access(user, case)
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
