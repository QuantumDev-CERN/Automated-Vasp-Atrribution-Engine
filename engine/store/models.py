"""Postgres models (M7).

cases: intake records (mirrors what SAHYOG submits).
trace_jobs: durable mirror of arq job state — audit trail, not the queue.
reports: generated investigation reports + evidentiary certificate fields.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    jurisdiction: Mapped[str] = mapped_column(String(16), default="IN")
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


class Watch(Base):
    """M10: watchlist subscriptions — addresses under ongoing surveillance."""
    __tablename__ = "watches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    address: Mapped[str] = mapped_column(String(128))
    chain: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(128), default="")
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id"), nullable=True)
    alert_url: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    seen_hashes: Mapped[list] = mapped_column(JSONB, default=list)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class WatchAlert(Base):
    """M10: one row per detected movement on a watched address."""
    __tablename__ = "watch_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    watch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("watches.id"))
    tx_hash: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(8))  # in|out
    counterparty: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(String(64), default="")
    asset: Mapped[str] = mapped_column(String(64), default="")
    vasp_hit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivered: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class ApiUser(Base):
    """M12: API-key identities for RBAC. Only the key *hash* is stored —
    the raw key is shown once at creation and never again."""
    __tablename__ = "api_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))  # viewer|analyst|auditor|admin
    jurisdictions: Mapped[list] = mapped_column(JSONB, default=list)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    """M12: durable audit trail of every API action."""
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    user_name: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(128))
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(128), default="")
    jurisdiction: Mapped[str] = mapped_column(String(16), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class FeedbackOutcome(Base):
    """M13: confirmed VASP-cooperation outcomes — the ground truth the
    confidence model is recalibrated against."""
    __tablename__ = "feedback_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id"))
    vasp: Mapped[str] = mapped_column(String(128))
    predicted_confidence: Mapped[float] = mapped_column()
    outcome: Mapped[str] = mapped_column(
        String(16))  # confirmed|refuted|inconclusive
    notes: Mapped[str] = mapped_column(Text, default="")
    recorded_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)


class CalibrationModel(Base):
    """M13: versioned confidence-calibration curves. Immutable — a new
    fit writes a new row, never updates history."""
    __tablename__ = "calibration_models"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow)
    created_by: Mapped[str] = mapped_column(String(128), default="")
    n_outcomes: Mapped[int] = mapped_column(default=0)
    bucket_values: Mapped[list] = mapped_column(JSONB, default=list)
    bucket_counts: Mapped[list] = mapped_column(JSONB, default=list)
