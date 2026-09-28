"""M26 read-API tests: the console's data contracts.

Covers GET /cases, GET /cases/{id}/latest, the filings register +
resend, watch check history + alert disposition, the ranked intel
feed, and GET /admin/users/me. Everything is served from the real
memory store — no fixtures in the API layer.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.store.base import (
    ApiUserIn, CaseIn, FilingIn, ReportIn, WatchCheckRec, WatchIn,
)
from engine.store.memory import MemoryStore

pytestmark = pytest.mark.asyncio


def _case_app(store):
    from api.routers.cases import router
    app = FastAPI()
    app.state.store = store
    app.include_router(router)
    return app


def _filing_app(store):
    from api.routers.filings import router
    app = FastAPI()
    app.state.store = store
    app.include_router(router)
    return app


def _watch_app(store):
    from api.routers.watchlist import router
    app = FastAPI()
    app.state.store = store
    app.include_router(router)
    return app


def _graph_app(store, graph_store):
    from api.routers.graph import router
    app = FastAPI()
    app.state.store = store
    app.state.graph_store = graph_store
    app.include_router(router)
    return app


def _admin_app(store):
    from api.routers.admin import router
    app = FastAPI()
    app.state.store = store
    app.include_router(router)
    return app


async def _seed_case_with_trace(store):
    case = await store.create_case(CaseIn(
        fir_number="EVAL/DEMO/001", suspect_address="0xabc",
        chain="ethereum", jurisdiction="IN",
        notes="evaluation trace — not a real investigation"))
    job = await store.create_job(case.id, "0xabc", "ethereum")
    report = await store.save_report(ReportIn(
        job_id=job.id, case_id=case.id, report_text="r",
        report_hash="h", inputs_hash="i",
        generated_at=datetime.now(timezone.utc),
        engine_version="t", certificate_statement="s",
        risk_score=42, risk_level="medium", confidence=0.7,
        terminal_address="0xterm", terminal_reason="dead-end",
        hop_count=3))
    return case, job, report


async def test_cases_list_and_latest():
    store = MemoryStore()
    case, job, report = await _seed_case_with_trace(store)
    client = TestClient(_case_app(store))

    r = client.get("/cases")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    row = body["cases"][0]
    assert row["fir_number"] == "EVAL/DEMO/001"
    assert row["latest"]["risk_score"] == 42
    assert row["latest"]["confidence"] == 0.7
    assert row["latest"]["terminal_address"] == "0xterm"

    r = client.get("/cases", params={"status": "received"})
    assert r.json()["total"] == 1
    r = client.get("/cases", params={"status": "attributed"})
    assert r.json()["total"] == 0
    r = client.get("/cases", params={"search": "EVAL/DEMO"})
    assert r.json()["total"] == 1

    r = client.get(f"/cases/{case.id}/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["job"]["job_id"] == str(job.id)
    assert body["report"]["report_id"] == str(report.id)
    assert body["report"]["hop_count"] == 3
    assert body["certificate"]["report_hash"] == "h"

    r = client.get(f"/cases/{uuid.uuid4()}/latest")
    assert r.status_code == 404


async def test_cases_list_empty_latest_null():
    store = MemoryStore()
    await store.create_case(CaseIn(
        fir_number="EVAL/DEMO/002", suspect_address="0xdef",
        chain="bitcoin", jurisdiction="IN",
        notes="evaluation — not a real investigation"))
    client = TestClient(_case_app(store))
    row = client.get("/cases").json()["cases"][0]
    assert row["latest"] is None


async def test_filings_register_and_resend(monkeypatch):
    from engine import delivery

    store = MemoryStore()
    case, job, report = await _seed_case_with_trace(store)
    filing = await store.record_filing(FilingIn(
        case_id=case.id, report_id=report.id, status="failed",
        error="boom", attempts=3))
    client = TestClient(_filing_app(store))

    r = client.get("/filings")
    assert r.status_code == 200
    row = r.json()["filings"][0]
    assert row["status"] == "failed"
    assert row["fir_number"] == "EVAL/DEMO/001"
    assert row["suspect_address"] == "0xabc"

    r = client.get(f"/filings/{filing.id}")
    assert r.status_code == 200
    assert r.json()["transmission"]["attempts"] == 3
    assert r.json()["report"]["risk_score"] == 42

    class _Res:
        ok = True
        attempts = 1
        error = ""
        ack_ref = "MOCK-TEST123"  # M28: DeliveryResult carries ack_ref

    async def _fake_deliver(*a, **k):
        return _Res()

    monkeypatch.setattr(delivery, "deliver_attribution", _fake_deliver)
    r = client.post(f"/filings/{filing.id}/resend")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "delivered"
    assert body["resent_from"] == str(filing.id)
    # original row untouched; a new row records the re-send
    assert (await store.get_filing(filing.id)).status == "failed"
    assert r.json()["filing_id"] != str(filing.id)


async def test_watch_checks_and_disposition():
    store = MemoryStore()
    watch = await store.add_watch(WatchIn(
        address="0xw", chain="ethereum", label="eval",
        classification="suspect"))
    await store.record_watch_check(WatchCheckRec(
        id=uuid.uuid4(), watch_id=watch.id,
        checked_at=datetime.now(timezone.utc),
        txs_seen=10, new_events=2, alerts_delivered=2))
    await store.record_watch_check(WatchCheckRec(
        id=uuid.uuid4(), watch_id=watch.id,
        checked_at=datetime.now(timezone.utc),
        error="AdapterError: timeout"))
    client = TestClient(_watch_app(store))

    r = client.get(f"/watchlist/{watch.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["classification"] == "suspect"
    assert body["lifetime_checks"] == 2
    assert len(body["check_history"]) == 2
    assert body["check_history"][0]["error"] == "AdapterError: timeout"

    r = client.get(f"/watchlist/{watch.id}/checks")
    assert r.status_code == 200
    assert len(r.json()["checks"]) == 2

    # disposition round-trip through the store + list surface
    from engine.store.base import AlertRec
    alert = await store.record_alert(AlertRec(
        id=uuid.uuid4(), watch_id=watch.id, tx_hash="0xt",
        direction="in", counterparty="0xc", value="1", asset="ETH",
        vasp_hit=None, delivered=True,
        created_at=datetime.now(timezone.utc)))
    r = client.patch(
        f"/watchlist/{watch.id}/alerts/{alert.id}",
        json={"disposition": "false_positive",
              "notes": "known exchange hot wallet"})
    assert r.status_code == 200
    assert r.json()["disposition"] == "false_positive"

    r = client.patch(
        f"/watchlist/{watch.id}/alerts/{alert.id}",
        json={"disposition": "bogus"})
    assert r.status_code == 400

    detail = client.get(f"/watchlist/{watch.id}").json()
    row = [a for a in detail["alerts"]
           if a["alert_id"] == str(alert.id)][0]
    assert row["disposition"] == "false_positive"
    assert row["disposition_by"] == "system"  # synthetic admin, auth off


async def test_ranked_intel_feed():
    from engine.graph.memory_store import MemoryGraphStore

    store = MemoryStore()
    gs = MemoryGraphStore()
    await gs.tag_address("0xmix1", "ethereum", "mixer-deposit",
                         source="traversal:c1", case_id="c1")
    await gs.tag_address("0xmix2", "ethereum", "mixer-deposit",
                         source="traversal:c2", case_id="c2")
    await gs.tag_address("0xsw1", "ethereum", "swap-service",
                         source="traversal:c3", case_id="c3")
    client = TestClient(_graph_app(store, gs))

    r = client.get("/intel/links/ranked")
    assert r.status_code == 200
    ranked = r.json()["ranked"]
    assert ranked[0]["tag"] == "mixer-deposit"
    assert ranked[0]["address_count"] == 2
    # never-invented tags never appear
    assert all(e["address_count"] > 0 for e in ranked)


async def test_admin_me_and_email():
    store = MemoryStore()
    rec = await store.create_user(
        ApiUserIn(name="op", email="op@example.com", role="admin",
                  jurisdictions=["*"]), key_hash="x")
    client = TestClient(_admin_app(store))

    r = client.get("/admin/users/me")
    assert r.status_code == 200
    assert r.json()["name"] == "system"  # synthetic admin, auth off
    assert r.json()["last_active"] is None  # honest null, not fabricated

    users = client.get("/admin/users").json()["users"]
    assert users[0]["email"] == "op@example.com"
    assert rec.email == "op@example.com"


async def test_loopback_webhook_delivery_bypasses_broken_proxy():
    """This environment's NO_PROXY contains '[::1]', which crashes
    httpx's proxy parsing — delivery to the loopback SAHYOG mock must
    still work (trust_env=False)."""
    import threading
    from datetime import datetime, timezone
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from types import SimpleNamespace
    from uuid import uuid4

    from engine.delivery.webhook import deliver_attribution

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["sig"] = self.headers.get("X-Engine-Signature")
            received["idem"] = self.headers.get("X-Idempotency-Key")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        report = SimpleNamespace(
            id=uuid4(), report_hash="rh", inputs_hash="ih",
            generated_at=datetime.now(timezone.utc), engine_version="t")
        case = SimpleNamespace(id=uuid4())
        job = SimpleNamespace(id=uuid4(), address="0xabc", chain="ethereum")
        out = await deliver_attribution(
            report, case, job,
            base_url=f"http://127.0.0.1:{srv.server_port}",
            secret="s3cret")
        assert out.ok, out.error
        assert received["sig"].startswith("sha256=")
        assert received["idem"] == "rh"
    finally:
        srv.shutdown()
