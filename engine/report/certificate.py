"""Evidentiary certificate (M6).

Hash-stamped, timestamped, tamper-evident certificate for the
investigation report, framed for Section 63 of the Bharatiya Sakshya
Adhiniyam, 2023 (electronic evidence: the certificate identifies the
electronic record and the manner of its production).

Tamper-evidence is cryptographic, not procedural: the certificate binds
the SHA-256 of the canonical report bytes and of the canonical case
inputs. Any alteration of either invalidates verification.
"""
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class EvidentiaryCertificate:
    report_hash: str     # sha256 of canonical report bytes
    inputs_hash: str     # sha256 of canonical case-input bytes
    generated_at: str    # UTC ISO-8601
    engine_version: str  # engine git commit (or "unknown")
    statement: str       # Section 63 framing


def _canonical_bytes(obj) -> bytes:
    if isinstance(obj, str):
        obj = obj.encode("utf-8")
    return obj


def _engine_version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and sha else "unknown"
    except Exception:
        return "unknown"


_STATEMENT = (
    "Certificate under Section 63 of the Bharatiya Sakshya Adhiniyam, "
    "2023. This certificate identifies the electronic record described "
    "below — an investigation report produced by the VASP Attribution "
    "Engine — and the manner of its production. The report_hash is the "
    "SHA-256 digest of the report in its canonical form; the inputs_hash "
    "is the SHA-256 digest of the canonical case inputs (subject wallet, "
    "transaction references, case identifier). Any alteration of the "
    "report or the inputs after issuance invalidates this certificate, "
    "verifiable by recomputation."
)


def issue_certificate(report_text: str, inputs: dict) -> EvidentiaryCertificate:
    """Issue a certificate binding a report to its inputs."""
    report_hash = hashlib.sha256(
        _canonical_bytes(report_text)).hexdigest()
    inputs_hash = hashlib.sha256(
        json.dumps(inputs, sort_keys=True).encode("utf-8")).hexdigest()
    return EvidentiaryCertificate(
        report_hash=report_hash,
        inputs_hash=inputs_hash,
        generated_at=datetime.now(timezone.utc).isoformat(),
        engine_version=_engine_version(),
        statement=_STATEMENT,
    )


def verify_certificate(cert: EvidentiaryCertificate, report_text: str,
                       inputs: dict) -> bool:
    """Recompute both digests; True only if neither was altered."""
    expected = issue_certificate(report_text, inputs)
    return (cert.report_hash == expected.report_hash
            and cert.inputs_hash == expected.inputs_hash)
