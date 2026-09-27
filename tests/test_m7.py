"""M7 tests: sanctions intel, store, pipeline, webhook, intake API.

All offline: stub adapters, MemoryStore, MemoryQueue, and httpx mock
transports. The docker-backed integration path (real Redis/Postgres) is
covered by scripts/smoke_m7.py --integration, gated on env.
"""
from uuid import UUID

import httpx
import pytest

from engine.adapters.base import (
    Asset, AssetKind, CanonicalTx, Chain, ChainAdapter, FlowParty,
)
from engine.delivery import (
    build_payload, deliver_attribution, sign_body,
)
from engine.intel import SanctionsList
from engine.jobs import MemoryQueue, PipelineDeps, run_trace_pipeline
from engine.report import verify_report
from engine.scoring import score_risk
from engine.store import MemoryStore
from engine.store.base import CaseIn, ReportIn
from engine.vasp import CaseDetails


# ---------- sanctions intel ----------

def test_fixture_loads_real_ofac_entries():
    table = SanctionsList.from_fixture()
    assert len(table) == 8
    hit = table.lookup("0x252a8bd2319d8a555b872990601221b3a2053bce")
    assert hit is not None
    assert hit.currency == "ETH"
    assert hit.name == "MESRI Behzad"
    assert hit.source.startswith("OFAC")


def test_lookup_is_case_insensitive_for_evm():
    table = SanctionsList.from_fixture()
    lower = "0x252a8bd2319d8a555b872990601221b3a2053bce"
    assert table.lookup(lower.upper()) is not None
    assert table.lookup("0x" + "0" * 40) is None


def test_lookup_exact_for_non_evm():
    table = SanctionsList.from_fixture()
    assert table.lookup("12aNKp2iDKuhEde2YfPdd4DFGenRUTKupL") is not None
    assert table.lookup("12ankp2idkuhede2yfPdd4dfgenrutkupl") is None


def test_sanctions_hit_scores_fifty_and_critical_with_mixer():
    risk = score_risk([], sanctions_hits=("0xabc",))
    assert risk.total == 50 and risk.level == "high"
    assert risk.signals[0].name == "sanctions-list-hit"


# ---------- store ----------

async def _make_case(store: MemoryStore):
    return await store.create_case(CaseIn(
        fir_number="FIR/2026/001234", suspect_address="0xsubject",
        chain="ethereum", officer_id="off-1", notes="ransomware"))


@pytest.mark.asyncio
async def test_store_case_and_job_round_trip():
    store = MemoryStore()
    case = await _make_case(store)
    assert (await store.get_case(case.id)).fir_number == "FIR/2026/001234"
    await store.set_case_status(case.id, "attributed")
    assert (await store.get_case(case.id)).status == "attributed"

    job = await store.create_job(case.id, "0xsubject", "ethereum")
    assert job.status == "queued"
    await store.set_job(job.id, "running", arq_job_id="arq-1")
    got = await store.get_job(job.id)
    assert got.status == "running" and got.arq_job_id == "arq-1"
    await store.set_job(job.id, "failed", error="boom")
    assert (await store.get_job(job.id)).error == "boom"
    assert await store.get_case(UUID(int=0)) is None


@pytest.mark.asyncio
async def test_store_report_round_trip():
    from datetime import datetime, timezone
    store = MemoryStore()
    case = await _make_case(store)
    job = await store.create_job(case.id, "0xsubject", "ethereum")
    rep = await store.save_report(ReportIn(
        job_id=job.id, case_id=case.id, report_text="body",
        report_hash="a" * 64, inputs_hash="b" * 64,
        generated_at=datetime.now(timezone.utc), engine_version="test",
        certificate_statement="stmt"))
    assert (await store.get_report(rep.id)).report_hash == "a" * 64
    assert (await store.get_report_by_job(job.id)).id == rep.id
    await store.set_webhook_status(rep.id, "delivered")
    assert (await store.get_report(rep.id)).webhook_status == "delivered"


# ---------- pipeline (stub adapter) ----------

class StubAdapter(ChainAdapter):
    chain = Chain.ETHEREUM

    def __init__(self, txs: list[CanonicalTx]):
        super().__init__()
        self._txs = txs

    async def get_transactions(self, address: str,
                               limit: int = 100) -> list[CanonicalTx]:
        return [t for t in self._txs
                if any(p.address == address for p in t.inputs + t.outputs)]

    async def get_token_transfers(self, address: str,
                                  limit: int = 100) -> list[CanonicalTx]:
        return []

    async def health_check(self) -> bool:
        return True


def _tx(tx_hash: str, src: str, dst: str, value: str = "100") -> CanonicalTx:
    asset = Asset(kind=AssetKind.NATIVE, chain=Chain.ETHEREUM, symbol="ETH")
    return CanonicalTx(
        tx_hash=tx_hash, chain=Chain.ETHEREUM, asset=asset,
        inputs=[FlowParty(address=src, value=value)],
        outputs=[FlowParty(address=dst, value=value)])


def _case() -> CaseDetails:
    return CaseDetails(
        case_id="FIR/2026/001234", agency="Cyber Cell", officer="Insp. X",
        wallets=("0xsubject",), tx_hashes=("0xt1",),
        date_from="2026-01-01", date_to="2026-09-27",
        suspected_offence="test")


@pytest.mark.asyncio
async def test_pipeline_empty_graph_produces_dead_end_report():
    deps = PipelineDeps(adapter_factory=lambda c: StubAdapter([]))
    result = await run_trace_pipeline("0xsubject", "ethereum", _case(), deps)
    assert result.terminal_reason == "dead-end"
    assert result.attribution.overall == 1.0  # no hops, nothing discounted
    assert result.risk.level == "low"
    assert verify_report(result.report)
    assert "FIR/2026/001234" in result.report.text


