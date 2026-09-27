"""M10: watch-alert delivery.

Same signed-webhook discipline as the M7 attribution delivery:
HMAC-SHA256 in X-Engine-Signature, X-Idempotency-Key = watch + tx so a
redelivered tick can't double-alert. 4xx = receiver rejected (no
retry); network errors / 5xx retry with backoff.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Optional
from uuid import UUID

import httpx

from ..delivery.webhook import DeliveryResult, sign_body

if TYPE_CHECKING:
    from .watcher import WatchEvent

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 8)


def build_alert_payload(event: WatchEvent, watch) -> dict:
    return {
        "watch_id": str(event.watch_id),
        "label": watch.label,
        "address": watch.address,
        "chain": watch.chain,
        "tx_hash": event.tx_hash,
        "direction": event.direction,
        "counterparty": event.counterparty,
        "value": event.value,
        "asset": event.asset,
        "vasp_hit": event.vasp_hit,
        "block_time": event.block_time,
        "alert": ("WATCHED WALLET MOVED INTO VASP " + event.vasp_hit
                  if event.vasp_hit else "movement on watched wallet"),
    }


async def deliver_watch_alert(
    event: WatchEvent,
    watch,
    *,
    secret: str,
    client: Optional[httpx.AsyncClient] = None,
) -> DeliveryResult:
    body = json.dumps(build_alert_payload(event, watch),
                      sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Engine-Signature": sign_body(body, secret),
        "X-Idempotency-Key": f"{event.watch_id}:{event.tx_hash}",
    }
    url = watch.alert_url
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
                        f"receiver rejected alert: {resp.status_code}")
                last_error = f"server error: {resp.status_code}"
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(BACKOFF_SECONDS[attempt])
        return DeliveryResult(False, attempts, last_status, last_error)
    finally:
        if owned:
            await client.aclose()
