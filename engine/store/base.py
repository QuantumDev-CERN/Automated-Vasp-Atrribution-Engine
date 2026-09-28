"""Store protocol (M7).

The API and worker code against this interface. PostgresStore is the
real implementation; MemoryStore is the in-process fake for unit tests
and for running the API without docker. Both are dumb record keepers —
no business logic lives here.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass
class CaseIn:
    fir_number: str
    suspect_address: str
    chain: str
    officer_id: str = ""
    notes: str = ""
    jurisdiction: str = "IN"  # ISO-ish jurisdiction code, e.g. IN, US


@dataclass
class CaseRec:
    id: UUID
    fir_number: str
    suspect_address: str
    chain: str
    officer_id: str
    notes: str
    status: str
    created_at: datetime
    jurisdiction: str = "IN"


@dataclass
class JobRec:
    id: UUID
    case_id: UUID
    address: str
    chain: str
    status: str
    arq_job_id: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class ReportIn:
    job_id: UUID
    case_id: UUID
    report_text: str
    report_hash: str
    inputs_hash: str
    generated_at: datetime
    engine_version: str
    certificate_statement: str
    # M26: trace outcome summary — durable, queryable without parsing
    # report_text or depending on the graph store.
    risk_score: int | None = None          # 0..100
    risk_level: str = ""                   # low|medium|high|critical
    confidence: float | None = None        # 0..1 overall attribution
    terminal_address: str | None = None
    terminal_reason: str | None = None
    hop_count: int | None = None


@dataclass
class ReportRec(ReportIn):
    id: UUID = field(default=None)  # type: ignore[assignment]
    webhook_status: str = "pending"
    created_at: datetime = field(default=None)  # type: ignore[assignment]


@dataclass
class WatchIn:
    address: str
    chain: str
    label: str = ""
    case_id: UUID | None = None
    alert_url: str = ""
    created_by: str = ""
    classification: str = ""  # M26: operator-set category, e.g.
    # "ransomware" | "scam" | "mixer" | "sanctions" — free text, never
    # inferred by the engine.


@dataclass
class WatchRec(WatchIn):
    id: UUID = field(default=None)  # type: ignore[assignment]
    status: str = "active"  # active|paused
    seen_hashes: list[str] = field(default_factory=list)
    last_checked_at: datetime | None = None
    created_at: datetime = field(default=None)  # type: ignore[assignment]


@dataclass
class AlertRec:
    id: UUID
    watch_id: UUID
    tx_hash: str
    direction: str  # in|out
    counterparty: str
    value: str
    asset: str
    vasp_hit: str | None
    delivered: bool
    created_at: datetime
    # M26: analyst disposition — set via
    # PATCH /watchlist/{watch_id}/alerts/{alert_id}. Empty = not reviewed.
    disposition: str = ""  # true_positive|false_positive|benign|escalated
    disposition_notes: str = ""
    disposition_by: str = ""
    disposition_at: datetime | None = None


#: allowed values for AlertRec.disposition (M26)
VALID_DISPOSITIONS = ("true_positive", "false_positive", "benign",
                      "escalated")


@dataclass
class WatchCheckRec:
    """M26: one persisted watch-check cycle — the watch detail page's
    history. Recorded by process_watch on every run (baseline or not),
    including failures (error set, counts zero)."""
    id: UUID
    watch_id: UUID
    checked_at: datetime
    txs_seen: int = 0
    new_events: int = 0
    alerts_delivered: int = 0
    baseline: bool = False
    error: str = ""


# ------------------------------------------------------------------ M12 RBAC

VALID_ROLES = ("viewer", "analyst", "auditor", "admin")

#: capabilities implied by each role
ROLE_CAPS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"read"}),
    "analyst": frozenset({"read", "write"}),
    "auditor": frozenset({"read", "audit"}),
    "admin": frozenset({"read", "write", "audit", "manage_users"}),
}


@dataclass
class ApiUserIn:
    name: str
    role: str  # one of VALID_ROLES
    jurisdictions: list[str]  # e.g. ["IN"]; ["*"] = every jurisdiction
    email: str = ""  # M26: operator contact; shown in the admin console


@dataclass
class ApiUserRec(ApiUserIn):
    id: UUID = field(default=None)  # type: ignore[assignment]
    key_hash: str = ""  # sha256 of the raw API key; never stored raw
    created_at: datetime = field(default=None)  # type: ignore[assignment]
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def can_access(self, jurisdiction: str) -> bool:
        return "*" in self.jurisdictions or jurisdiction in self.jurisdictions

    def has_cap(self, cap: str) -> bool:
        return cap in ROLE_CAPS.get(self.role, frozenset())


@dataclass
class AuditEventIn:
    user_id: UUID | None
    user_name: str
    action: str  # e.g. "POST /cases"
    target_type: str = ""  # case|job|report|watch|user|audit
    target_id: str = ""
    jurisdiction: str = ""
    ip: str = ""
    outcome: str = ""  # HTTP status or "denied"
    detail: str = ""


@dataclass
class AuditEventRec(AuditEventIn):
    id: UUID = field(default=None)  # type: ignore[assignment]
    created_at: datetime = field(default=None)  # type: ignore[assignment]


# ------------------------------------------------------- M26 filings ----

#: delivery channels for a filing
FILING_CHANNEL_SAHYOG = "sahyog"

#: filing lifecycle
FILING_PENDING = "pending"
FILING_DELIVERED = "delivered"
FILING_FAILED = "failed"


@dataclass
class FilingIn:
    """M26: one durable record of an attribution package filed with an
    authority. Recorded by the worker on every webhook delivery attempt
    (success or failure) and by POST /filings/{id}/resend. The SAHYOG
    mock's in-memory log is not durable — this is."""
    case_id: UUID
    report_id: UUID
    channel: str = FILING_CHANNEL_SAHYOG
    status: str = FILING_PENDING  # pending|delivered|failed
    ack_ref: str = ""   # receiver's acknowledgement reference, if any
    error: str = ""     # failure detail when status == failed
    attempts: int = 0   # webhook POST attempts for this filing


