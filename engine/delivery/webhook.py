"""Webhook delivery to SAHYOG (M7).

When a trace job completes, the engine POSTs the attribution result to
the SAHYOG webhook endpoint. Three guarantees:

1. Authenticity: HMAC-SHA256 over the raw body, shared secret from
   config (settings.engine_webhook_secret). The mock SAHYOG verifies it.
2. Idempotency: X-Idempotency-Key = report_hash. Re-deliveries of the
   same report are deduplicated by the receiver.
3. Retry: network errors and 5xx are retried with backoff; 4xx is not
   (the payload is wrong, retrying won't help). arq also retries the
   whole job function on failure.
"""
import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass

import httpx

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0, 4.0)


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    attempts: int
    status_code: int | None
    error: str | None = None


def sign_body(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_payload(report, case, job) -> dict:
    return {
        "case_id": str(case.id),
        "job_id": str(job.id),
        "report_id": str(report.id),
        "status": "attributed",
        "report_hash": report.report_hash,
        "inputs_hash": report.inputs_hash,
        "generated_at": report.generated_at.isoformat(),
        "engine_version": report.engine_version,
        "subject": {"address": job.address, "chain": job.chain},
    }


async def deliver_attribution(report, case, job, *, base_url: str,
                              secret: str,
                              client: httpx.AsyncClient | None = None,
                              ) -> DeliveryResult:
    body = json.dumps(build_payload(report, case, job),
                      sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Engine-Signature": sign_body(body, secret),
        "X-Idempotency-Key": report.report_hash,
    }
    url = base_url.rstrip("/") + "/sahyog/webhook/attribution"

    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=15.0)
    try:
        attempts = 0
        last_status: int | None = None
        last_error: str | None = None
        for attempt in range(MAX_ATTEMPTS):
            attempts = attempt + 1
            try:
                resp = await client.post(url, content=body, headers=headers)
                last_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    return DeliveryResult(True, attempts, last_status)
                if 400 <= resp.status_code < 500:
                    return DeliveryResult(
                        False, attempts, last_status,
                        f"receiver rejected payload: {resp.status_code}")
                last_error = f"server error: {resp.status_code}"
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(BACKOFF_SECONDS[attempt])
        return DeliveryResult(False, attempts, last_status, last_error)
    finally:
        if owned:
            await client.aclose()
