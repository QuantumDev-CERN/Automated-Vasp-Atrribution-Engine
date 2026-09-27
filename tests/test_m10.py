"""M10 tests: watchlist store, watcher diffing, alert delivery, API.

All offline: stub adapters, MemoryStore, httpx MockTransport.
"""
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.store import MemoryStore, WatchIn
from engine.store.base import AlertRec
from engine.watch import (
    build_alert_payload, check_watch, deliver_watch_alert, process_watch,
)
from engine.watch.watcher import WatchEvent


def _tx(tx_hash, src, dst, value="100"):
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)])


class StubAdapter(ChainAdapter):
    chain = Chain.ETHEREUM

    def __init__(self, txs):
        super().__init__()
        self._txs = txs

    async def get_transactions(self, address, limit=100):
        return [t for t in self._txs
                if any(p.address.lower() == address.lower()
                       for p in t.inputs + t.outputs)][:limit]

    async def get_token_transfers(self, address, limit=100):
        return []

    async def health_check(self):
        return True


async def _watch(store, **kw):
    kw.setdefault("address", "0xwatched")
    kw.setdefault("chain", "ethereum")
    return await store.add_watch(WatchIn(**kw))


# ---------- store ----------

async def test_watch_crud_and_alerts():
    store = MemoryStore()
    w = await _watch(store, label="person of interest")
    assert w.status == "active"
    assert (await store.get_watch(w.id)).label == "person of interest"
    assert len(await store.list_watches()) == 1

    await store.set_watch(w.id, "paused")
    assert (await store.get_watch(w.id)).status == "paused"
    assert await store.list_watches() == []
    assert len(await store.list_watches(active_only=False)) == 1

    alert = AlertRec(
        id=uuid.uuid4(), watch_id=w.id, tx_hash="0xh1", direction="out",
        counterparty="0xevil", value="100", asset="ETH", vasp_hit=None,
        delivered=True, created_at=datetime.now(timezone.utc))
    await store.record_alert(alert)
    alerts = await store.list_alerts(w.id)
    assert len(alerts) == 1 and alerts[0].tx_hash == "0xh1"

    await store.remove_watch(w.id)
    assert await store.get_watch(w.id) is None


# ---------- watcher ----------

async def test_first_tick_is_baseline_no_events():
    store = MemoryStore()
    w = await _watch(store)
    txs = [_tx("0xh1", "0xwatched", "0xaaa"), _tx("0xh2", "0xbbb", "0xwatched")]
    check = await check_watch(w, lambda c: StubAdapter(txs))
    assert check.baseline is True
    assert check.events == []
    assert set(check.seen_hashes) == {"0xh1", "0xh2"}
    # persist the cursor like the tick does
    await store.set_watch(w.id, "active", seen_hashes=check.seen_hashes,
                          last_checked_at=check.checked_at)
    w2 = await store.get_watch(w.id)
    check2 = await check_watch(w2, lambda c: StubAdapter(txs))
    assert check2.baseline is False
    assert check2.events == []  # nothing new


async def test_new_movements_become_events_with_direction():
    store = MemoryStore()
    w = await _watch(store)
    old = [_tx("0xh1", "0xwatched", "0xaaa")]
    c1 = await check_watch(w, lambda c: StubAdapter(old))
    await store.set_watch(w.id, "active", seen_hashes=c1.seen_hashes,
                          last_checked_at=c1.checked_at)
    w = await store.get_watch(w.id)

    new = old + [_tx("0xh2", "0xwatched", "0xbbb", value="250"),
                 _tx("0xh3", "0xccc", "0xwatched", value="50")]
    check = await check_watch(w, lambda c: StubAdapter(new))
    assert len(check.events) == 2
    by_hash = {e.tx_hash: e for e in check.events}
    assert by_hash["0xh2"].direction == "out"
    assert by_hash["0xh2"].counterparty == "0xbbb"
    assert by_hash["0xh2"].value == "250"
    assert by_hash["0xh3"].direction == "in"
    assert by_hash["0xh3"].counterparty == "0xccc"


async def test_vasp_hit_flagged_via_resolver():
    store = MemoryStore()
    w = await _watch(store)
    c1 = await check_watch(w, lambda c: StubAdapter([]))
    await store.set_watch(w.id, "active", seen_hashes=c1.seen_hashes,
                          last_checked_at=c1.checked_at)
    w = await store.get_watch(w.id)
    txs = [_tx("0xh9", "0xwatched", "0xexchangehot")]
    check = await check_watch(
        w, lambda c: StubAdapter(txs),
        vasp_resolver=lambda a: "CoinDCX" if a == "0xexchangehot" else None)
    assert check.events[0].vasp_hit == "CoinDCX"


# ---------- alert delivery ----------

def _event() -> WatchEvent:
    return WatchEvent(
        watch_id=uuid.uuid4(), tx_hash="0xh9", direction="out",
        counterparty="0xexchangehot", value="250", asset="ETH",
        vasp_hit="CoinDCX")


