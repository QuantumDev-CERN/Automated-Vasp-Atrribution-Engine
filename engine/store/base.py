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


@dataclass
class ReportRec(ReportIn):
    id: UUID
    webhook_status: str = "pending"
    created_at: datetime = field(default=None)  # type: ignore[assignment]


class Store(Protocol):
    async def create_case(self, case: CaseIn) -> CaseRec: ...
    async def get_case(self, case_id: UUID) -> CaseRec | None: ...
    async def set_case_status(self, case_id: UUID, status: str) -> None: ...

    async def create_job(self, case_id: UUID, address: str,
                         chain: str) -> JobRec: ...
    async def get_job(self, job_id: UUID) -> JobRec | None: ...
    async def set_job(self, job_id: UUID, status: str,
                      arq_job_id: str | None = None,
                      error: str | None = None) -> None: ...

    async def save_report(self, report: ReportIn) -> ReportRec: ...
    async def get_report(self, report_id: UUID) -> ReportRec | None: ...
    async def get_report_by_job(self, job_id: UUID) -> ReportRec | None: ...
    async def set_webhook_status(self, report_id: UUID,
                                 status: str) -> None: ...
