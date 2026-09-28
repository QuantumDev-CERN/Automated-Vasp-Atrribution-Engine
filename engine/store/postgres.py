"""Postgres store (M7). Implements the Store protocol with SQLAlchemy 2.0
async + asyncpg. Used when DATABASE_URL/postgres_dsn points at a real
database (docker-compose `postgres` service).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from . import models
from .base import (
    AlertRec, ApiUserIn, ApiUserRec, AuditEventIn, AuditEventRec,
    CalibrationModelRec, CaseIn, CaseRec, FeedbackOutcomeIn,
    FeedbackOutcomeRec, FilingIn, FilingRec, JobRec, ReportIn, ReportRec,
    WatchCheckRec,
)


def _case(rec: models.Case) -> CaseRec:
    return CaseRec(id=rec.id, fir_number=rec.fir_number,
                   suspect_address=rec.suspect_address, chain=rec.chain,
                   officer_id=rec.officer_id, notes=rec.notes,
                   status=rec.status, created_at=rec.created_at,
                   jurisdiction=rec.jurisdiction or "IN")


def _api_user(rec: models.ApiUser) -> ApiUserRec:
    return ApiUserRec(id=rec.id, name=rec.name, role=rec.role,
                      jurisdictions=list(rec.jurisdictions or []),
                      email=rec.email or "",
                      key_hash=rec.key_hash, created_at=rec.created_at,
                      revoked_at=rec.revoked_at)


def _audit(rec: models.AuditEvent) -> AuditEventRec:
    return AuditEventRec(
        id=rec.id, user_id=rec.user_id, user_name=rec.user_name,
        action=rec.action, target_type=rec.target_type,
        target_id=rec.target_id, jurisdiction=rec.jurisdiction, ip=rec.ip,
        outcome=rec.outcome, detail=rec.detail, created_at=rec.created_at)


def _outcome(rec: models.FeedbackOutcome) -> FeedbackOutcomeRec:
    return FeedbackOutcomeRec(
        id=rec.id, case_id=rec.case_id, vasp=rec.vasp,
        predicted_confidence=rec.predicted_confidence, outcome=rec.outcome,
        notes=rec.notes, recorded_by=rec.recorded_by,
        created_at=rec.created_at)


def _calibration(rec: models.CalibrationModel) -> CalibrationModelRec:
    return CalibrationModelRec(
        version=rec.version, created_at=rec.created_at,
        created_by=rec.created_by, n_outcomes=rec.n_outcomes,
        bucket_values=tuple(rec.bucket_values or []),
        bucket_counts=tuple(rec.bucket_counts or []))


def _job(rec: models.TraceJob) -> JobRec:
    return JobRec(id=rec.id, case_id=rec.case_id, address=rec.address,
                  chain=rec.chain, status=rec.status,
                  arq_job_id=rec.arq_job_id, error=rec.error,
                  created_at=rec.created_at, updated_at=rec.updated_at)


def _report(rec: models.ReportRecord) -> ReportRec:
    return ReportRec(
        id=rec.id, job_id=rec.job_id, case_id=rec.case_id,
        report_text=rec.report_text, report_hash=rec.report_hash,
        inputs_hash=rec.inputs_hash, generated_at=rec.generated_at,
        engine_version=rec.engine_version,
        certificate_statement=rec.certificate_statement,
        webhook_status=rec.webhook_status, created_at=rec.created_at,
        risk_score=rec.risk_score, risk_level=rec.risk_level or "",
        confidence=rec.confidence,
        terminal_address=rec.terminal_address,
        terminal_reason=rec.terminal_reason, hop_count=rec.hop_count)


def _watch(rec: models.Watch) -> "WatchRec":
    from .base import WatchRec
    return WatchRec(
        id=rec.id, address=rec.address, chain=rec.chain, label=rec.label,
        case_id=rec.case_id, alert_url=rec.alert_url,
        created_by=rec.created_by, classification=rec.classification or "",
        status=rec.status,
        seen_hashes=list(rec.seen_hashes or []),
        last_checked_at=rec.last_checked_at, created_at=rec.created_at)


def _alert(rec: models.WatchAlert) -> "AlertRec":
    from .base import AlertRec
    return AlertRec(
        id=rec.id, watch_id=rec.watch_id, tx_hash=rec.tx_hash,
        direction=rec.direction, counterparty=rec.counterparty,
        value=rec.value, asset=rec.asset, vasp_hit=rec.vasp_hit,
        delivered=rec.delivered, created_at=rec.created_at,
        disposition=rec.disposition or "",
        disposition_notes=rec.disposition_notes or "",
        disposition_by=rec.disposition_by or "",
        disposition_at=rec.disposition_at)


def _filing(rec: models.Filing) -> FilingRec:
    return FilingRec(
        id=rec.id, case_id=rec.case_id, report_id=rec.report_id,
        channel=rec.channel, status=rec.status, ack_ref=rec.ack_ref or "",
        error=rec.error or "", attempts=rec.attempts, filed_at=rec.filed_at)


class PostgresStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_case(self, case: CaseIn) -> CaseRec:
        async with self._sessions() as s:
            rec = models.Case(fir_number=case.fir_number,
                              suspect_address=case.suspect_address,
                              chain=case.chain, officer_id=case.officer_id,
                              notes=case.notes,
                              jurisdiction=case.jurisdiction or "IN")
            s.add(rec)
            await s.commit()
            return _case(rec)

    async def get_case(self, case_id: uuid.UUID) -> CaseRec | None:
        async with self._sessions() as s:
            rec = await s.get(models.Case, case_id)
            return _case(rec) if rec else None

    async def set_case_status(self, case_id: uuid.UUID, status: str) -> None:
        async with self._sessions() as s:
            rec = await s.get(models.Case, case_id)
            if rec:
                rec.status = status
                await s.commit()

    async def list_cases(self, *, limit: int = 50, offset: int = 0,
                         status: str | None = None,
                         search: str | None = None,
                         jurisdictions: list[str] | None = None,
                         ) -> tuple[list[CaseRec], int]:
        from sqlalchemy import func, or_
        async with self._sessions() as s:
            q = select(models.Case)
            count_q = select(func.count()).select_from(models.Case)
            if jurisdictions is not None and "*" not in jurisdictions:
                q = q.where(models.Case.jurisdiction.in_(jurisdictions))
                count_q = count_q.where(
                    models.Case.jurisdiction.in_(jurisdictions))
            if status:
                q = q.where(models.Case.status == status)
                count_q = count_q.where(models.Case.status == status)
            if search:
                like = f"%{search}%"
                cond = or_(models.Case.fir_number.ilike(like),
                           models.Case.suspect_address.ilike(like),
                           models.Case.officer_id.ilike(like))
                q = q.where(cond)
                count_q = count_q.where(cond)
            total = (await s.execute(count_q)).scalar_one()
            q = q.order_by(models.Case.created_at.desc()
                           ).limit(limit).offset(offset)
            rows = (await s.execute(q)).scalars().all()
            return [_case(r) for r in rows], total

    async def get_latest_job(self, case_id: uuid.UUID) -> JobRec | None:
        async with self._sessions() as s:
            res = await s.execute(
                select(models.TraceJob).where(
                    models.TraceJob.case_id == case_id).order_by(
                    models.TraceJob.created_at.desc()).limit(1))
            rec = res.scalar_one_or_none()
            return _job(rec) if rec else None

    async def get_report_by_case(
        self, case_id: uuid.UUID,
    ) -> ReportRec | None:
        job = await self.get_latest_job(case_id)
        if job is None:
            return None
        return await self.get_report_by_job(job.id)

    async def create_job(self, case_id: uuid.UUID, address: str,
                         chain: str) -> JobRec:
        async with self._sessions() as s:
            rec = models.TraceJob(case_id=case_id, address=address,
                                  chain=chain)
            s.add(rec)
            await s.commit()
            return _job(rec)

    async def get_job(self, job_id: uuid.UUID) -> JobRec | None:
        async with self._sessions() as s:
            rec = await s.get(models.TraceJob, job_id)
            return _job(rec) if rec else None

    async def set_job(self, job_id: uuid.UUID, status: str,
                      arq_job_id: str | None = None,
                      error: str | None = None) -> None:
        async with self._sessions() as s:
            rec = await s.get(models.TraceJob, job_id)
            if rec:
                rec.status = status
                rec.updated_at = datetime.now(rec.updated_at.tzinfo)
                if arq_job_id is not None:
                    rec.arq_job_id = arq_job_id
                if error is not None:
                    rec.error = error
                await s.commit()

    async def save_report(self, report: ReportIn) -> ReportRec:
        async with self._sessions() as s:
            rec = models.ReportRecord(
                job_id=report.job_id, case_id=report.case_id,
                report_text=report.report_text,
                report_hash=report.report_hash,
                inputs_hash=report.inputs_hash,
                generated_at=report.generated_at,
                engine_version=report.engine_version,
                certificate_statement=report.certificate_statement,
                risk_score=report.risk_score,
                risk_level=report.risk_level or "",
                confidence=report.confidence,
                terminal_address=report.terminal_address,
                terminal_reason=report.terminal_reason,
                hop_count=report.hop_count)
            s.add(rec)
            await s.commit()
            return _report(rec)

    async def get_report(self, report_id: uuid.UUID) -> ReportRec | None:
        async with self._sessions() as s:
            rec = await s.get(models.ReportRecord, report_id)
            return _report(rec) if rec else None

    async def get_report_by_job(self, job_id: uuid.UUID) -> ReportRec | None:
        async with self._sessions() as s:
            res = await s.execute(
                select(models.ReportRecord).where(
                    models.ReportRecord.job_id == job_id))
            rec = res.scalar_one_or_none()
            return _report(rec) if rec else None

    async def set_webhook_status(self, report_id: uuid.UUID,
                                 status: str) -> None:
        async with self._sessions() as s:
            rec = await s.get(models.ReportRecord, report_id)
            if rec:
                rec.webhook_status = status
                await s.commit()

    # ---------- M10: watchlist ----------

    async def add_watch(self, watch: "WatchIn") -> "WatchRec":
        from .base import WatchIn, WatchRec
        async with self._sessions() as s:
            rec = models.Watch(
                address=watch.address, chain=watch.chain, label=watch.label,
                case_id=watch.case_id, alert_url=watch.alert_url,
                created_by=watch.created_by,
                classification=watch.classification or "")
            s.add(rec)
            await s.commit()
            await s.refresh(rec)
            return _watch(rec)

    async def get_watch(self, watch_id: uuid.UUID) -> "WatchRec | None":
        async with self._sessions() as s:
            rec = await s.get(models.Watch, watch_id)
            return _watch(rec) if rec else None

    async def list_watches(self, active_only: bool = True) -> list["WatchRec"]:
        async with self._sessions() as s:
            q = select(models.Watch).order_by(models.Watch.created_at)
            if active_only:
                q = q.where(models.Watch.status == "active")
            res = await s.execute(q)
            return [_watch(r) for r in res.scalars().all()]

    async def set_watch(self, watch_id: uuid.UUID, status: str,
                        seen_hashes: "list[str] | None" = None,
                        last_checked_at: "datetime | None" = None) -> None:
        async with self._sessions() as s:
            rec = await s.get(models.Watch, watch_id)
            if rec:
                rec.status = status
                if seen_hashes is not None:
                    rec.seen_hashes = seen_hashes
                if last_checked_at is not None:
                    rec.last_checked_at = last_checked_at
                await s.commit()

    async def remove_watch(self, watch_id: uuid.UUID) -> None:
        async with self._sessions() as s:
            rec = await s.get(models.Watch, watch_id)
            if rec:
                await s.delete(rec)
                await s.commit()

    async def record_alert(self, alert: "AlertRec") -> "AlertRec":
        async with self._sessions() as s:
            rec = models.WatchAlert(
                id=alert.id, watch_id=alert.watch_id, tx_hash=alert.tx_hash,
                direction=alert.direction, counterparty=alert.counterparty,
                value=alert.value, asset=alert.asset,
                vasp_hit=alert.vasp_hit, delivered=alert.delivered)
            s.add(rec)
            await s.commit()
            return alert

    async def list_alerts(self, watch_id: uuid.UUID) -> list["AlertRec"]:
        async with self._sessions() as s:
            res = await s.execute(
                select(models.WatchAlert).where(
                    models.WatchAlert.watch_id == watch_id).order_by(
                    models.WatchAlert.created_at))
            return [_alert(r) for r in res.scalars().all()]

    async def record_watch_check(self, check: WatchCheckRec) -> WatchCheckRec:
        async with self._sessions() as s:
            rec = models.WatchCheck(
                id=check.id, watch_id=check.watch_id,
                checked_at=check.checked_at, txs_seen=check.txs_seen,
                new_events=check.new_events,
                alerts_delivered=check.alerts_delivered,
                baseline=check.baseline, error=check.error or "")
            s.add(rec)
            await s.commit()
            return check

    async def list_watch_checks(
        self, watch_id: uuid.UUID, *, limit: int = 20,
    ) -> list[WatchCheckRec]:
        async with self._sessions() as s:
            res = await s.execute(
                select(models.WatchCheck).where(
                    models.WatchCheck.watch_id == watch_id).order_by(
                    models.WatchCheck.checked_at.desc()).limit(limit))
            return [
                WatchCheckRec(
                    id=r.id, watch_id=r.watch_id, checked_at=r.checked_at,
                    txs_seen=r.txs_seen, new_events=r.new_events,
                    alerts_delivered=r.alerts_delivered, baseline=r.baseline,
                    error=r.error or "")
                for r in res.scalars().all()
            ]

    async def set_alert_disposition(
        self, alert_id: uuid.UUID, disposition: str, notes: str = "",
        by: str = "",
    ) -> bool:
        async with self._sessions() as s:
            rec = await s.get(models.WatchAlert, alert_id)
            if rec is None:
                return False
            rec.disposition = disposition
            rec.disposition_notes = notes
            rec.disposition_by = by
            rec.disposition_at = datetime.now(timezone.utc)
            await s.commit()
            return True

    # ------------------------------------------------------- M26 filings
    async def record_filing(self, filing: FilingIn) -> FilingRec:
        async with self._sessions() as s:
            rec = models.Filing(
                case_id=filing.case_id, report_id=filing.report_id,
                channel=filing.channel, status=filing.status,
                ack_ref=filing.ack_ref or "", error=filing.error or "",
                attempts=filing.attempts)
            s.add(rec)
            await s.commit()
            await s.refresh(rec)
            return _filing(rec)

    async def get_filing(self, filing_id: uuid.UUID) -> FilingRec | None:
        async with self._sessions() as s:
            rec = await s.get(models.Filing, filing_id)
            return _filing(rec) if rec else None

    async def list_filings(self, *, limit: int = 50, offset: int = 0,
                           status: str | None = None,
                           ) -> tuple[list[FilingRec], int]:
        from sqlalchemy import func
        async with self._sessions() as s:
            q = select(models.Filing)
            count_q = select(func.count()).select_from(models.Filing)
            if status:
                q = q.where(models.Filing.status == status)
                count_q = count_q.where(models.Filing.status == status)
            total = (await s.execute(count_q)).scalar_one()
            q = q.order_by(models.Filing.filed_at.desc()
                           ).limit(limit).offset(offset)
            rows = (await s.execute(q)).scalars().all()
            return [_filing(r) for r in rows], total

    # ------------------------------------------------------------ M12 RBAC
    async def create_user(self, user: ApiUserIn,
                          key_hash: str) -> ApiUserRec:
        async with self._sessions() as s:
            rec = models.ApiUser(name=user.name, role=user.role,
                                 jurisdictions=list(user.jurisdictions),
                                 email=user.email or "",
                                 key_hash=key_hash)
            s.add(rec)
            await s.commit()
            return _api_user(rec)

    async def get_user(self, user_id: uuid.UUID) -> ApiUserRec | None:
        async with self._sessions() as s:
            rec = await s.get(models.ApiUser, user_id)
            return _api_user(rec) if rec else None

    async def get_user_by_key_hash(self, key_hash: str) -> ApiUserRec | None:
        async with self._sessions() as s:
            rec = (await s.execute(
                select(models.ApiUser).where(
                    models.ApiUser.key_hash == key_hash))).scalar_one_or_none()
            return _api_user(rec) if rec else None

    async def list_users(self) -> list[ApiUserRec]:
        async with self._sessions() as s:
            rows = (await s.execute(
                select(models.ApiUser).order_by(
                    models.ApiUser.created_at))).scalars().all()
            return [_api_user(r) for r in rows]

    async def revoke_user(self, user_id: uuid.UUID) -> None:
        async with self._sessions() as s:
            rec = await s.get(models.ApiUser, user_id)
            if rec:
                rec.revoked_at = datetime.now(timezone.utc)
                await s.commit()

    async def log_audit(self, event: AuditEventIn) -> AuditEventRec:
        async with self._sessions() as s:
            rec = models.AuditEvent(
                user_id=event.user_id, user_name=event.user_name,
                action=event.action, target_type=event.target_type,
                target_id=event.target_id, jurisdiction=event.jurisdiction,
                ip=event.ip, outcome=event.outcome, detail=event.detail)
            s.add(rec)
            await s.commit()
            return _audit(rec)

    async def list_audit_events(
        self, *, limit: int = 100, user_id: uuid.UUID | None = None,
        action: str | None = None,
    ) -> list[AuditEventRec]:
        async with self._sessions() as s:
            q = select(models.AuditEvent).order_by(
                models.AuditEvent.created_at.desc()).limit(limit)
            if user_id is not None:
                q = q.where(models.AuditEvent.user_id == user_id)
            if action is not None:
                q = q.where(models.AuditEvent.action == action)
            rows = (await s.execute(q)).scalars().all()
            return [_audit(r) for r in rows]

    # -------------------------------------------------------- M13 feedback
    async def record_outcome(
        self, outcome: FeedbackOutcomeIn, recorded_by: str,
    ) -> FeedbackOutcomeRec:
        async with self._sessions() as s:
            rec = models.FeedbackOutcome(
                case_id=outcome.case_id, vasp=outcome.vasp,
                predicted_confidence=outcome.predicted_confidence,
                outcome=outcome.outcome, notes=outcome.notes,
                recorded_by=recorded_by)
            s.add(rec)
            await s.commit()
            return _outcome(rec)

    async def list_outcomes(
        self, *, limit: int = 1000, outcome: str | None = None,
    ) -> list[FeedbackOutcomeRec]:
        async with self._sessions() as s:
            q = select(models.FeedbackOutcome).order_by(
                models.FeedbackOutcome.created_at).limit(limit)
            if outcome is not None:
                q = q.where(models.FeedbackOutcome.outcome == outcome)
            rows = (await s.execute(q)).scalars().all()
            return [_outcome(r) for r in rows]

    async def save_calibration(
        self, model: CalibrationModelRec,
    ) -> CalibrationModelRec:
        async with self._sessions() as s:
            rec = models.CalibrationModel(
                version=model.version, created_at=model.created_at,
                created_by=model.created_by, n_outcomes=model.n_outcomes,
                bucket_values=list(model.bucket_values),
                bucket_counts=list(model.bucket_counts))
            s.add(rec)
            await s.commit()
            return _calibration(rec)

    async def get_calibration(
        self, version: str | None = None,
    ) -> CalibrationModelRec | None:
        async with self._sessions() as s:
            if version is not None:
                rec = await s.get(models.CalibrationModel, version)
                return _calibration(rec) if rec else None
            rec = (await s.execute(
                select(models.CalibrationModel).order_by(
                    models.CalibrationModel.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            return _calibration(rec) if rec else None

    async def list_calibrations(self) -> list[CalibrationModelRec]:
        async with self._sessions() as s:
            rows = (await s.execute(
                select(models.CalibrationModel).order_by(
                    models.CalibrationModel.created_at))).scalars().all()
            return [_calibration(r) for r in rows]
