"""In-process store (M7). Implements the Store protocol with dicts.

Used by unit tests and by developers running the API without docker.
Not durable — restart wipes it. Anything that needs to survive a
restart must use PostgresStore.
"""
import uuid
from datetime import datetime, timezone

from .base import (
    AlertRec, ApiUserIn, ApiUserRec, AuditEventIn, AuditEventRec,
    CalibrationModelRec, CaseIn, CaseRec, FeedbackOutcomeIn,
    FeedbackOutcomeRec, FilingIn, FilingRec, JobRec, ReportIn, ReportRec,
    WatchCheckRec, WatchIn, WatchRec,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self) -> None:
        self.cases: dict[uuid.UUID, CaseRec] = {}
        self.jobs: dict[uuid.UUID, JobRec] = {}
        self.reports: dict[uuid.UUID, ReportRec] = {}
        self.watches: dict[uuid.UUID, WatchRec] = {}
        self.alerts: dict[uuid.UUID, AlertRec] = {}
        self.watch_checks: dict[uuid.UUID, WatchCheckRec] = {}
        self.filings: dict[uuid.UUID, FilingRec] = {}
        self.users: dict[uuid.UUID, ApiUserRec] = {}
        self.audit: list[AuditEventRec] = []
        self.outcomes: dict[uuid.UUID, FeedbackOutcomeRec] = {}
        self.calibrations: dict[str, CalibrationModelRec] = {}

    async def create_case(self, case: CaseIn) -> CaseRec:
        rec = CaseRec(id=uuid.uuid4(), fir_number=case.fir_number,
                      suspect_address=case.suspect_address, chain=case.chain,
                      officer_id=case.officer_id, notes=case.notes,
                      jurisdiction=case.jurisdiction,
                      status="received", created_at=_now())
        self.cases[rec.id] = rec
        return rec

    async def get_case(self, case_id: uuid.UUID) -> CaseRec | None:
        return self.cases.get(case_id)

    async def set_case_status(self, case_id: uuid.UUID, status: str) -> None:
        if case_id in self.cases:
            self.cases[case_id].status = status

    async def list_cases(self, *, limit: int = 50, offset: int = 0,
                         status: str | None = None,
                         search: str | None = None,
                         jurisdictions: list[str] | None = None,
                         ) -> tuple[list[CaseRec], int]:
        recs = list(self.cases.values())
        if jurisdictions is not None and "*" not in jurisdictions:
            recs = [r for r in recs if r.jurisdiction in jurisdictions]
        if status:
            recs = [r for r in recs if r.status == status]
        if search:
            q = search.lower()
            recs = [r for r in recs
                    if q in r.fir_number.lower()
                    or q in r.suspect_address.lower()
                    or q in r.officer_id.lower()]
        recs.sort(key=lambda r: r.created_at, reverse=True)
        return recs[offset:offset + limit], len(recs)

    async def create_job(self, case_id: uuid.UUID, address: str,
                         chain: str) -> JobRec:
        rec = JobRec(id=uuid.uuid4(), case_id=case_id, address=address,
                     chain=chain, status="queued", arq_job_id=None,
                     error=None, created_at=_now(), updated_at=_now())
        self.jobs[rec.id] = rec
        return rec

    async def get_job(self, job_id: uuid.UUID) -> JobRec | None:
        return self.jobs.get(job_id)

    async def set_job(self, job_id: uuid.UUID, status: str,
                      arq_job_id: str | None = None,
                      error: str | None = None) -> None:
        rec = self.jobs.get(job_id)
        if rec is None:
            return
        rec.status = status
        rec.updated_at = _now()
        if arq_job_id is not None:
            rec.arq_job_id = arq_job_id
        if error is not None:
            rec.error = error

    async def save_report(self, report: ReportIn) -> ReportRec:
        rec = ReportRec(id=uuid.uuid4(), created_at=_now(), **{
            f.name: getattr(report, f.name)
            for f in ReportIn.__dataclass_fields__.values()})
        self.reports[rec.id] = rec
        return rec

    async def get_report(self, report_id: uuid.UUID) -> ReportRec | None:
        return self.reports.get(report_id)

    async def get_report_by_job(self, job_id: uuid.UUID) -> ReportRec | None:
        for rec in self.reports.values():
            if rec.job_id == job_id:
                return rec
        return None

    async def get_latest_job(self, case_id: uuid.UUID) -> JobRec | None:
        cands = [j for j in self.jobs.values() if j.case_id == case_id]
        if not cands:
            return None
        return max(cands, key=lambda j: j.created_at)

    async def get_report_by_case(self, case_id: uuid.UUID) -> ReportRec | None:
        job = await self.get_latest_job(case_id)
        if job is None:
            return None
        return await self.get_report_by_job(job.id)

    async def set_webhook_status(self, report_id: uuid.UUID,
                                 status: str) -> None:
        if report_id in self.reports:
            self.reports[report_id].webhook_status = status

    # ---------- M10: watchlist ----------

    async def add_watch(self, watch: WatchIn) -> WatchRec:
        rec = WatchRec(id=uuid.uuid4(), created_at=_now(), **{
            f.name: getattr(watch, f.name)
            for f in WatchIn.__dataclass_fields__.values()})
        self.watches[rec.id] = rec
        return rec

    async def get_watch(self, watch_id: uuid.UUID) -> WatchRec | None:
        return self.watches.get(watch_id)

    async def list_watches(self, active_only: bool = True) -> list[WatchRec]:
        recs = list(self.watches.values())
        if active_only:
            recs = [r for r in recs if r.status == "active"]
        return sorted(recs, key=lambda r: r.created_at)

    async def set_watch(self, watch_id: uuid.UUID, status: str,
                        seen_hashes: list[str] | None = None,
                        last_checked_at=None) -> None:
        rec = self.watches.get(watch_id)
        if rec is None:
            return
        rec.status = status
        if seen_hashes is not None:
            rec.seen_hashes = seen_hashes
        if last_checked_at is not None:
            rec.last_checked_at = last_checked_at

    async def remove_watch(self, watch_id: uuid.UUID) -> None:
        self.watches.pop(watch_id, None)

    async def record_alert(self, alert: AlertRec) -> AlertRec:
        self.alerts[alert.id] = alert
        return alert

    async def list_alerts(self, watch_id: uuid.UUID) -> list[AlertRec]:
        return sorted(
            (a for a in self.alerts.values() if a.watch_id == watch_id),
            key=lambda a: a.created_at)

    async def record_watch_check(self, check: WatchCheckRec) -> WatchCheckRec:
        self.watch_checks[check.id] = check
        return check

    async def list_watch_checks(
        self, watch_id: uuid.UUID, *, limit: int = 20,
    ) -> list[WatchCheckRec]:
        rows = sorted(
            (c for c in self.watch_checks.values()
             if c.watch_id == watch_id),
            key=lambda c: c.checked_at, reverse=True)
        return rows[:limit]

    async def set_alert_disposition(
        self, alert_id: uuid.UUID, disposition: str, notes: str = "",
        by: str = "",
    ) -> bool:
        rec = self.alerts.get(alert_id)
        if rec is None:
            return False
        rec.disposition = disposition
        rec.disposition_notes = notes
        rec.disposition_by = by
        rec.disposition_at = _now()
        return True

    # ------------------------------------------------------- M26 filings
    async def record_filing(self, filing: FilingIn) -> FilingRec:
        rec = FilingRec(id=uuid.uuid4(), filed_at=_now(), **{
            f.name: getattr(filing, f.name)
            for f in FilingIn.__dataclass_fields__.values()})
        self.filings[rec.id] = rec
        return rec

    async def get_filing(self, filing_id: uuid.UUID) -> FilingRec | None:
        return self.filings.get(filing_id)

    async def list_filings(self, *, limit: int = 50, offset: int = 0,
                           status: str | None = None,
                           ) -> tuple[list[FilingRec], int]:
        recs = list(self.filings.values())
        if status:
            recs = [r for r in recs if r.status == status]
        recs.sort(key=lambda r: r.filed_at, reverse=True)
        return recs[offset:offset + limit], len(recs)

    # ------------------------------------------------------------ M12 RBAC
    async def create_user(self, user: ApiUserIn,
                          key_hash: str) -> ApiUserRec:
        rec = ApiUserRec(id=uuid.uuid4(), name=user.name, role=user.role,
                         jurisdictions=list(user.jurisdictions),
                         email=user.email,
                         key_hash=key_hash, created_at=_now())
        self.users[rec.id] = rec
        return rec

    async def get_user(self, user_id: uuid.UUID) -> ApiUserRec | None:
        return self.users.get(user_id)

    async def get_user_by_key_hash(self, key_hash: str) -> ApiUserRec | None:
        for rec in self.users.values():
            if rec.key_hash == key_hash:
                return rec
        return None

    async def list_users(self) -> list[ApiUserRec]:
        return sorted(self.users.values(), key=lambda u: u.created_at)

    async def revoke_user(self, user_id: uuid.UUID) -> None:
        rec = self.users.get(user_id)
        if rec is not None:
            rec.revoked_at = _now()

    async def log_audit(self, event: AuditEventIn) -> AuditEventRec:
        rec = AuditEventRec(id=uuid.uuid4(), created_at=_now(), **{
            f: getattr(event, f) for f in (
                "user_id", "user_name", "action", "target_type", "target_id",
                "jurisdiction", "ip", "outcome", "detail")})
        self.audit.append(rec)
        return rec

    async def list_audit_events(
        self, *, limit: int = 100, user_id: uuid.UUID | None = None,
        action: str | None = None,
    ) -> list[AuditEventRec]:
        rows = self.audit
        if user_id is not None:
            rows = [e for e in rows if e.user_id == user_id]
        if action is not None:
            rows = [e for e in rows if e.action == action]
        return list(reversed(rows[-limit:]))

    # -------------------------------------------------------- M13 feedback
    async def record_outcome(
        self, outcome: FeedbackOutcomeIn, recorded_by: str,
    ) -> FeedbackOutcomeRec:
        rec = FeedbackOutcomeRec(
            id=uuid.uuid4(), case_id=outcome.case_id, vasp=outcome.vasp,
            predicted_confidence=outcome.predicted_confidence,
            outcome=outcome.outcome, notes=outcome.notes,
            recorded_by=recorded_by, created_at=_now())
        self.outcomes[rec.id] = rec
        return rec

    async def list_outcomes(
        self, *, limit: int = 1000, outcome: str | None = None,
    ) -> list[FeedbackOutcomeRec]:
        rows = list(self.outcomes.values())
        if outcome is not None:
            rows = [r for r in rows if r.outcome == outcome]
        rows.sort(key=lambda r: r.created_at)
        return rows[-limit:]

    async def save_calibration(
        self, model: CalibrationModelRec,
    ) -> CalibrationModelRec:
        self.calibrations[model.version] = model
        return model

    async def get_calibration(
        self, version: str | None = None,
    ) -> CalibrationModelRec | None:
        if version is not None:
            return self.calibrations.get(version)
        if not self.calibrations:
            return None
        return self.calibrations[max(
            self.calibrations,
            key=lambda v: int(v.split("-")[1]) if v.split("-")[1].isdigit()
            else 0)]

    async def list_calibrations(self) -> list[CalibrationModelRec]:
        return [self.calibrations[v] for v in sorted(
            self.calibrations,
            key=lambda v: int(v.split("-")[1]) if v.split("-")[1].isdigit()
            else 0)]
