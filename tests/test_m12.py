"""M12 tests: RBAC roles, jurisdiction scoping, audit trail.

Non-enforced mode (default) keeps working without keys; enforced mode
requires X-API-Key. All in-memory, no network.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import config as config_mod
from api.core.audit import AuditMiddleware
from api.routers import admin as admin_router_mod
from api.routers import cases as cases_router_mod
from api.routers import watchlist as watchlist_router_mod
from engine.auth import hash_key
from engine.store import ApiUserIn, CaseIn, MemoryStore


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "auth_enforced", True)


def _app(store: MemoryStore) -> FastAPI:
    app = FastAPI()
    app.state.store = store
    app.include_router(cases_router_mod.router)
    app.include_router(watchlist_router_mod.router)
    app.include_router(admin_router_mod.router)
    app.add_middleware(AuditMiddleware)
    return app


async def _mkuser(store: MemoryStore, name: str, role: str,
                  jurisdictions=("IN",)) -> tuple[str, object]:
    raw = f"vea_test_{name}_{role}"
    rec = await store.create_user(
        ApiUserIn(name=name, role=role, jurisdictions=list(jurisdictions)),
        key_hash=hash_key(raw))
    return raw, rec


def _headers(raw: str | None) -> dict:
    return {"X-API-Key": raw} if raw else {}


# ---------- user lifecycle ----------

async def test_user_lifecycle_and_revocation():
    store = MemoryStore()
    raw, rec = await _mkuser(store, "ra", "analyst")
    found = await store.get_user_by_key_hash(hash_key(raw))
    assert found is not None and found.name == "ra" and found.active
    assert found.has_cap("write") and not found.has_cap("audit")
    assert found.can_access("IN") and not found.can_access("US")

    await store.revoke_user(rec.id)
    assert (await store.get_user(rec.id)).active is False
    assert len(await store.list_users()) == 1


async def test_role_capabilities():
    store = MemoryStore()
    for role, caps in (("viewer", {"read"}), ("analyst", {"read", "write"}),
                       ("auditor", {"read", "audit"}),
                       ("admin", {"read", "write", "audit",
                                 "manage_users"})):
        _, rec = await _mkuser(store, f"u_{role}", role)
        assert {c for c in ("read", "write", "audit", "manage_users")
                if rec.has_cap(c)} == caps


# ---------- enforcement ----------

async def test_enforced_mode_requires_key(enforced):
    store = MemoryStore()
    client = TestClient(_app(store))
    r = client.get(f"/cases/{uuid.uuid4()}")
    assert r.status_code == 401

    raw, _ = await _mkuser(store, "viewer1", "viewer")
    r = client.get(f"/cases/{uuid.uuid4()}", headers=_headers(raw))
    assert r.status_code == 404  # authenticated, case just doesn't exist


async def test_non_enforced_mode_still_works_without_key():
    store = MemoryStore()
    client = TestClient(_app(store))
    r = client.post("/cases", json={
        "fir_number": "FIR/1", "suspect_address": "0xa",
        "chain": "ethereum"})
    assert r.status_code == 201
    events = await store.list_audit_events()
    assert events[0].user_name == "system"
    assert events[0].action == "POST /cases"
    assert events[0].outcome == "201"

    # routes with an ID in the path get target + jurisdiction attribution
    cid = r.json()["case_id"]
    client.get(f"/cases/{cid}")
    events = await store.list_audit_events()
    assert events[0].action == "GET /cases/{case_id}"
    assert events[0].target_type == "case"
    assert events[0].target_id == cid
    assert events[0].jurisdiction == "IN"


# ---------- role caps ----------

async def test_viewer_cannot_write_but_can_read(enforced):
    store = MemoryStore()
    client = TestClient(_app(store))
    vraw, _ = await _mkuser(store, "v", "viewer")
    araw, _ = await _mkuser(store, "a", "analyst")

    r = client.post("/cases", headers=_headers(vraw), json={
        "fir_number": "FIR/2", "suspect_address": "0xb",
        "chain": "ethereum"})
    assert r.status_code == 403

    r = client.post("/cases", headers=_headers(araw), json={
        "fir_number": "FIR/2", "suspect_address": "0xb",
        "chain": "ethereum"})
    assert r.status_code == 201
    cid = r.json()["case_id"]

    r = client.get(f"/cases/{cid}", headers=_headers(vraw))
    assert r.status_code == 200

    r = client.post("/watchlist", headers=_headers(vraw), json={
        "address": "0xb", "chain": "ethereum"})
    assert r.status_code == 403


# ---------- jurisdiction ----------

async def test_jurisdiction_scoping(enforced):
    store = MemoryStore()
    client = TestClient(_app(store))
    in_raw, _ = await _mkuser(store, "in_analyst", "analyst", ("IN",))
    admin_raw, _ = await _mkuser(store, "boss", "admin", ("*",))

    us_case = await store.create_case(CaseIn(
        fir_number="FIR/US", suspect_address="0xus", chain="ethereum",
        jurisdiction="US"))

    # IN analyst cannot see the US case
    r = client.get(f"/cases/{us_case.id}", headers=_headers(in_raw))
    assert r.status_code == 403
    # ... nor create one there
    r = client.post("/cases", headers=_headers(in_raw), json={
        "fir_number": "FIR/3", "suspect_address": "0xc",
        "chain": "ethereum", "jurisdiction": "US"})
    assert r.status_code == 403
    # admin with global scope can
    r = client.get(f"/cases/{us_case.id}", headers=_headers(admin_raw))
    assert r.status_code == 200
    assert r.json()["jurisdiction"] == "US"


# ---------- admin: users + audit ----------

async def test_admin_user_management(enforced):
    store = MemoryStore()
    client = TestClient(_app(store))
    admin_raw, admin_rec = await _mkuser(store, "root", "admin", ("*",))
    analyst_raw, _ = await _mkuser(store, "op", "analyst")

    # analyst cannot manage users
    r = client.post("/admin/users", headers=_headers(analyst_raw), json={
        "name": "newbie", "role": "viewer", "jurisdictions": ["IN"]})
    assert r.status_code == 403

    # bad role rejected
    r = client.post("/admin/users", headers=_headers(admin_raw), json={
        "name": "bad", "role": "superuser", "jurisdictions": ["IN"]})
    assert r.status_code == 400

    # admin creates a user: raw key returned once
    r = client.post("/admin/users", headers=_headers(admin_raw), json={
        "name": "newbie", "role": "viewer", "jurisdictions": ["IN"]})
    assert r.status_code == 201
    body = r.json()
    new_key = body["api_key"]
    assert new_key.startswith("vea_")
    new_id = body["user_id"]

    # the new key works
    r = client.get(f"/cases/{uuid.uuid4()}", headers=_headers(new_key))
    assert r.status_code == 404

    # listing never exposes hashes
    r = client.get("/admin/users", headers=_headers(admin_raw))
    assert all("key_hash" not in u and "api_key" not in u
               for u in r.json()["users"])

    # cannot revoke yourself
    r = client.delete(f"/admin/users/{admin_rec.id}",
                      headers=_headers(admin_raw))
    assert r.status_code == 400

    # revoke the new user: their key dies immediately
    r = client.delete(f"/admin/users/{new_id}", headers=_headers(admin_raw))
    assert r.json()["revoked"] is True
    r = client.get(f"/cases/{uuid.uuid4()}", headers=_headers(new_key))
    assert r.status_code == 401


async def test_audit_trail_query(enforced):
    store = MemoryStore()
    client = TestClient(_app(store))
    auditor_raw, _ = await _mkuser(store, "eye", "auditor")
    viewer_raw, _ = await _mkuser(store, "v2", "viewer")
    analyst_raw, _ = await _mkuser(store, "a2", "analyst")

    r = client.post("/cases", headers=_headers(analyst_raw), json={
        "fir_number": "FIR/9", "suspect_address": "0xd9",
        "chain": "ethereum"})
    cid = r.json()["case_id"]
    client.get(f"/cases/{cid}", headers=_headers(viewer_raw))
    client.get(f"/cases/{cid}", headers={})  # 401, still audited

    # viewer cannot read the audit trail
    r = client.get("/admin/audit", headers=_headers(viewer_raw))
    assert r.status_code == 403

    r = client.get("/admin/audit", headers=_headers(auditor_raw))
    assert r.status_code == 200
    events = r.json()["events"]
    actions = {(e["action"], e["outcome"]) for e in events}
    assert ("POST /cases", "201") in actions
    assert ("GET /cases/{case_id}", "200") in actions
    assert ("GET /cases/{case_id}", "401") in actions
    denied = [e for e in events
              if e["outcome"] == "401"][0]
    assert denied["user_name"] == "anonymous"

    # filtering works
    r = client.get("/admin/audit?action=POST /cases",
                   headers=_headers(auditor_raw))
    assert all(e["action"] == "POST /cases"
               for e in r.json()["events"])