async def test_alert_signed_and_idempotent():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sig"] = request.headers.get("X-Engine-Signature")
        seen["idem"] = request.headers.get("X-Idempotency-Key")
        seen["body"] = request.content
        return httpx.Response(200, json={"ack": True})

    class FakeWatch:
        id = uuid.uuid4()
        label = "poi"
        address = "0xwatched"
        chain = "ethereum"
        alert_url = "http://x/hook"

    event = _event()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await deliver_watch_alert(
            event, FakeWatch(), secret="s3cret", client=client)
    assert result.ok and result.attempts == 1
    assert seen["sig"].startswith("sha256=")
    assert seen["idem"] == f"{event.watch_id}:0xh9"

    # the signature actually verifies against the body
    import hashlib
    import hmac
    expected = "sha256=" + hmac.new(
        b"s3cret", seen["body"], hashlib.sha256).hexdigest()
    assert hmac.compare_digest(seen["sig"], expected)

    payload = build_alert_payload(event, FakeWatch())
    assert "CoinDCX" in payload["alert"]


async def test_alert_4xx_does_not_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "nope"})

    class FakeWatch:
        id = uuid.uuid4()
        label = ""
        address = "0xwatched"
        chain = "ethereum"
        alert_url = "http://x/hook"

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await deliver_watch_alert(
            _event(), FakeWatch(), secret="s", client=client)
    assert not result.ok and result.attempts == 1 and len(calls) == 1


# ---------- process_watch (delivery stubbed) ----------

async def test_process_watch_records_and_delivers(monkeypatch):
    from engine.watch import watcher as watcher_mod

    store = MemoryStore()
    w = await _watch(store)
    txs = [_tx("0xh1", "0xwatched", "0xaaa")]

    async def fake_check(watch, adapter_factory, **kw):
        return await check_watch(watch, lambda c: StubAdapter(txs), **kw)

    delivered = []

    async def fake_deliver(event, watch, *, secret, client=None):
        delivered.append(event.tx_hash)

        class R:
            ok = True
        return R()

    monkeypatch.setattr(watcher_mod, "check_watch", fake_check)
    monkeypatch.setattr(watcher_mod, "deliver_watch_alert", fake_deliver)

    out1 = await process_watch(store, w, adapter_factory=lambda c: None,
                               alert_url="http://x/hook", secret="s")
    assert out1["baseline"] is True and out1["events"] == []

    w = await store.get_watch(w.id)
    txs.append(_tx("0xh2", "0xwatched", "0xbbb"))
    out2 = await process_watch(store, w, adapter_factory=lambda c: None,
                               alert_url="http://x/hook", secret="s")
    assert len(out2["events"]) == 1
    assert delivered == ["0xh2"]
    alerts = await store.list_alerts(w.id)
    assert len(alerts) == 1 and alerts[0].delivered is True


# ---------- API ----------

def _api_app(store: MemoryStore) -> FastAPI:
    from api.routers.watchlist import router as watchlist_router

    app = FastAPI()
    app.state.store = store
    app.include_router(watchlist_router)
    return app


def test_api_crud():
    store = MemoryStore()
    client = TestClient(_api_app(store))
    r = client.post("/watchlist", json={
        "address": "0xwatched", "chain": "ethereum", "label": "poi"})
    assert r.status_code == 201
    wid = r.json()["watch_id"]

    assert client.get("/watchlist").json()["watches"][0]["label"] == "poi"
    assert client.get(f"/watchlist/{wid}").status_code == 200

    r = client.patch(f"/watchlist/{wid}", json={"status": "paused"})
    assert r.json()["status"] == "paused"
    assert client.get("/watchlist").json()["watches"] == []

    r = client.delete(f"/watchlist/{wid}")
    assert r.json()["removed"] is True
    assert client.get(f"/watchlist/{wid}").status_code == 404


def test_api_check_now_uses_baseline_then_events(monkeypatch):
    import engine.jobs.pipeline as pipeline_mod

    store = MemoryStore()
    txs = [_tx("0xh1", "0xwatched", "0xaaa")]

    monkeypatch.setattr(pipeline_mod, "make_adapter",
                        lambda chain: StubAdapter(txs))

    client = TestClient(_api_app(store))
    wid = client.post("/watchlist", json={
        "address": "0xwatched", "chain": "ethereum"}).json()["watch_id"]

    # delivery goes to the mock SAHYOG URL — stub it at the watcher level
    from engine.watch import watcher as watcher_mod

    async def fake_deliver(event, watch, *, secret, client=None):
        class R:
            ok = True
        return R()

    monkeypatch.setattr(watcher_mod, "deliver_watch_alert", fake_deliver)

    r = client.post(f"/watchlist/{wid}/check")
    assert r.json()["baseline"] is True

    txs.append(_tx("0xh2", "0xbbb", "0xwatched"))
    r = client.post(f"/watchlist/{wid}/check")
    body = r.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["direction"] == "in"

    alerts = client.get(f"/watchlist/{wid}/alerts").json()["alerts"]
    assert len(alerts) == 1 and alerts[0]["tx_hash"] == "0xh2"
