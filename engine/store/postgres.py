"""Postgres store (M7). Implements the Store protocol with SQLAlchemy 2.0
async + asyncpg. Used when DATABASE_URL/postgres_dsn points at a real
database (docker-compose `postgres` service).
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from . import models
from .base import CaseIn, CaseRec, JobRec, ReportIn, ReportRec


def _case(rec: models.Case) -> CaseRec:
    return CaseRec(id=rec.id, fir_number=rec.fir_number,
                   suspect_address=rec.suspect_address, chain=rec.chain,
                   officer_id=rec.officer_id, notes=rec.notes,
                   status=rec.status, created_at=rec.created_at)


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


class PostgresStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_case(self, case: CaseIn) -> CaseRec:
        async with self._sessions() as s:
            rec = models.Case(fir_number=case.fir_number,
                              suspect_address=case.suspect_address,
                              chain=case.chain, officer_id=case.officer_id,
                              notes=case.notes)
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
