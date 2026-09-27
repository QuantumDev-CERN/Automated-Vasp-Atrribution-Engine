"""M12: FastAPI auth dependencies.

- get_current_user: X-API-Key -> ApiUserRec. When auth is NOT enforced
  (default), a missing/invalid key falls back to a synthetic system
  admin so local runs and existing tests keep working; every action is
  still audit-logged under that identity. When AUTH_ENFORCED=1, a valid
  active key is required.
- require_cap("read"|"write"|"audit"|"manage_users"): 403 unless the
  caller's role implies the capability.
- assert_case_access: 403 when the caller's jurisdictions don't cover
  the case's jurisdiction.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request

from api.core.config import settings
from engine.auth import hash_key
from engine.store.base import ApiUserRec, CaseRec

_SYSTEM_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def system_user() -> ApiUserRec:
    return ApiUserRec(
        id=_SYSTEM_USER_ID, name="system", role="admin",
        jurisdictions=["*"],
        key_hash="", created_at=datetime.now(timezone.utc))


async def get_current_user(request: Request) -> ApiUserRec:
    raw = request.headers.get("X-API-Key", "")
    user: ApiUserRec | None = None
    if raw:
        user = await request.app.state.store.get_user_by_key_hash(
            hash_key(raw))
        if user is not None and not user.active:
            user = None
    if user is None:
        if settings.auth_enforced:
            raise HTTPException(
                401, "missing or invalid API key (X-API-Key)",
                headers={"WWW-Authenticate": "ApiKey"})
        user = system_user()
    request.state.user = user
    return user


def require_cap(cap: str):
    async def _dep(user: ApiUserRec = Depends(get_current_user)) -> ApiUserRec:
        if not user.has_cap(cap):
            raise HTTPException(
                403, f"role '{user.role}' lacks capability '{cap}'")
        return user

    return _dep


def assert_jurisdiction(user: ApiUserRec, jurisdiction: str) -> None:
    if not user.can_access(jurisdiction):
        raise HTTPException(
            403, f"jurisdiction '{jurisdiction}' outside your scope "
                 f"({', '.join(user.jurisdictions)})")


def assert_case_access(user: ApiUserRec, case: CaseRec) -> None:
    assert_jurisdiction(user, case.jurisdiction)