@pytest.mark.asyncio
async def test_pipeline_traces_two_hops_and_scores():
    txs = [_tx("0xt1", "0xsubject", "0xmid"),
           _tx("0xt2", "0xmid", "0xfinal")]
    deps = PipelineDeps(adapter_factory=lambda c: StubAdapter(txs))
    result = await run_trace_pipeline("0xsubject", "ethereum", _case(), deps)
    assert [n.address for n in result.path] == [
        "0xsubject", "0xmid", "0xfinal"]
    assert result.terminal_address == "0xfinal"
    # direct-transfer x2, default classifier confidence 0.5 each
    assert result.attribution.overall == pytest.approx(0.25)
    assert verify_report(result.report)


@pytest.mark.asyncio
async def test_pipeline_flags_sanctioned_subject():
    table = SanctionsList.from_fixture()
    sanctioned = "0x252a8bd2319d8a555b872990601221b3a2053bce"
    deps = PipelineDeps(adapter_factory=lambda c: StubAdapter([]),
                        sanctions=table)
    result = await run_trace_pipeline(sanctioned, "ethereum", _case(), deps)
    assert result.sanctions_hits == (sanctioned,)
    assert result.risk.total == 50
    assert any(s.name == "sanctions-list-hit"
               for s in result.risk.signals)


@pytest.mark.asyncio
async def test_pipeline_resolves_terminal_vasp_when_known():
    txs = [_tx("0xt1", "0xsubject", "0xhotwallet")]
    deps = PipelineDeps(
        adapter_factory=lambda c: StubAdapter(txs),
        vasp_resolver=lambda addr: "coindcx"
        if addr == "0xhotwallet" else None)
    result = await run_trace_pipeline("0xsubject", "ethereum", _case(), deps)
    assert result.terminal_vasp is not None
    assert result.terminal_vasp.name == "CoinDCX"
    assert "DRAFT" in result.drafted_request
    assert "CoinDCX" in result.report.text


# ---------- webhook ----------

def _rec(**kw):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from uuid import uuid4
    base = dict(id=uuid4(), report_hash="h" * 64, inputs_hash="i" * 64,
                generated_at=datetime.now(timezone.utc),
                engine_version="test",
                case_id=uuid4(), job_id=uuid4(),
                address="0xsubject", chain="ethereum",
                fir_number="FIR/1", suspect_address="0xsubject",
                officer_id="o", notes="", status="received")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_webhook_delivers_signed_payload():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sig"] = request.headers["X-Engine-Signature"]
        seen["idem"] = request.headers["X-Idempotency-Key"]
        seen["body"] = request.content
        return httpx.Response(200, json={"ack": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    report, case, job = _rec(), _rec(), _rec()
    res = await deliver_attribution(
        report, case, job, base_url="http://x", secret="s3cret",
        client=client)
    assert res.ok and res.attempts == 1 and res.status_code == 200
    assert seen["sig"] == sign_body(seen["body"], "s3cret")
    assert seen["sig"].startswith("sha256=")
    assert seen["idem"] == "h" * 64
    payload = build_payload(report, case, job)
    assert payload["status"] == "attributed"
    assert payload["report_hash"] == "h" * 64


@pytest.mark.asyncio
async def test_webhook_retries_then_gives_up():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "down"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    res = await deliver_attribution(
        _rec(), _rec(), _rec(), base_url="http://x", secret="s",
        client=client)
    assert not res.ok and res.attempts == 3 and len(calls) == 3
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_webhook_no_retry_on_4xx():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "bad"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    res = await deliver_attribution(
        _rec(), _rec(), _rec(), base_url="http://x", secret="s",
        client=client)
    assert not res.ok and res.attempts == 1 and len(calls) == 1


# ---------- intake API ----------

def _test_app():
    from fastapi import FastAPI
    from api.routers.cases import router as cases_router
    from api.routers.jobs import router as jobs_router
    from api.routers.reports import router as reports_router
    app = FastAPI()
    app.state.store = MemoryStore()
    app.state.queue = MemoryQueue()
    app.include_router(cases_router)
    app.include_router(jobs_router)
    app.include_router(reports_router)
    return app


def test_intake_api_submit_and_fetch_case():
    from fastapi.testclient import TestClient
    client = TestClient(_test_app())
    r = client.post("/cases", json={
        "fir_number": "FIR/2026/009999", "suspect_address": "0xabc",
        "chain": "ethereum", "officer_id": "off-9"})
    assert r.status_code == 201
    case_id = r.json()["case_id"]
    r = client.get(f"/cases/{case_id}")
    assert r.status_code == 200
    assert r.json()["fir_number"] == "FIR/2026/009999"
    assert client.get("/cases/00000000-0000-0000-0000-000000000000"
                      ).status_code == 404


def test_jobs_api_enqueue_and_status():
    from fastapi.testclient import TestClient
    client = TestClient(_test_app())
    case_id = client.post("/cases", json={
        "fir_number": "FIR/1", "suspect_address": "0xabc",
        "chain": "ethereum"}).json()["case_id"]
    r = client.post("/jobs/trace", json={"case_id": case_id})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    r = client.get(f"/jobs/{job_id}")
    assert r.json()["status"] == "queued"
    assert r.json()["report_id"] is None
    # unknown case -> 404, unknown job -> 404
    assert client.post(
        "/jobs/trace",
        json={"case_id": "00000000-0000-0000-0000-000000000000"}
    ).status_code == 404
    assert client.get(
        "/jobs/00000000-0000-0000-0000-000000000000").status_code == 404
