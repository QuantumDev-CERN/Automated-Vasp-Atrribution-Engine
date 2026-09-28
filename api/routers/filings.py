"""Filings register (M26).

A filing is the durable record of an attribution package delivered to
an authority (SAHYOG webhook). The worker records one row per delivery
attempt — success or failure — so the register survives restarts,
unlike the SAHYOG mock's in-memory log.

  GET  /filings                 — paginated register (ref, filed, subject,
                                 type, channel, status, ack ref)
  GET  /filings/{filing_id}     — detail: summary, signals cited (from the
                                 report), transmission log, acknowledgement
  POST /filings/{filing_id}/resend — server-side signed re-send of the
                                 attribution webhook (the frontend must
                                 never hold SAHYOG_WEBHOOK_SECRET)
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.auth import (
    assert_case_access, get_current_user, require_cap,
)
from engine.store.base import (
    FILING_DELIVERED, FILING_FAILED, FilingIn, ApiUserRec,
)

router = APIRouter(prefix="/filings", tags=["filings"],
                   dependencies=[Depends(require_cap("read"))])

_write = Depends(require_cap("write"))


def _dump(filing, case=None) -> dict:
    return {
        "filing_id": str(filing.id),
        "case_id": str(filing.case_id),
        "report_id": str(filing.report_id),
        "fir_number": case.fir_number if case else None,
        "suspect_address": case.suspect_address if case else None,
        "chain": case.chain if case else None,
        "channel": filing.channel,
        "status": filing.status,
        "ack_ref": filing.ack_ref or None,
        "error": filing.error or None,
        "attempts": filing.attempts,
        "filed_at": filing.filed_at.isoformat(),
    }


@router.get("")
async def list_filings(request: Request,
                       user: ApiUserRec = Depends(get_current_user),
                       limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0),
                       status: str | None = None) -> dict:
    store = request.app.state.store
    recs, total = await store.list_filings(
        limit=limit, offset=offset, status=status)
    rows = []
    for rec in recs:
        case = await store.get_case(rec.case_id)
        if case is not None and not user.can_access(case.jurisdiction):
            continue  # M12: never leak a case outside the caller's scope
        rows.append(_dump(rec, case))
    return {"filings": rows, "total": total, "limit": limit,
            "offset": offset}


@router.get("/{filing_id}")
async def get_filing(filing_id: UUID, request: Request,
                     user: ApiUserRec = Depends(get_current_user)) -> dict:
    store = request.app.state.store
    rec = await store.get_filing(filing_id)
    if rec is None:
        raise HTTPException(404, "filing not found")
    case = await store.get_case(rec.case_id)
    if case is not None:
        assert_case_access(user, case)
    report = await store.get_report(rec.report_id)
    return {
        **_dump(rec, case),
        "transmission": {
            "attempts": rec.attempts,
            "status": rec.status,
            "error": rec.error or None,
        },
        "report": ({
            "report_id": str(report.id),
            "report_hash": report.report_hash,
            "risk_score": report.risk_score,
            "risk_level": report.risk_level or None,
            "confidence": report.confidence,
            "terminal_address": report.terminal_address,
            "terminal_reason": report.terminal_reason,
        } if report else None),
    }


@router.post("/{filing_id}/resend", dependencies=[_write])
async def resend_filing(filing_id: UUID, request: Request,
                        user: ApiUserRec = Depends(get_current_user)) -> dict:
    """Server-side signed re-send of the attribution webhook for this
    filing's report. Records a NEW filing row (audit trail of the
    re-send); the original row is untouched."""
    from api.core.config import settings
    from engine.delivery import deliver_attribution

    store = request.app.state.store
    rec = await store.get_filing(filing_id)
    if rec is None:
        raise HTTPException(404, "filing not found")
    case = await store.get_case(rec.case_id)
    if case is not None:
        assert_case_access(user, case)
    report = await store.get_report(rec.report_id)
    if report is None:
        raise HTTPException(409, "filing has no report to re-send")
    job = await store.get_job(report.job_id)
    if job is None:
        raise HTTPException(409, "filing has no job to re-send")

    delivery = await deliver_attribution(
        report, case, job,
        base_url=settings.sahyog_mock_url,
        secret=settings.engine_webhook_secret)
    new_rec = await store.record_filing(FilingIn(
        case_id=rec.case_id, report_id=rec.report_id,
        channel=rec.channel,
        status=FILING_DELIVERED if delivery.ok else FILING_FAILED,
        ack_ref=delivery.ack_ref or "",  # M28: mock-issued ack reference
        error="" if delivery.ok else (delivery.error or ""),
        attempts=delivery.attempts,
    ))
    await store.set_webhook_status(
        report.id, "delivered" if delivery.ok else "failed")
    return {
        "filing_id": str(new_rec.id),
        "resent_from": str(rec.id),
        "status": new_rec.status,
        "attempts": new_rec.attempts,
        "error": new_rec.error or None,
    }
