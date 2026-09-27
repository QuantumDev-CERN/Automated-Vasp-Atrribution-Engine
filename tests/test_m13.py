"""M13 tests: calibration fitting, application, report binding, API.

No network; MemoryStore throughout.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.core import config as config_mod
from api.routers import feedback as feedback_router_mod
from engine.feedback import (
    calibrate_confidence, describe_model, fit_calibration,
)
from engine.scoring import score_attribution
from engine.store import (
    CalibrationModelRec, CaseIn, FeedbackOutcomeIn, FeedbackOutcomeRec,
    MemoryStore,
)
from engine.store.base import VALID_OUTCOMES
from engine.traversal.engine import VisitedNode


def _outcome(predicted: float, outcome: str,
             case_id: uuid.UUID | None = None) -> FeedbackOutcomeRec:
    return FeedbackOutcomeRec(
        id=uuid.uuid4(), case_id=case_id or uuid.uuid4(),
        vasp="CoinDCX", predicted_confidence=predicted, outcome=outcome,
        recorded_by="tester", created_at=datetime.now(timezone.utc))


def _visited():
    return [
        VisitedNode(address="0xsub", hop=0),
        VisitedNode(address="0xmid", hop=1, via_tx="0xt1",
                    via_kind="peel", via_confidence=0.9),
        VisitedNode(address="0xterm", hop=2, via_tx="0xt2",
                    via_kind="direct-transfer", via_confidence=0.95),
    ]


# ---------- identity when there is no evidence ----------

def test_no_model_is_identity():
    assert calibrate_confidence(None, 0.73) == (0.73, None)

    empty = CalibrationModelRec(
        version="cal-1", created_at=datetime.now(timezone.utc),
        created_by="t", n_outcomes=0,
        bucket_values=(0.05,) * 10, bucket_counts=(0,) * 10)
    assert calibrate_confidence(empty, 0.73) == (0.73, None)


def test_score_unchanged_without_calibration():
    s = score_attribution(_visited())
    assert s.calibration_version is None
    assert not any("calibrated" in n for n in s.notes)
    # raw product: 0.9*0.90 * 0.95*1.00 = 0.7695
    assert s.overall == 0.7695


# ---------- fitting ----------

def test_fit_pulls_high_decile_toward_empirical_rate():
    # top bucket: 8 confirmed / 2 refuted at predicted ~0.85
    outcomes = ([_outcome(0.85, "confirmed") for _ in range(8)]
                + [_outcome(0.85, "refuted") for _ in range(2)]
                + [_outcome(0.15, "confirmed")])  # thin bucket -> identity
    model = fit_calibration(outcomes, version="cal-1", created_by="t")
    assert model.n_outcomes == 11
    assert model.bucket_counts[8] == 10
    # shrunk: w=10/20=0.5 -> 0.5*0.8 + 0.5*0.85 = 0.825
    assert model.bucket_values[8] == pytest.approx(0.825)
    # thin bucket stays on the identity line
    assert model.bucket_values[1] == pytest.approx(0.15)
    # monotonic everywhere
    vals = model.bucket_values
    assert all(b >= a for a, b in zip(vals, vals[1:]))

    v, ver = calibrate_confidence(model, 0.85)
    assert ver == "cal-1"
    assert v == pytest.approx(0.825, abs=0.01)


def test_inconclusive_outcomes_excluded():
    outcomes = ([_outcome(0.85, "confirmed") for _ in range(6)]
                + [_outcome(0.85, "inconclusive") for _ in range(20)])
    model = fit_calibration(outcomes, version="cal-1", created_by="t")
    assert model.n_outcomes == 6
    assert model.bucket_counts[8] == 6


def test_pava_enforces_monotonicity_on_adversarial_data():
    # low predictions always confirm, high predictions always refute —
    # raw curve would slope the wrong way
    outcomes = ([_outcome(0.05, "confirmed") for _ in range(10)]
                + [_outcome(0.95, "refuted") for _ in range(10)])
    model = fit_calibration(outcomes, version="cal-1", created_by="t")
    vals = model.bucket_values
    assert all(b >= a - 1e-9 for a, b in zip(vals, vals[1:])), vals


# ---------- scoring + report binding ----------

def test_calibrated_score_names_its_version():
    outcomes = ([_outcome(0.85, "confirmed") for _ in range(8)]
                + [_outcome(0.85, "refuted") for _ in range(2)])
    model = fit_calibration(outcomes, version="cal-7", created_by="t")
    s = score_attribution(_visited(), calibration=model)
    assert s.calibration_version == "cal-7"
    assert s.overall != 0.7695  # the curve moved it
    assert any("cal-7" in n for n in s.notes)


def test_report_binds_calibration_version():
    from engine.report import ReportInput, build_report, verify_report
    from engine.report.generator import CaseDetails
    from engine.scoring import score_risk

    outcomes = ([_outcome(0.85, "confirmed") for _ in range(8)]
                + [_outcome(0.85, "refuted") for _ in range(2)])
    model = fit_calibration(outcomes, version="cal-3", created_by="t")
    attribution = score_attribution(_visited(), calibration=model)
    risk = score_risk(_visited())
    case = CaseDetails(case_id="FIR/1", agency="X", officer="Y",
                       wallets=("0xsub",), tx_hashes=(), date_from="",
                       date_to="", suspected_offence="test")
    report = build_report(ReportInput(
        case=case, subject_wallet="0xsub", chain="ethereum",
        attribution=attribution, risk=risk,
        terminal_vasp=None, route=None,
        calibration_version=attribution.calibration_version or ""))
    assert "cal-3" in report.body
    assert report.certificate_inputs["calibration_version"] == "cal-3"
    assert verify_report(report)

    # and without a model the report honestly says so
    raw = score_attribution(_visited())
    report2 = build_report(ReportInput(
        case=case, subject_wallet="0xsub", chain="ethereum",
        attribution=raw, risk=risk, terminal_vasp=None, route=None))
    assert "no confirmed outcomes yet" in report2.body
    assert report2.certificate_inputs["calibration_version"] is None


# ---------- API ----------

@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(config_mod.settings, "auth_enforced", True)


def _app(store: MemoryStore) -> FastAPI:
    app = FastAPI()
    app.state.store = store
    app.include_router(feedback_router_mod.router)
    return app


async def _mkuser(store, name, role):
    from engine.auth import hash_key
    from engine.store import ApiUserIn

    raw = f"vea_fb_{name}"
    await store.create_user(
        ApiUserIn(name=name, role=role, jurisdictions=["*"]),
        key_hash=hash_key(raw))
    return raw


async def test_feedback_api_end_to_end(enforced):
    store = MemoryStore()
    case = await store.create_case(CaseIn(
        fir_number="FIR/FB", suspect_address="0xfb", chain="ethereum"))
    analyst = await _mkuser(store, "analyst1", "analyst")
    viewer = await _mkuser(store, "viewer1", "viewer")
    H = lambda raw: {"X-API-Key": raw}  # noqa: E731

    client = TestClient(_app(store))

    # viewer cannot record
    r = client.post("/feedback/outcomes", headers=H(viewer), json={
        "case_id": str(case.id), "vasp": "CoinDCX",
        "predicted_confidence": 0.85, "outcome": "confirmed"})
    assert r.status_code == 403

    # bad outcome value rejected
    r = client.post("/feedback/outcomes", headers=H(analyst), json={
        "case_id": str(case.id), "vasp": "CoinDCX",
        "predicted_confidence": 0.85, "outcome": "maybe"})
    assert r.status_code == 400

    for _ in range(8):
        r = client.post("/feedback/outcomes", headers=H(analyst), json={
            "case_id": str(case.id), "vasp": "CoinDCX",
            "predicted_confidence": 0.85, "outcome": "confirmed"})
        assert r.status_code == 201
    for _ in range(2):
        client.post("/feedback/outcomes", headers=H(analyst), json={
            "case_id": str(case.id), "vasp": "CoinDCX",
            "predicted_confidence": 0.85, "outcome": "refuted"})

    r = client.get("/feedback/outcomes", headers=H(viewer))
    assert r.json()["count"] == 10
    r = client.get("/feedback/outcomes?outcome=refuted", headers=H(viewer))
    assert r.json()["count"] == 2

    # no calibration yet
    r = client.get("/feedback/calibration", headers=H(viewer))
    assert r.json()["model"] is None

    # recalibrate -> cal-1
    r = client.post("/feedback/recalibrate", headers=H(analyst))
    assert r.status_code == 200
    body = r.json()
    assert body["model"]["version"] == "cal-1"
    assert body["model"]["n_outcomes"] == 10

    r = client.get("/feedback/calibration", headers=H(viewer))
    model = r.json()["model"]
    assert model["version"] == "cal-1"
    top = [b for b in model["buckets"]
           if b["decile"] == "0.8-0.9"][0]
    assert top["samples"] == 10
    assert top["calibrated"] == pytest.approx(0.825)

    # second fit versions up, never overwrites
    r = client.post("/feedback/recalibrate", headers=H(analyst))
    assert r.json()["model"]["version"] == "cal-2"
    assert len(await store.list_calibrations()) == 2
    first = await store.get_calibration("cal-1")
    assert first.version == "cal-1"
    latest = await store.get_calibration()
    assert latest.version == "cal-2"


async def test_store_round_trip():
    store = MemoryStore()
    case = await store.create_case(CaseIn(
        fir_number="FIR/RT", suspect_address="0xrt", chain="ethereum"))
    rec = await store.record_outcome(FeedbackOutcomeIn(
        case_id=case.id, vasp="CoinDCX", predicted_confidence=0.9,
        outcome="confirmed", notes="FIU reply"), recorded_by="officer")
    assert rec.recorded_by == "officer"
    assert len(await store.list_outcomes()) == 1
    assert await store.get_calibration() is None
