"""Admin API (M12): user/API-key management and the audit trail.

  POST   /admin/users        — create user, returns the raw API key ONCE
  GET    /admin/users        — list users (key hashes never exposed)
  DELETE /admin/users/{id}   — revoke a user's key
  GET    /admin/audit        — query the audit trail

User management needs the manage_users capability (admin).
Reading the audit trail needs the audit capability (auditor, admin).
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.core.auth import get_current_user, require_cap
from engine.auth import hash_key, new_api_key
from engine.store.base import VALID_ROLES, ApiUserIn, ApiUserRec

router = APIRouter(prefix="/admin", tags=["admin"])

_manage = Depends(require_cap("manage_users"))
_audit_cap = Depends(require_cap("audit"))


class UserCreate(BaseModel):
    name: str
    role: str = Field(..., examples=["viewer", "analyst", "auditor", "admin"])
    jurisdictions: list[str] = Field(default_factory=lambda: ["IN"])


def _dump(u: ApiUserRec) -> dict:
    return {
        "user_id": str(u.id),
        "name": u.name,
        "role": u.role,
        "jurisdictions": u.jurisdictions,
        "active": u.active,
        "created_at": u.created_at.isoformat(),
    }


@router.post("/users", status_code=201, dependencies=[_manage])
async def create_user(payload: UserCreate, request: Request) -> dict:
    if payload.role not in VALID_ROLES:
        raise HTTPException(
            400, f"role must be one of {', '.join(VALID_ROLES)}")
    raw_key = new_api_key()
    rec = await request.app.state.store.create_user(
        ApiUserIn(name=payload.name, role=payload.role,
                  jurisdictions=payload.jurisdictions),
        key_hash=hash_key(raw_key))
    return {**_dump(rec), "api_key": raw_key,
            "warning": "store this key now — it is never shown again"}


@router.get("/users", dependencies=[_manage])
async def list_users(request: Request) -> dict:
    users = await request.app.state.store.list_users()
    return {"users": [_dump(u) for u in users]}


@router.delete("/users/{user_id}", dependencies=[_manage])
async def revoke_user(user_id: UUID, request: Request,
                      me: ApiUserRec = Depends(get_current_user)) -> dict:
    if user_id == me.id:
        raise HTTPException(400, "you cannot revoke your own key")
    if await request.app.state.store.get_user(user_id) is None:
        raise HTTPException(404, "user not found")
    await request.app.state.store.revoke_user(user_id)
    return {"user_id": str(user_id), "revoked": True}


@router.get("/audit", dependencies=[_audit_cap])
async def query_audit(request: Request, limit: int = 100,
                      user_id: UUID | None = None,
                      action: str | None = None) -> dict:
    events = await request.app.state.store.list_audit_events(
        limit=min(limit, 1000), user_id=user_id, action=action)
    return {"events": [{
        "event_id": str(e.id),
        "user_id": str(e.user_id) if e.user_id else None,
        "user_name": e.user_name,
        "action": e.action,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "jurisdiction": e.jurisdiction,
        "ip": e.ip,
        "outcome": e.outcome,
        "created_at": e.created_at.isoformat(),
    } for e in events]}
