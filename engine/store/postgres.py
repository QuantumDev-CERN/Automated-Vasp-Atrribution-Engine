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
    AlertRec, ApiUserIn, ApiUserRec, AuditEventIn, AuditEventRec, CaseIn,
    CaseRec, JobRec, ReportIn, ReportRec,
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
                      key_hash=rec.key_hash, created_at=rec.created_at,
                      revoked_at=rec.revoked_at)


def _audit(rec: models.AuditEvent) -> AuditEventRec:
    return AuditEventRec(
        id=rec.id, user_id=rec.user_id, user_name=rec.user_name,
        action=rec.action, target_type=rec.target_type,
        target_id=rec.target_id, jurisdiction=rec.jurisdiction, ip=rec.ip,
        outcome=rec.outcome, detail=rec.detail, created_at=rec.created_at)


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
        webhook_status=rec.webhook_status, created_at=rec.created_at)


def _watch(rec: models.Watch) -> "WatchRec":
    from .base import WatchRec
    return WatchRec(
        id=rec.id, address=rec.address, chain=rec.chain, label=rec.label,
        case_id=rec.case_id, alert_url=rec.alert_url,
        created_by=rec.created_by, status=rec.status,
        seen_hashes=list(rec.seen_hashes or []),
        last_checked_at=rec.last_checked_at, created_at=rec.created_at)


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
                certificate_statement=report.certificate_statement)
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
                created_by=watch.created_by)
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
        from .base import AlertRec
        async with self._sessions() as s:
            res = await s.execute(
                select(models.WatchAlert).where(
                    models.WatchAlert.watch_id == watch_id).order_by(
                    models.WatchAlert.created_at))
            return [
                AlertRec(id=r.id, watch_id=r.watch_id, tx_hash=r.tx_hash,
                         direction=r.direction, counterparty=r.counterparty,
                         value=r.value, asset=r.asset, vasp_hit=r.vasp_hit,
                         delivered=r.delivered, created_at=r.created_at)
                for r in res.scalars().all()
            ]

    # ------------------------------------------------------------ M12 RBAC
    async def create_user(self, user: ApiUserIn,
                          key_hash: str) -> ApiUserRec:
        async with self._sessions() as s:
            rec = models.ApiUser(name=user.name, role=user.role,
                                 jurisdictions=list(user.jurisdictions),
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
