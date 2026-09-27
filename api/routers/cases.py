"""Case intake (M7). Mirrors what SAHYOG submits; returns a case ID
immediately — the trace itself runs async (see jobs router)."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
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
