"""Feedback loop API (M13).

  POST /feedback/outcomes     — record a confirmed VASP-cooperation
                                 outcome (ground truth for the model)
  GET  /feedback/outcomes     — list recorded outcomes
  POST /feedback/recalibrate  — fit a new versioned calibration curve
  GET  /feedback/calibration  — the latest calibration curve

Recording and recalibrating need the write capability (analyst+);
reading needs read (viewer+). Every call is audit-logged by the
M12 middleware like everything else.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.core.auth import (
    assert_case_access, get_current_user, require_cap,
)
from engine.feedback import describe_model, fit_calibration
from engine.store.base import VALID_OUTCOMES, ApiUserRec, FeedbackOutcomeIn

router = APIRouter(prefix="/feedback", tags=["feedback"],
                   dependencies=[Depends(require_cap("read"))])

_write = Depends(require_cap("write"))


class OutcomeSubmit(BaseModel):
    case_id: UUID
    vasp: str = Field(..., examples=["CoinDCX"])
    predicted_confidence: float = Field(..., ge=0.0, le=1.0)
    outcome: str = Field(..., examples=["confirmed", "refuted",
                                        "inconclusive"])
    notes: str = ""


def _dump(o) -> dict:
    return {
        "outcome_id": str(o.id),
        "case_id": str(o.case_id),
        "vasp": o.vasp,
        "predicted_confidence": o.predicted_confidence,
        "outcome": o.outcome,
        "notes": o.notes,
        "recorded_by": o.recorded_by,
        "created_at": o.created_at.isoformat(),
    }


@router.post("/outcomes", status_code=201, dependencies=[_write])
async def record_outcome(payload: OutcomeSubmit, request: Request,
                         user: ApiUserRec = Depends(get_current_user)
                         ) -> dict:
    if payload.outcome not in VALID_OUTCOMES:
        raise HTTPException(
            400, f"outcome must be one of {', '.join(VALID_OUTCOMES)}")
    store = request.app.state.store
    case = await store.get_case(payload.case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    assert_case_access(user, case)
    rec = await store.record_outcome(FeedbackOutcomeIn(
        case_id=payload.case_id, vasp=payload.vasp,
        predicted_confidence=payload.predicted_confidence,
        outcome=payload.outcome, notes=payload.notes),
        recorded_by=user.name)
    return _dump(rec)


@router.get("/outcomes")
async def list_outcomes(request: Request,
                        outcome: str | None = None) -> dict:
    if outcome is not None and outcome not in VALID_OUTCOMES:
        raise HTTPException(
            400, f"outcome must be one of {', '.join(VALID_OUTCOMES)}")
    rows = await request.app.state.store.list_outcomes(outcome=outcome)
    return {"outcomes": [_dump(o) for o in rows], "count": len(rows)}


@router.post("/recalibrate", dependencies=[_write])
async def recalibrate(request: Request,
                      user: ApiUserRec = Depends(get_current_user)) -> dict:
    """Fit a new calibration version from all confirmed/refuted outcomes.

    The new model is immutable and versioned; reports generated before
    it keep the version they were certified with.
    """
    store = request.app.state.store
    outcomes = await store.list_outcomes()
    usable = [o for o in outcomes if o.outcome in ("confirmed", "refuted")]
    existing = await store.list_calibrations()
    version = f"cal-{len(existing) + 1}"
    model = fit_calibration(usable, version=version, created_by=user.name)
    await store.save_calibration(model)
    return {"model": describe_model(model),
            "note": "historical reports are unchanged; new traces use "
                    f"{version}"}


@router.get("/calibration")
async def get_calibration(request: Request) -> dict:
    model = await request.app.state.store.get_calibration()
    if model is None:
        return {"model": None,
                "note": "no calibration fitted yet — the raw model is "
                        "used unchanged"}
    return {"model": describe_model(model)}
