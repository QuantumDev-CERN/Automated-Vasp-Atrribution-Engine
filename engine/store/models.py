"""Postgres models (M7).

cases: intake records (mirrors what SAHYOG submits).
trace_jobs: durable mirror of arq job state — audit trail, not the queue.
reports: generated investigation reports + evidentiary certificate fields.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fir_number: Mapped[str] = mapped_column(String(64))
    suspect_address: Mapped[str] = mapped_column(String(128))
    chain: Mapped[str] = mapped_column(String(32))
    officer_id: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="received")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class TraceJob(Base):
    __tablename__ = "trace_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id"))
    address: Mapped[str] = mapped_column(String(128))
    chain: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(32), default="queued")  # queued|running|done|failed
    arq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ReportRecord(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trace_jobs.id"))
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id"))
    report_text: Mapped[str] = mapped_column(Text)
    report_hash: Mapped[str] = mapped_column(String(64), unique=True)
    inputs_hash: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    engine_version: Mapped[str] = mapped_column(String(64), default="unknown")
    certificate_statement: Mapped[str] = mapped_column(Text)
    webhook_status: Mapped[str] = mapped_column(
        String(32), default="pending")  # pending|delivered|failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
