"""M6 tests: confidence scoring, risk scoring, report + certificate."""
import pytest

from engine.traversal.engine import VisitedNode
from engine.scoring import (
    KIND_DISCOUNT,
    score_attribution,
    score_risk,
)
from engine.report import (
    ReportInput,
    build_report,
    verify_report,
    issue_certificate,
    verify_certificate,
)
from engine.vasp import (
    find_vasp,
    route_for,
    recommend_and_draft,
    CaseDetails,
    LegalInstrument,
)


def _node(address: str, kind: str | None, conf: float | None = 0.9,
          hop: int = 1) -> VisitedNode:
    return VisitedNode(address=address, hop=hop, via_tx="0xtx",
                       via_kind=kind, via_confidence=conf)


def _case() -> CaseDetails:
    return CaseDetails(
        case_id="SAHYOG/2026/0042",
        agency="Cyber Cell, Delhi Police",
        officer="Insp. R. Sharma",
        wallets=("0xabc123",),
        tx_hashes=("0xdeadbeef",),
        date_from="2026-01-01",
        date_to="2026-09-27",
        suspected_offence="ransomware extortion",
    )


# ---------- confidence ----------

def test_discount_table_sane():
    assert set(KIND_DISCOUNT) >= {
        "direct-transfer", "dex-swap", "peel", "sweep-candidate",
        "bridge-lock", "mixer-deposit", "swap-service"}
    assert all(0 < d <= 1.0 for d in KIND_DISCOUNT.values())


def test_confirmed_sweep_spikes_confidence_to_floor():
    visited = [_node("subject", None, None, 0),
               _node("a", "direct-transfer", 0.5),
               _node("b", "direct-transfer", 0.5)]
    score = score_attribution(visited, terminal_reason="sweep-consolidation")
    assert score.overall == 0.80  # 0.25 path raised to the sweep floor
    assert any("sweep" in n for n in score.notes)


def test_mixer_deposit_craters_confidence():
    visited = [_node("subject", None, None, 0),
               _node("a", "direct-transfer", 0.9),
               _node("pool", "mixer-deposit", 0.9)]
    score = score_attribution(visited, terminal_reason="mixer-deposit")
    assert score.overall < 0.5
    assert any("anonymity set" in n for n in score.notes)


def test_peel_chain_decays_composite():
    visited = [_node("subject", None, None, 0)] + [
        _node(f"p{i}", "peel", 0.9, i) for i in range(1, 7)]
    score = score_attribution(visited)
    assert 0 < score.overall < 0.5  # (0.9*0.9)^6 ≈ 0.28


def test_dex_swap_barely_dents():
    visited = [_node("subject", None, None, 0),
               _node("router", "dex-swap", 1.0)]
    assert score_attribution(visited).overall == 0.95


def test_bridge_explicit_destination_barely_dents():
    visited = [_node("subject", None, None, 0),
               _node("bridge", "bridge-lock", 1.0)]
    weak = score_attribution(visited)
    strong = score_attribution(visited, bridge_explicit_destination=True)
    assert weak.overall == 0.85
    assert strong.overall == 0.95


def test_unknown_kind_gets_conservative_default():
    visited = [_node("subject", None, None, 0),
               _node("x", "future-kind", 1.0)]
    assert score_attribution(visited).overall == 0.70


def test_missing_classifier_confidence_assumed_and_noted():
    visited = [_node("subject", None, None, 0),
               _node("a", "direct-transfer", None)]
    score = score_attribution(visited)
    assert score.overall == 0.70
    assert any("assumed 0.70" in n for n in score.notes)


# ---------- risk ----------

def test_clean_path_is_low_risk():
    visited = [_node("subject", None, None, 0),
               _node("a", "direct-transfer", 0.9)]
    risk = score_risk(visited, terminal_reason="sweep-consolidation",
                      terminal_vasp_registered=True)
    assert (risk.total, risk.level) == (0, "low")


def test_mixer_path_scores_medium_on_mixer_alone():
    visited = [_node("subject", None, None, 0),
               _node("pool", "mixer-deposit", 0.9)]
    risk = score_risk(visited, terminal_reason="mixer-deposit")
    assert risk.total == 40 and risk.level == "medium"
    assert risk.signals[0].name == "mixer-deposit"


def test_kitchen_sink_path_is_critical():
    visited = ([_node("subject", None, None, 0)]
               + [_node(f"p{i}", "peel", 0.9, i) for i in range(1, 6)]
               + [_node("svc", "swap-service", 0.8, 6),
                  _node("br", "bridge-lock", 0.8, 7),
                  _node("pool", "mixer-deposit", 0.9, 8)])
    risk = score_risk(visited, terminal_reason="mixer-deposit",
                      terminal_vasp_registered=False)
    assert risk.total == 100 and risk.level == "critical"
    names = {s.name for s in risk.signals}
    assert {"mixer-deposit", "swap-service", "cross-chain-bridge",
            "layering-like-peel-chain",
            "unregistered-terminal-vasp"} <= names


def test_unregistered_terminal_adds_risk():
    visited = [_node("subject", None, None, 0),
               _node("a", "direct-transfer", 0.9)]
    assert score_risk(visited,
                      terminal_vasp_registered=False).total == 10


# ---------- certificate ----------

def test_certificate_verifies_and_detects_tampering():
    inputs = {"case_id": "X", "subject_wallet": "0xabc"}
    cert = issue_certificate("report body", inputs)
    assert verify_certificate(cert, "report body", inputs)
    assert not verify_certificate(cert, "report body TAMPERED", inputs)
    assert not verify_certificate(cert, "report body",
                                  {"case_id": "Y",
                                   "subject_wallet": "0xabc"})
    assert len(cert.report_hash) == 64
    assert "63" in cert.statement  # BSA Section 63 framing


# ---------- report ----------

def _report_input() -> ReportInput:
    visited = [_node("subject", None, None, 0),
               _node("a", "direct-transfer", 0.9),
               _node("hotwallet", "sweep-candidate", 0.85)]
    attribution = score_attribution(visited,
                                    terminal_reason="sweep-consolidation")
    risk = score_risk(visited, terminal_reason="sweep-consolidation",
                      terminal_vasp_registered=True)
    vasp = find_vasp("CoinDCX")
    rec = route_for(vasp)
    _, draft = recommend_and_draft("coindcx", _case())
    return ReportInput(case=_case(), subject_wallet="0xsubject",
                       chain="ethereum", attribution=attribution, risk=risk,
                       terminal_vasp=vasp, route=rec,
                       drafted_request=draft)


def test_report_builds_and_verifies():
    report = build_report(_report_input())
    assert verify_report(report)
    for needle in ("SAHYOG/2026/0042", "0xsubject", "CoinDCX",
                   "FUND-FLOW PATH", "EVIDENTIARY CERTIFICATE",
                   "sahyog_pmla", "report_hash"):
        assert needle in report.text, needle


def test_report_without_resolved_vasp_stays_honest():
    inp = _report_input()
    inp.terminal_vasp = None
    inp.route = None
    inp.drafted_request = ""
    report = build_report(inp)
    assert verify_report(report)
    assert "not available" in report.text
    assert "SAHYOG/2026/0042" in report.text
