"""M12: audit middleware — every API action lands in the durable audit
trail with who did it, what they touched, and the outcome.

Skipped for health/ready/docs. Denied requests (401/403 raised by the
auth dependencies) are logged too, so failed access attempts are
visible to auditors.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

_SKIP_PREFIXES = ("/health", "/ready", "/docs", "/openapi.json", "/redoc")

_TARGET_PARAMS = ("case_id", "job_id", "report_id", "watch_id", "user_id")


def _action_of(request: Request) -> str:
    route = request.scope.get("route")
    path = route.path if route is not None else request.url.path
    return f"{request.method} {path}"


def _target_of(request: Request) -> tuple[str, str]:
    params = request.path_params
    for name in _TARGET_PARAMS:
        if name in params:
            return name.removesuffix("_id"), str(params[name])
    return "", ""


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith(_SKIP_PREFIXES):
            return await call_next(request)

        outcome = ""
        try:
            response = await call_next(request)
            outcome = str(response.status_code)
        except HTTPException as exc:
            outcome = str(exc.status_code)
            raise
        except Exception:
            outcome = "500"
            raise
        finally:
            try:
                await self._log(request, outcome)
            except Exception:
                # audit must never break the request it observes
                import traceback
                traceback.print_exc()
        return response

    async def _log(self, request: Request, outcome: str) -> None:
        store = request.app.state.store
        user = getattr(request.state, "user", None)
        target_type, target_id = _target_of(request)
        jurisdiction = ""
        if target_type == "case" and target_id:
            try:
                from uuid import UUID

                case = await store.get_case(UUID(target_id))
                if case is not None:
                    jurisdiction = case.jurisdiction
            except Exception:
                pass
        from engine.store.base import AuditEventIn

        await store.log_audit(AuditEventIn(
            user_id=user.id if user else None,
            user_name=user.name if user else "anonymous",
            action=_action_of(request),
            target_type=target_type, target_id=target_id,
            jurisdiction=jurisdiction,
            ip=request.client.host if request.client else "",
            outcome=outcome,
        ))
