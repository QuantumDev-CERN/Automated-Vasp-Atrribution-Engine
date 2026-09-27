"""Mock SAHYOG server.

Stands in for the real SAHYOG portal until real API access exists.
Two jobs:
  1. Accept case submissions (what our engine's intake API will mirror).
  2. Receive webhook callbacks from our engine as traces resolve.

When the engine signs webhooks (X-Engine-Signature), the mock verifies
the HMAC against ENGINE_WEBHOOK_SECRET — proving the signing path works
end to end. Unsigned webhooks are still accepted (lenient mode).

Run: uvicorn integrations.sahyog_mock.mock_server:app --port 8091
"""
import hashlib
import hmac
import os
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="SAHYOG (mock)")

_cases: dict[str, dict] = {}
_webhooks: list[dict] = []
_seen_idempotency_keys: set[str] = set()


class CaseSubmit(BaseModel):
    fir_number: str = Field(..., examples=["FIR/2026/001234"])
    suspect_address: str
    chain: str = Field(..., examples=["ethereum", "tron", "bitcoin"])
    officer_id: str = "demo-officer"
    notes: str = ""


@app.post("/sahyog/cases")
async def submit_case(payload: CaseSubmit) -> dict:
    case_id = f"CASE-{uuid4().hex[:8].upper()}"
    _cases[case_id] = {
        "case_id": case_id,
        **payload.model_dump(),
        "status": "received",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"case_id": case_id, "status": "received"}


@app.get("/sahyog/cases/{case_id}")
async def get_case(case_id: str) -> dict:
    if case_id not in _cases:
        return {"error": "case not found"}
    return _cases[case_id]


@app.post("/sahyog/webhook/attribution")
async def receive_attribution(request: Request) -> JSONResponse:
    """Our engine POSTs trace results here as they resolve (M7)."""
    body = await request.body()
    sig = request.headers.get("X-Engine-Signature")
    if sig:
        secret = os.environ.get("ENGINE_WEBHOOK_SECRET",
                                "dev-webhook-secret-change-me")
        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return JSONResponse({"error": "bad signature"}, status_code=401)

    idem = request.headers.get("X-Idempotency-Key")
    if idem and idem in _seen_idempotency_keys:
        return JSONResponse({"ack": True, "duplicate": True,
                             "webhooks_received": len(_webhooks)})
    if idem:
        _seen_idempotency_keys.add(idem)

    payload = await request.json()
    _webhooks.append(
        {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
    )
    case_id = payload.get("case_id")
    if case_id in _cases:
        _cases[case_id]["status"] = payload.get("status", "attributed")
    return JSONResponse({"ack": True,
                         "webhooks_received": len(_webhooks)})


@app.get("/sahyog/webhooks")
async def list_webhooks() -> dict:
    return {"count": len(_webhooks), "webhooks": _webhooks}
