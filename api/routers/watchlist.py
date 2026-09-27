"""Watchlist API (M10).

  POST /watchlist                  — subscribe an address
  GET  /watchlist                  — list subscriptions
  GET  /watchlist/{watch_id}       — one subscription
  PATCH /watchlist/{watch_id}      — pause/resume {status}
  DELETE /watchlist/{watch_id}     — unsubscribe
  GET  /watchlist/{watch_id}/alerts — detected movements
  POST /watchlist/{watch_id}/check  — run one check cycle now
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.core.auth import require_cap
from engine.store import WatchIn

router = APIRouter(prefix="/watchlist", tags=["watchlist"],
                   dependencies=[Depends(require_cap("read"))])

_write = Depends(require_cap("write"))


class WatchSubmit(BaseModel):
    address: str
    chain: str = Field(..., examples=["ethereum", "bitcoin", "tron"])
    label: str = ""
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
        "last_checked_at": (w.last_checked_at.isoformat()
                            if w.last_checked_at else None),
        "created_at": w.created_at.isoformat(),
    }


@router.post("", status_code=201, dependencies=[_write])
async def add_watch(payload: WatchSubmit, request: Request) -> dict:
    rec = await request.app.state.store.add_watch(WatchIn(
        address=payload.address, chain=payload.chain, label=payload.label,
        case_id=payload.case_id, alert_url=payload.alert_url,
        created_by=payload.created_by))
    return {"watch_id": str(rec.id), "status": rec.status}


@router.get("")
async def list_watches(request: Request,
                       active_only: bool = True) -> dict:
    recs = await request.app.state.store.list_watches(active_only=active_only)
    return {"watches": [_dump(r) for r in recs]}


@router.get("/{watch_id}")
async def get_watch(watch_id: UUID, request: Request) -> dict:
    rec = await request.app.state.store.get_watch(watch_id)
    if rec is None:
        raise HTTPException(404, "watch not found")
    return _dump(rec)


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
