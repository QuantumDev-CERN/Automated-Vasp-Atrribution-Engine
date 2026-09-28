"""Case intake (M7). Mirrors what SAHYOG submits; returns a case ID
immediately — the trace itself runs async (see jobs router).

M26 adds the read side the console needs: GET /cases (register with
filters) and GET /cases/{case_id}/latest (case -> latest job + report
summary with risk/confidence/terminal)."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.core.auth import (
    assert_case_access, assert_jurisdiction, get_current_user, require_cap,
)
from engine.store.base import ApiUserRec, CaseIn

router = APIRouter(prefix="/cases", tags=["cases"],
                   dependencies=[Depends(require_cap("read"))])


class CaseSubmit(BaseModel):
    fir_number: str = Field(..., examples=["FIR/2026/001234"])
    suspect_address: str
    chain: str = Field(..., examples=["ethereum", "bitcoin", "tron"])
    officer_id: str = ""
    notes: str = ""
    jurisdiction: str = "IN"  # M12: case-scoped authorization


def _dump(rec) -> dict:
    return {
        "case_id": str(rec.id),
        "fir_number": rec.fir_number,
        "suspect_address": rec.suspect_address,
        "chain": rec.chain,
        "officer_id": rec.officer_id,
        "notes": rec.notes,
        "jurisdiction": rec.jurisdiction,
        "status": rec.status,
        "created_at": rec.created_at.isoformat(),
    }


@router.post("", status_code=201,
             dependencies=[Depends(require_cap("write"))])
async def submit_case(payload: CaseSubmit, request: Request,
                      user: ApiUserRec = Depends(get_current_user)) -> dict:
    assert_jurisdiction(user, payload.jurisdiction)
    rec = await request.app.state.store.create_case(CaseIn(
        fir_number=payload.fir_number,
        suspect_address=payload.suspect_address,
        chain=payload.chain,
        officer_id=payload.officer_id,
        notes=payload.notes,
        jurisdiction=payload.jurisdiction,
    ))
    return {"case_id": str(rec.id), "status": rec.status}


@router.get("/{case_id}")
async def get_case(case_id: UUID, request: Request,
                   user: ApiUserRec = Depends(get_current_user)) -> dict:
    rec = await request.app.state.store.get_case(case_id)
    if rec is None:
        raise HTTPException(404, "case not found")
    assert_case_access(user, rec)
    return _dump(rec)


def _latest_summary(report) -> dict | None:
    """Durable trace-outcome summary for list rows and the detail view."""
    if report is None:
        return None
    return {
        "report_id": str(report.id),
        "job_id": str(report.job_id),
        "risk_score": report.risk_score,
        "risk_level": report.risk_level or None,
        "confidence": report.confidence,
        "terminal_address": report.terminal_address,
        "terminal_reason": report.terminal_reason,
        "hop_count": report.hop_count,
        "webhook_status": report.webhook_status,
        "generated_at": report.generated_at.isoformat(),
    }


@router.get("")
async def list_cases(request: Request,
                     user: ApiUserRec = Depends(get_current_user),
                     limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0),
                     status: str | None = None,
                     search: str | None = None) -> dict:
    """Paginated case register (M26). Each row carries its latest trace
    summary so the register table can show risk/confidence/terminal
    without N extra round trips per row... (one latest-job lookup per
    case; paginated, so bounded)."""
    store = request.app.state.store
    recs, total = await store.list_cases(
        limit=limit, offset=offset, status=status, search=search,
        jurisdictions=user.jurisdictions)
    rows = []
    for rec in recs:
        report = await store.get_report_by_case(rec.id)
        rows.append({**_dump(rec), "latest": _latest_summary(report)})
    return {"cases": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/{case_id}/latest")
async def case_latest(case_id: UUID, request: Request,
                      user: ApiUserRec = Depends(get_current_user)) -> dict:
    """Case -> latest trace job + report summary (M26). Powers the case
    detail SCORES panel, the attribution panel, and the report &
    certificate card."""
    store = request.app.state.store
    rec = await store.get_case(case_id)
    if rec is None:
        raise HTTPException(404, "case not found")
    assert_case_access(user, rec)
    job = await store.get_latest_job(rec.id)
    report = await store.get_report_by_case(rec.id)
    return {
        "case_id": str(rec.id),
        "case": _dump(rec),
        "job": ({
            "job_id": str(job.id),
            "address": job.address,
            "chain": job.chain,
            "status": job.status,
            "error": job.error,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        } if job else None),
        "report": _latest_summary(report),
        "certificate": ({
            "report_hash": report.report_hash,
            "inputs_hash": report.inputs_hash,
            "statement": report.certificate_statement,
            "engine_version": report.engine_version,
        } if report else None),
    }