@dataclass
class FilingRec(FilingIn):
    id: UUID = field(default=None)  # type: ignore[assignment]
    filed_at: datetime = field(default=None)  # type: ignore[assignment]


# -------------------------------------------------------------- M13 feedback

VALID_OUTCOMES = ("confirmed", "refuted", "inconclusive")


@dataclass
class FeedbackOutcomeIn:
    case_id: UUID
    vasp: str  # attributed VASP name the cooperation request went to
    predicted_confidence: float  # overall confidence reported at trace time
    outcome: str  # one of VALID_OUTCOMES
    notes: str = ""


@dataclass
class FeedbackOutcomeRec(FeedbackOutcomeIn):
    id: UUID = field(default=None)  # type: ignore[assignment]
    recorded_by: str = ""
    created_at: datetime = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True)
class CalibrationModelRec:
    """One versioned confidence-calibration curve.

    bucket_values[i] = calibrated accuracy for decile i (centers
    0.05..0.95); bucket_counts[i] = confirmed+refuted samples in it.
    A model with n_outcomes == 0 is the identity (no behavior change).
    """
    version: str  # "cal-1", "cal-2", ...
    created_at: datetime
    created_by: str
    n_outcomes: int
    bucket_values: tuple[float, ...]
    bucket_counts: tuple[int, ...]


