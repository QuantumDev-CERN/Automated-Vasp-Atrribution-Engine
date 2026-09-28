"""Watchlist API (M10).

  POST /watchlist                  — subscribe an address
  GET  /watchlist                  — list subscriptions (with lifetime
                                     check/alert counts + 24h volume)
  GET  /watchlist/{watch_id}       — one subscription + check history +
                                     alert ledger with dispositions
  PATCH /watchlist/{watch_id}      — pause/resume {status}
  DELETE /watchlist/{watch_id}     — unsubscribe
  GET  /watchlist/{watch_id}/alerts — detected movements
  GET  /watchlist/{watch_id}/checks — check-cycle history
  PATCH /watchlist/{watch_id}/alerts/{alert_id} — reviewer disposition
  POST /watchlist/{watch_id}/check  — run one check cycle now
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.core.auth import get_current_user, require_cap
from engine.store import WatchIn
from engine.store.base import ApiUserRec

router = APIRouter(prefix="/watchlist", tags=["watchlist"],
                   dependencies=[Depends(require_cap("read"))])

_write = Depends(require_cap("write"))


class WatchSubmit(BaseModel):
    address: str
    chain: str = Field(..., examples=["ethereum", "bitcoin", "tron"])
    label: str = ""
    classification: str = Field(
        "general", examples=["suspect", "terminal", "counterparty",
                             "vasp", "general"])
    case_id: UUID | None = None
    alert_url: str = ""  # "" = mock SAHYOG watch-alert endpoint
    created_by: str = ""


class WatchPatch(BaseModel):
    status: str = Field(..., examples=["active", "paused"])


def _dump(w) -> dict:
    return {
        "watch_id": str(w.id),
        "address": w.address,
        "chain": w.chain,
        "label": w.label,
        "case_id": str(w.case_id) if w.case_id else None,
        "alert_url": w.alert_url,
        "created_by": w.created_by,
        "status": w.status,
        "classification": getattr(w, "classification", "general"),
        "last_checked_at": (w.last_checked_at.isoformat()
                            if w.last_checked_at else None),
        "created_at": w.created_at.isoformat(),
    }


async def _enrich(w, request: Request) -> dict:
    """M26: watch detail payload — check history, alert ledger with
    dispositions, cadence, lifetime counts. All from durable rows."""
    from datetime import datetime, timedelta, timezone

    from api.core.config import settings

    store = request.app.state.store
    checks = await store.list_watch_checks(w.id, limit=50)
    alerts = await store.list_alerts(w.id)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return {
        **_dump(w),
        "cadence_minutes": settings.watch_poll_minutes,
        "lifetime_checks": len(checks),
        "lifetime_alerts": len(alerts),
        "alerts_24h": sum(1 for a in alerts
                           if a.created_at.isoformat() >= cutoff),
        "check_history": [
            {"check_id": str(c.id),
             "checked_at": c.checked_at.isoformat(),
             "txs_seen": c.txs_seen, "new_events": c.new_events,
             "alerts_delivered": c.alerts_delivered,
             "baseline": c.baseline, "error": c.error or None}
            for c in checks
        ],
        "alerts": [
            {"alert_id": str(a.id), "tx_hash": a.tx_hash,
             "direction": a.direction, "counterparty": a.counterparty,
             "value": a.value, "asset": a.asset,
             "vasp_hit": a.vasp_hit, "delivered": a.delivered,
             "disposition": a.disposition or None,
             "disposition_notes": a.disposition_notes or None,
             "disposition_by": a.disposition_by or None,
             "disposition_at": (a.disposition_at.isoformat()
                                if a.disposition_at else None),
             "created_at": a.created_at.isoformat()}
            for a in alerts
        ],
    }


@router.post("", status_code=201, dependencies=[_write])
async def add_watch(payload: WatchSubmit, request: Request) -> dict:
    rec = await request.app.state.store.add_watch(WatchIn(
        address=payload.address, chain=payload.chain, label=payload.label,
        classification=payload.classification,
        case_id=payload.case_id, alert_url=payload.alert_url,
        created_by=payload.created_by))
    return {"watch_id": str(rec.id), "status": rec.status}


@router.get("")
async def list_watches(request: Request,
                       active_only: bool = True) -> dict:
    """Watch register (M26): every row carries its lifetime check/alert
    counts and 24h alert volume for the ledger table."""
    store = request.app.state.store
    recs = await store.list_watches(active_only=active_only)
    rows = []
    for r in recs:
        checks = await store.list_watch_checks(r.id, limit=1)
        alerts = await store.list_alerts(r.id)
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=24)).isoformat()
        rows.append({
            **_dump(r),
            "lifetime_checks": len(checks),
            "lifetime_alerts": len(alerts),
            "alerts_24h": sum(1 for a in alerts
                               if a.created_at.isoformat() >= cutoff),
            "last_check_ok": (checks[0].error in (None, "")
                              if checks else None),
        })
    return {"watches": rows}


@router.get("/{watch_id}")
async def get_watch(watch_id: UUID, request: Request) -> dict:
    rec = await request.app.state.store.get_watch(watch_id)
    if rec is None:
        raise HTTPException(404, "watch not found")
    return await _enrich(rec, request)


@router.patch("/{watch_id}", dependencies=[_write])
async def patch_watch(watch_id: UUID, payload: WatchPatch,
                      request: Request) -> dict:
    if payload.status not in ("active", "paused"):
        raise HTTPException(400, "status must be active|paused")
    rec = await request.app.state.store.get_watch(watch_id)
    if rec is None:
        raise HTTPException(404, "watch not found")
    await request.app.state.store.set_watch(watch_id, payload.status)
    return {"watch_id": str(watch_id), "status": payload.status}


@router.delete("/{watch_id}", dependencies=[_write])
async def remove_watch(watch_id: UUID, request: Request) -> dict:
    rec = await request.app.state.store.get_watch(watch_id)
    if rec is None:
        raise HTTPException(404, "watch not found")
    await request.app.state.store.remove_watch(watch_id)
    return {"watch_id": str(watch_id), "removed": True}


@router.get("/{watch_id}/checks")
async def list_checks(watch_id: UUID, request: Request,
                      limit: int = Query(50, ge=1, le=200)) -> dict:
    """Check-cycle history (M26): every poll of this watch, including
    baselines and failures."""
    store = request.app.state.store
    if await store.get_watch(watch_id) is None:
        raise HTTPException(404, "watch not found")
    checks = await store.list_watch_checks(watch_id, limit=limit)
    return {"watch_id": str(watch_id), "checks": [
        {"check_id": str(c.id),
         "checked_at": c.checked_at.isoformat(),
         "txs_seen": c.txs_seen, "new_events": c.new_events,
         "alerts_delivered": c.alerts_delivered,
         "baseline": c.baseline, "error": c.error or None}
        for c in checks]}


class DispositionSubmit(BaseModel):
    disposition: str = Field(
        ..., description="true_positive|false_positive|benign|escalated")
    notes: str = ""


@router.patch("/{watch_id}/alerts/{alert_id}", dependencies=[_write])
async def dispose_alert(watch_id: UUID, alert_id: UUID,
                        payload: DispositionSubmit,
                        request: Request,
                        user: ApiUserRec = Depends(get_current_user)) -> dict:
    """Reviewer disposition for an alert (M26): reviewed / false_positive
    / escalated, with notes and reviewer attribution."""
    from engine.store.base import VALID_DISPOSITIONS

    if payload.disposition not in VALID_DISPOSITIONS:
        raise HTTPException(
            400, f"disposition must be one of {sorted(VALID_DISPOSITIONS)}")
    store = request.app.state.store
    if await store.get_watch(watch_id) is None:
        raise HTTPException(404, "watch not found")
    ok = await store.set_alert_disposition(
        alert_id, payload.disposition, payload.notes,
        by=user.name or "")
    if not ok:
        raise HTTPException(404, "alert not found")
    return {"alert_id": str(alert_id), "watch_id": str(watch_id),
            "disposition": payload.disposition}


@router.get("/{watch_id}/alerts")
async def list_alerts(watch_id: UUID, request: Request) -> dict:
    if await request.app.state.store.get_watch(watch_id) is None:
        raise HTTPException(404, "watch not found")
    alerts = await request.app.state.store.list_alerts(watch_id)
    return {"watch_id": str(watch_id), "alerts": [
        {"alert_id": str(a.id), "tx_hash": a.tx_hash,
         "direction": a.direction, "counterparty": a.counterparty,
         "value": a.value, "asset": a.asset, "vasp_hit": a.vasp_hit,
         "delivered": a.delivered,
         "disposition": a.disposition or None,
         "disposition_notes": a.disposition_notes or None,
         "disposition_by": a.disposition_by or None,
         "disposition_at": (a.disposition_at.isoformat()
                            if a.disposition_at else None),
         "created_at": a.created_at.isoformat()}
        for a in alerts]}


@router.post("/{watch_id}/check", dependencies=[_write])
async def check_now(watch_id: UUID, request: Request) -> dict:
    """Run one check cycle immediately (no waiting for the cron tick)."""
    from api.core.config import settings
    from engine.jobs.pipeline import make_adapter
    from engine.watch.watcher import process_watch

    store = request.app.state.store
    rec = await store.get_watch(watch_id)
    if rec is None:
        raise HTTPException(404, "watch not found")
    if rec.status != "active":
        raise HTTPException(400, "watch is paused")
    out = await process_watch(
        store, rec, adapter_factory=make_adapter,
        alert_url=(rec.alert_url
                   or settings.sahyog_mock_url.rstrip("/")
                   + "/sahyog/webhook/watch-alert"),
        secret=settings.engine_webhook_secret)
    return {"watch_id": str(watch_id), **out}
