"""In-process store (M7). Implements the Store protocol with dicts.

Used by unit tests and by developers running the API without docker.
Not durable — restart wipes it. Anything that needs to survive a
restart must use PostgresStore.
"""
import uuid
from datetime import datetime, timezone

from .base import CaseIn, CaseRec, JobRec, ReportIn, ReportRec


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self) -> None:
        self.cases: dict[uuid.UUID, CaseRec] = {}
        self.jobs: dict[uuid.UUID, JobRec] = {}
        self.reports: dict[uuid.UUID, ReportRec] = {}

    async def create_case(self, case: CaseIn) -> CaseRec:
        rec = CaseRec(id=uuid.uuid4(), fir_number=case.fir_number,
                      suspect_address=case.suspect_address, chain=case.chain,
                      officer_id=case.officer_id, notes=case.notes,
                      status="received", created_at=_now())
        self.cases[rec.id] = rec
        return rec

    async def get_case(self, case_id: uuid.UUID) -> CaseRec | None:
        return self.cases.get(case_id)

    async def set_case_status(self, case_id: uuid.UUID, status: str) -> None:
        if case_id in self.cases:
            self.cases[case_id].status = status

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

    async def set_webhook_status(self, report_id: uuid.UUID,
                                 status: str) -> None:
        if report_id in self.reports:
            self.reports[report_id].webhook_status = status