class Store(Protocol):
    async def create_case(self, case: CaseIn) -> CaseRec: ...
    async def get_case(self, case_id: UUID) -> CaseRec | None: ...
    async def set_case_status(self, case_id: UUID, status: str) -> None: ...
    # M26: paginated case register. jurisdictions=None = no scoping;
    # otherwise only cases whose jurisdiction is in the list.
    async def list_cases(self, *, limit: int = 50, offset: int = 0,
                         status: str | None = None,
                         search: str | None = None,
                         jurisdictions: list[str] | None = None,
                         ) -> tuple[list[CaseRec], int]: ...

    async def create_job(self, case_id: UUID, address: str,
                         chain: str) -> JobRec: ...
    async def get_job(self, job_id: UUID) -> JobRec | None: ...
    async def set_job(self, job_id: UUID, status: str,
                      arq_job_id: str | None = None,
                      error: str | None = None) -> None: ...
    # M26: latest job / report for a case (powers the case detail view).
    async def get_latest_job(self, case_id: UUID) -> JobRec | None: ...
    async def get_report_by_case(self, case_id: UUID) -> ReportRec | None: ...

    async def save_report(self, report: ReportIn) -> ReportRec: ...
    async def get_report(self, report_id: UUID) -> ReportRec | None: ...
    async def get_report_by_job(self, job_id: UUID) -> ReportRec | None: ...
    async def set_webhook_status(self, report_id: UUID,
                                 status: str) -> None: ...

    # M10: watchlist
    async def add_watch(self, watch: WatchIn) -> WatchRec: ...
    async def get_watch(self, watch_id: UUID) -> WatchRec | None: ...
    async def list_watches(self, active_only: bool = True) -> list[WatchRec]: ...
    async def set_watch(self, watch_id: UUID, status: str,
                        seen_hashes: list[str] | None = None,
                        last_checked_at: datetime | None = None) -> None: ...
    async def remove_watch(self, watch_id: UUID) -> None: ...
    async def record_alert(self, alert: AlertRec) -> AlertRec: ...
    async def list_alerts(self, watch_id: UUID) -> list[AlertRec]: ...
    # M26: watch check history + alert dispositions
    async def record_watch_check(self, check: WatchCheckRec) -> WatchCheckRec: ...
    async def list_watch_checks(
        self, watch_id: UUID, *, limit: int = 20,
    ) -> list[WatchCheckRec]: ...
    async def set_alert_disposition(
        self, alert_id: UUID, disposition: str, notes: str = "",
        by: str = "",
    ) -> bool: ...

    # M26: durable filings register
    async def record_filing(self, filing: FilingIn) -> FilingRec: ...
    async def get_filing(self, filing_id: UUID) -> FilingRec | None: ...
    async def list_filings(self, *, limit: int = 50, offset: int = 0,
                           status: str | None = None,
                           ) -> tuple[list[FilingRec], int]: ...

    # -- M12: users, API keys, audit trail ---------------------------
    async def create_user(self, user: ApiUserIn,
                          key_hash: str) -> ApiUserRec: ...
    async def get_user(self, user_id: UUID) -> ApiUserRec | None: ...
    async def get_user_by_key_hash(self, key_hash: str) -> ApiUserRec | None: ...
    async def list_users(self) -> list[ApiUserRec]: ...
    async def revoke_user(self, user_id: UUID) -> None: ...
    async def log_audit(self, event: AuditEventIn) -> AuditEventRec: ...
    async def list_audit_events(
        self, *, limit: int = 100, user_id: UUID | None = None,
        action: str | None = None,
    ) -> list[AuditEventRec]: ...

    # -- M13: feedback loop --------------------------------------
    async def record_outcome(
        self, outcome: FeedbackOutcomeIn, recorded_by: str,
    ) -> FeedbackOutcomeRec: ...
    async def list_outcomes(
        self, *, limit: int = 1000,
        outcome: str | None = None,
    ) -> list[FeedbackOutcomeRec]: ...
    async def save_calibration(
        self, model: CalibrationModelRec,
    ) -> CalibrationModelRec: ...
    async def get_calibration(
        self, version: str | None = None,
    ) -> CalibrationModelRec | None: ...
    async def list_calibrations(self) -> list[CalibrationModelRec]: ...
