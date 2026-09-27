"""M5+M6 smoke: score a synthetic path, route the terminal, build + verify
the investigation report.

Offline — no indexer keys needed. Exercises the full analyst path:
traversal-style nodes -> confidence -> risk -> VASP route -> report +
evidentiary certificate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.traversal.engine import VisitedNode
from engine.scoring import score_attribution, score_risk
from engine.report import ReportInput, build_report, verify_report
from engine.vasp import (
    CaseDetails, find_vasp, recommend_and_draft, LegalInstrument,
)


def main() -> int:
    visited = [
        VisitedNode(address="0xsubject", hop=0),
        VisitedNode(address="0xpeel1", hop=1, via_tx="0xt1",
                    via_kind="peel", via_confidence=0.9),
        VisitedNode(address="0xpeel2", hop=2, via_tx="0xt2",
                    via_kind="peel", via_confidence=0.9),
        VisitedNode(address="0xhotwallet", hop=3, via_tx="0xt3",
                    via_kind="sweep-candidate", via_confidence=0.85),
    ]
    attribution = score_attribution(visited,
                                    terminal_reason="sweep-consolidation")
    risk = score_risk(visited, terminal_reason="sweep-consolidation",
                      terminal_vasp_registered=True)
    print(f"confidence: {attribution.overall:.2f} "
          f"({len(attribution.hops)} hops)")
    print(f"risk: {risk.total}/100 ({risk.level})")

    case = CaseDetails(
        case_id="SAHYOG/2026/0042", agency="Cyber Cell, Delhi Police",
        officer="Insp. R. Sharma", wallets=("0xsubject",),
        tx_hashes=("0xt1", "0xt2", "0xt3"),
        date_from="2026-01-01", date_to="2026-09-27",
        suspected_offence="ransomware extortion")
    rec, draft = recommend_and_draft("coindcx", case)
    assert rec.instrument == LegalInstrument.SAHYOG_PMLA

    report = build_report(ReportInput(
        case=case, subject_wallet="0xsubject", chain="ethereum",
        attribution=attribution, risk=risk, terminal_vasp=find_vasp("CoinDCX"),
        route=rec, drafted_request=draft))
    assert verify_report(report), "certificate verification failed"
    assert "SAHYOG/2026/0042" in report.text
    print(f"report: {len(report.text)} chars, "
          f"cert {report.certificate.report_hash[:12]}… verified")
    print("M6 smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
