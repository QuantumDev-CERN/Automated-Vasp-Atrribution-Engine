"""Case intake (M7). Mirrors what SAHYOG submits; returns a case ID
immediately — the trace itself runs async (see jobs router)."""
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from engine.store.base import CaseIn

router = APIRouter(prefix="/cases", tags=["cases"])


class CaseSubmit(BaseModel):
    fir_number: str = Field(..., examples=["FIR/2026/001234"])
    suspect_address: str
    chain: str = Field(..., examples=["ethereum", "bitcoin", "tron"])
    officer_id: str = ""
    notes: str = ""


def _dump(rec) -> dict:
    return {
        "case_id": str(rec.id),
        "fir_number": rec.fir_number,
        "suspect_address": rec.suspect_address,
        "chain": rec.chain,
        "officer_id": rec.officer_id,
        "notes": rec.notes,
        "status": rec.status,
        "created_at": rec.created_at.isoformat(),
    }


@router.post("", status_code=201)
async def submit_case(payload: CaseSubmit, request: Request) -> dict:
    rec = await request.app.state.store.create_case(CaseIn(
        fir_number=payload.fir_number,
        suspect_address=payload.suspect_address,
        chain=payload.chain,
        officer_id=payload.officer_id,
        notes=payload.notes,
    ))
    return {"case_id": str(rec.id), "status": rec.status}


@router.get("/{case_id}")
async def get_case(case_id: UUID, request: Request) -> dict:
    rec = await request.app.state.store.get_case(case_id)
    if rec is None:
        raise HTTPException(404, "case not found")
    return _dump(rec)
