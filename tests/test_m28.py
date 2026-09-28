"""M28: SAHYOG acknowledgement reference — contract completion.

The mock receiver now issues a real ack_ref; the delivery layer
captures it; the worker persists it on the filing; the filings API
and the frontend filing detail render it. All offline.
"""
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from engine.delivery.webhook import DeliveryResult, deliver_attribution
from engine.store.base import FilingIn
from engine.store.memory import MemoryStore
from integrations.sahyog_mock.mock_server import app as mock_app


class _Fake:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _objs():
    now = datetime.now(timezone.utc)
    report = _Fake(id=uuid.uuid4(), report_hash="rh" * 32, inputs_hash="ih",
                   generated_at=now, engine_version="test")
    case = _Fake(id=uuid.uuid4())
    job = _Fake(id=uuid.uuid4(), address="0xabc", chain="ethereum")
    return report, case, job


def _client_for(payload=None, status=200, raw=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=payload or {})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_deliver_attribution_captures_ack_ref():
    report, case, job = _objs()
    client = _client_for({"ack": True, "ack_ref": "MOCK-ABC123"})
    result = await deliver_attribution(
        report, case, job, base_url="http://mock", secret="s",
        client=client)
    await client.aclose()
    assert isinstance(result, DeliveryResult)
    assert result.ok and result.ack_ref == "MOCK-ABC123"


async def test_deliver_attribution_missing_ack_ref_is_none():
    report, case, job = _objs()
    client = _client_for({"ack": True})
    result = await deliver_attribution(
        report, case, job, base_url="http://mock", secret="s",
        client=client)
    await client.aclose()
    assert result.ok and result.ack_ref is None


async def test_deliver_attribution_non_json_ack_is_none():
    report, case, job = _objs()
    client = _client_for(status=200, raw=b"OK")
    result = await deliver_attribution(
        report, case, job, base_url="http://mock", secret="s",
        client=client)
    await client.aclose()
    assert result.ok and result.ack_ref is None


def test_mock_server_issues_ack_ref():
    from fastapi.testclient import TestClient
    with TestClient(mock_app) as tc:
        resp = tc.post("/sahyog/webhook/attribution",
                       json={"case_id": "x", "status": "attributed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ack"] is True
    assert body["ack_ref"].startswith("MOCK-")
    assert len(body["ack_ref"]) > len("MOCK-")


async def test_filing_persists_ack_ref():
    store = MemoryStore()
    case_id, report_id = uuid.uuid4(), uuid.uuid4()
    rec = await store.record_filing(FilingIn(
        case_id=case_id, report_id=report_id, status="delivered",
        ack_ref="MOCK-XYZ789", attempts=1))
    fetched = await store.get_filing(rec.id)
    assert fetched is not None and fetched.ack_ref == "MOCK-XYZ789"
    recs, total = await store.list_filings()
    assert total == 1 and recs[0].ack_ref == "MOCK-XYZ789"


async def test_resend_persists_new_ack_ref(monkeypatch):
    """The resend endpoint records a NEW filing row carrying the fresh
    ack_ref from the re-delivery (audit trail, original untouched)."""
    from fastapi import Request

    from engine.delivery import webhook as delivery_mod
    from engine.store.base import ReportIn

    store = MemoryStore()
    case_id = uuid.uuid4()
    job = await store.create_job(case_id, "0x0", "ethereum")
    report = await store.save_report(ReportIn(
        job_id=job.id, case_id=case_id, report_text="t",
        report_hash="h" * 64, inputs_hash="i",
        generated_at=datetime.now(timezone.utc), engine_version="t",
        certificate_statement="c", risk_score=10, risk_level="low",
        confidence=0.5, terminal_address="0x1",
        terminal_reason="dead-end"))
    orig = await store.record_filing(FilingIn(
        case_id=case_id, report_id=report.id, status="failed",
        error="boom", attempts=3))

    class _Res:
        ok = True
        attempts = 1
        error = ""
        ack_ref = "MOCK-RESEND1"

    async def _fake_deliver(*a, **k):
        return _Res()

    # NB: the router does `from engine.delivery import deliver_attribution`
    # at call time, so the package namespace is the patch target — not
    # the webhook submodule.
    from engine import delivery as delivery_pkg
    monkeypatch.setattr(delivery_pkg, "deliver_attribution",
                        _fake_deliver)

    scope = {"type": "http", "app": _Fake(state=_Fake(store=store))}
    request = Request(scope)

    class _User:
        name = "tester"

        def can_access(self, jurisdiction):
            return True

    from api.routers.filings import resend_filing
    out = await resend_filing(orig.id, request, _User())
    assert out["status"] == "delivered"
    assert out["resent_from"] == str(orig.id)
    new_rec = await store.get_filing(uuid.UUID(out["filing_id"]))
    assert new_rec is not None
    assert new_rec.ack_ref == "MOCK-RESEND1"
    assert new_rec.id != orig.id
    # original row untouched
    still = await store.get_filing(orig.id)
    assert still.status == "failed" and not still.ack_ref
