"""Investigation report generator (M6).

Assembles the human-readable, investigation-ready report from the
engine's own outputs: traversal path + hop classifications (M3/M4),
path confidence + risk (M6 scoring), VASP directory attribution and
legal route + drafted request (M5), and the evidentiary certificate
(binding hash + timestamp).

The report is plain text with stable section layout so its canonical
bytes are hashable. Rendering never invents facts: every section is
built from objects passed in; unknown fields render as "not available".
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..scoring import AttributionScore, RiskScore
from ..vasp import (
    VaspRecord,
    CaseDetails,
    RouteRecommendation,
    LegalInstrument,
)
from .certificate import EvidentiaryCertificate, issue_certificate


@dataclass
class ReportInput:
    case: CaseDetails
    subject_wallet: str
    chain: str
    attribution: AttributionScore
    risk: RiskScore
    terminal_vasp: VaspRecord | None
    route: RouteRecommendation | None
    drafted_request: str = ""
    bridge_deposits: tuple = ()
    swap_deposits: tuple = ()  # M18: custodial swap-service deposits
    cross_case: str = ""  # M9: syndicate brief (empty when no links)
    calibration_version: str = ""  # M13: e.g. "cal-3"; "" = uncalibrated


@dataclass(frozen=True)
class InvestigationReport:
    text: str                  # body + human-readable certificate rendering
    body: str                  # canonical bytes the certificate binds
    certificate: EvidentiaryCertificate
    certificate_inputs: dict   # canonical inputs the certificate binds


def _hop_table(attribution: AttributionScore) -> str:
    lines = ["hop | address | kind | classifier x discount = cumulative",
             "----|---------|------|--------------------------------"]
    for i, h in enumerate(attribution.hops, start=1):
        lines.append(
            f"{i} | {h.address} | {h.kind} | "
            f"{h.classifier_confidence} x {h.discount} = {h.cumulative}")
    return "\n".join(lines) if attribution.hops else "(no hops beyond subject)"


# Display labels for terminal reasons in the human-readable report.
# The machine vocabulary (terminal_reason) is unchanged; this only
# affects the "Trail stopped" line.
_TERMINAL_LABELS = {
    # M19: the master plan's explicit wording — a recognized terminus,
    # not a failed trace.
    "otc-hawala-terminus":
        "OTC/hawala terminus — no further on-chain trail expected",
}


def _terminal_label(reason: str | None) -> str:
    if not reason:
        return "not available"
    return _TERMINAL_LABELS.get(reason, reason)


def _render_body(inp: ReportInput) -> str:
    now = datetime.now(timezone.utc).isoformat()
    vasp_line = (f"{inp.terminal_vasp.name} "
                 f"({'FIU-IND registered' if inp.terminal_vasp.fiu_ind_registered else 'NOT FIU-IND registered'}, "
                 f"domicile {inp.terminal_vasp.domicile})"
                 if inp.terminal_vasp else "not available (terminal not "
                 "resolved to a directory VASP)")
    route_line = (f"{inp.route.instrument.value} — {inp.route.rationale}"
                  if inp.route else "not available")
    risk_lines = "\n".join(
        f"  - {s.name} (+{s.points}): {s.reason}" for s in inp.risk.signals
    ) or "  (no risk signals)"
    conf_notes = "\n".join(
        f"  - {n}" for n in inp.attribution.notes) or "  (none)"
    calibration_line = (
        f"   Calibration   : {inp.calibration_version} "
        f"(empirical VASP-confirmation curve)"
        if inp.calibration_version else
        "   Calibration   : none (raw model — no confirmed outcomes yet)")
    bridges = "\n".join(
        f"  - {b.bridge} {b.direction} on {b.chain}: tx {b.tx_hash}"
        for b in inp.bridge_deposits) or "  (none)"
    swaps = "\n".join(
        f"  - {s.service} ({s.role}) on {s.chain}: "
        f"{s.asset_symbol or '?'} {s.value} from {s.address[:12]}…, "
        f"tx {s.tx_hash}"
        for s in inp.swap_deposits) or "  (none)"

    return f"""\
VASP ATTRIBUTION ENGINE — INVESTIGATION REPORT
Generated: {now} (UTC)

1. CASE
   Case ID   : {inp.case.case_id}
   Agency    : {inp.case.agency}
   Officer   : {inp.case.officer}
   Offence   : {inp.case.suspected_offence}
   Period    : {inp.case.date_from} to {inp.case.date_to}

2. SUBJECT
   Wallet    : {inp.subject_wallet}
   Chain     : {inp.chain}

3. FUND-FLOW PATH
{_hop_table(inp.attribution)}

4. TERMINAL ASSESSMENT
   Trail stopped: {_terminal_label(inp.attribution.terminal_reason)}

5. ATTRIBUTION
   Terminal VASP : {vasp_line}
   Confidence    : {inp.attribution.overall:.2f} (path-composite, 0..1)
{calibration_line}
   Confidence notes:
{conf_notes}

6. RISK
   Score         : {inp.risk.total}/100 ({inp.risk.level.upper()})
   Signals:
{risk_lines}

7. RECOMMENDED LEGAL ROUTE
   {route_line}

8. DRAFT DISCLOSURE REQUEST
{inp.drafted_request or '(no draft — terminal not resolved to a directory VASP)'}

9. CROSS-CHAIN LEADS
   Bridge deposits:
{bridges}
   Swap-service deposits:
{swaps}

10. CROSS-CASE LINKS
{inp.cross_case or '(no other persisted case shares addresses with this case)'}

11. EVIDENTIARY CERTIFICATE
    (see attached certificate)
"""


def build_report(inp: ReportInput) -> InvestigationReport:
    """Render the report body, then issue the certificate binding it."""
    body = _render_body(inp)
    inputs = {
        "case_id": inp.case.case_id,
        "subject_wallet": inp.subject_wallet,
        "chain": inp.chain,
        "terminal_reason": inp.attribution.terminal_reason,
        "terminal_vasp": inp.terminal_vasp.name if inp.terminal_vasp else None,
        "calibration_version": inp.calibration_version or None,
    }
    cert = issue_certificate(body, inputs)
    full_text = (body
                 + f"\nCERTIFICATE\n  report_hash : {cert.report_hash}\n"
                 + f"  inputs_hash : {cert.inputs_hash}\n"
                 + f"  generated_at: {cert.generated_at}\n"
                 + f"  engine      : {cert.engine_version}\n"
                 + f"  statement   : {cert.statement}\n")
    return InvestigationReport(text=full_text, body=body,
                               certificate=cert,
                               certificate_inputs=inputs)


def verify_report(report: InvestigationReport) -> bool:
    """Self-contained verification: recompute both digests from the report."""
    from .certificate import verify_certificate
    return verify_certificate(report.certificate, report.body,
                              report.certificate_inputs)
