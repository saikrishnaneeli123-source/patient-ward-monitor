"""ORM models for the ward monitor.

Design notes
------------
* A ``Patient`` is a person. A ``CaseRecord`` is one admission/episode for that
  person. Uploading a case sheet for someone already on the ward therefore adds
  a case record (or updates the open one) rather than duplicating the patient.
* Every auto-created record keeps a pointer to the ``Upload`` it came from and
  the raw model output in ``extraction_payload`` so any field can be traced back
  to the page it was read off.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sex(str, enum.Enum):
    male = "male"
    female = "female"
    other = "other"
    unknown = "unknown"


class CaseStatus(str, enum.Enum):
    active = "active"
    discharged = "discharged"
    transferred = "transferred"
    deceased = "deceased"


class VerificationStatus(str, enum.Enum):
    """Auto-extracted data is never trusted until a clinician confirms it."""

    unverified = "unverified"
    verified = "verified"
    rejected = "rejected"


class UploadStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class NoteKind(str, enum.Enum):
    """Clinical notes are append-only; a correction is a new note that supersedes."""

    progress = "progress"
    handover = "handover"
    escalation = "escalation"
    correction = "correction"


class Shift(str, enum.Enum):
    early = "early"
    late = "late"
    night = "night"


class Consciousness(str, enum.Enum):
    """ACVPU scale."""

    alert = "alert"
    confusion = "confusion"
    voice = "voice"
    pain = "pain"
    unresponsive = "unresponsive"


class Role(str, enum.Enum):
    """Who may do what. Permissions are defined in ``app.auth.ROLE_PERMISSIONS``."""

    admin = "admin"
    doctor = "doctor"
    nurse = "nurse"
    clerk = "clerk"
    readonly = "readonly"


class User(Base):
    """A member of ward staff. Actions are attributed to these, not typed names."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.readonly)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Optional bearer token for devices and integrations; stored hashed.
    api_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def display(self) -> str:
        return f"{self.full_name} ({self.role.value})"


class Ward(Base):
    __tablename__ = "wards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    code: Mapped[str | None] = mapped_column(String(32), default=None)

    beds: Mapped[list["Bed"]] = relationship(back_populates="ward", cascade="all, delete-orphan")


class Bed(Base):
    __tablename__ = "beds"
    __table_args__ = (UniqueConstraint("ward_id", "label", name="uq_bed_ward_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(32))

    ward: Mapped[Ward] = relationship(back_populates="beds")


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Hospital medical record number. Nullable because handwritten sheets often
    # omit it; unique when present so it can drive identity matching.
    mrn: Mapped[str | None] = mapped_column(String(64), unique=True, default=None, index=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, default=None)
    age_years: Mapped[int | None] = mapped_column(Integer, default=None)
    sex: Mapped[Sex] = mapped_column(Enum(Sex), default=Sex.unknown)
    phone: Mapped[str | None] = mapped_column(String(40), default=None)
    address: Mapped[str | None] = mapped_column(Text, default=None)
    next_of_kin: Mapped[str | None] = mapped_column(String(200), default=None)
    next_of_kin_phone: Mapped[str | None] = mapped_column(String(40), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case_records: Mapped[list["CaseRecord"]] = relationship(
        back_populates="patient", cascade="all, delete-orphan", order_by="CaseRecord.id.desc()"
    )


class CaseRecord(Base):
    """One admission episode — the "separate case record" created per patient."""

    __tablename__ = "case_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))

    ward_name: Mapped[str | None] = mapped_column(String(120), default=None)
    bed_label: Mapped[str | None] = mapped_column(String(32), default=None)

    admission_date: Mapped[date | None] = mapped_column(Date, default=None)
    discharge_date: Mapped[date | None] = mapped_column(Date, default=None)
    consultant: Mapped[str | None] = mapped_column(String(200), default=None)
    department: Mapped[str | None] = mapped_column(String(120), default=None)

    chief_complaint: Mapped[str | None] = mapped_column(Text, default=None)
    provisional_diagnosis: Mapped[str | None] = mapped_column(Text, default=None)
    history: Mapped[str | None] = mapped_column(Text, default=None)
    # JSON lists of strings / objects, kept flexible because sheets vary widely.
    allergies: Mapped[list] = mapped_column(JSON, default=list)
    comorbidities: Mapped[list] = mapped_column(JSON, default=list)
    medications: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    status: Mapped[CaseStatus] = mapped_column(Enum(CaseStatus), default=CaseStatus.active)
    verification: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus), default=VerificationStatus.unverified
    )
    # The name is kept alongside the FK so the audit line survives user deletion.
    verified_by: Mapped[str | None] = mapped_column(String(120), default=None)
    verified_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Provenance of an auto-created record.
    source_upload_id: Mapped[int | None] = mapped_column(
        ForeignKey("uploads.id", ondelete="SET NULL"), default=None
    )
    source_pages: Mapped[str | None] = mapped_column(String(40), default=None)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    extraction_warnings: Mapped[list] = mapped_column(JSON, default=list)
    extraction_payload: Mapped[dict | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    patient: Mapped[Patient] = relationship(back_populates="case_records")
    source_upload: Mapped["Upload | None"] = relationship(back_populates="case_records")
    observations: Mapped[list["Observation"]] = relationship(
        back_populates="case_record", cascade="all, delete-orphan", order_by="Observation.recorded_at.desc()"
    )
    alerts: Mapped[list["Alert"]] = relationship(
        back_populates="case_record", cascade="all, delete-orphan", order_by="Alert.created_at.desc()"
    )
    # Named `clinical_notes`, not `notes`: `notes` is already the free-text field
    # transcribed off the case sheet, and a relationship of the same name would
    # silently shadow it.
    clinical_notes: Mapped[list["Note"]] = relationship(
        back_populates="case_record", cascade="all, delete-orphan", order_by="Note.created_at.desc()"
    )

    @property
    def latest_observation(self) -> "Observation | None":
        return self.observations[0] if self.observations else None


class Upload(Base):
    """A scanned case sheet file (image or PDF) plus its processing outcome."""

    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer, default=None)

    status: Mapped[UploadStatus] = mapped_column(Enum(UploadStatus), default=UploadStatus.pending)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    sheets_detected: Mapped[int] = mapped_column(Integer, default=0)
    records_created: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str | None] = mapped_column(String(120), default=None)
    uploaded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    case_records: Mapped[list[CaseRecord]] = relationship(back_populates="source_upload")


class Observation(Base):
    """A set of vitals recorded at the bedside, scored with NEWS2."""

    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_record_id: Mapped[int] = mapped_column(ForeignKey("case_records.id", ondelete="CASCADE"), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    recorded_by: Mapped[str | None] = mapped_column(String(120), default=None)
    recorded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    respiratory_rate: Mapped[int | None] = mapped_column(Integer, default=None)
    spo2: Mapped[int | None] = mapped_column(Integer, default=None)
    on_oxygen: Mapped[bool] = mapped_column(Boolean, default=False)
    # NEWS2 SpO2 scale 2 is used for patients in hypercapnic respiratory failure.
    spo2_scale: Mapped[int] = mapped_column(Integer, default=1)
    systolic_bp: Mapped[int | None] = mapped_column(Integer, default=None)
    diastolic_bp: Mapped[int | None] = mapped_column(Integer, default=None)
    pulse: Mapped[int | None] = mapped_column(Integer, default=None)
    temperature_c: Mapped[float | None] = mapped_column(Float, default=None)
    consciousness: Mapped[Consciousness] = mapped_column(Enum(Consciousness), default=Consciousness.alert)
    pain_score: Mapped[int | None] = mapped_column(Integer, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)

    news2_score: Mapped[int | None] = mapped_column(Integer, default=None)
    news2_breakdown: Mapped[dict | None] = mapped_column(JSON, default=None)
    risk_level: Mapped[str | None] = mapped_column(String(20), default=None)

    case_record: Mapped[CaseRecord] = relationship(back_populates="observations")


class Alert(Base):
    """Raised when an observation crosses an escalation threshold."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_record_id: Mapped[int] = mapped_column(ForeignKey("case_records.id", ondelete="CASCADE"), index=True)
    observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("observations.id", ondelete="CASCADE"), default=None
    )
    severity: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    acknowledged_by: Mapped[str | None] = mapped_column(String(120), default=None)
    acknowledged_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    case_record: Mapped[CaseRecord] = relationship(back_populates="alerts")


class Note(Base):
    """A clinical or shift-handover note against a case.

    Notes are append-only, the way a paper chart is: an entry is never edited or
    deleted. A correction is a new note of kind ``correction`` pointing at the
    one it supersedes, so the original and the correction both stay readable.

    Handover notes carry SBAR fields (Situation, Background, Assessment,
    Recommendation) and are *received* by the incoming staff member, which is
    recorded — an unreceived handover is the thing that goes wrong at shift change.
    """

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_record_id: Mapped[int] = mapped_column(
        ForeignKey("case_records.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[NoteKind] = mapped_column(Enum(NoteKind), default=NoteKind.progress)
    shift: Mapped[Shift | None] = mapped_column(Enum(Shift), default=None)

    body: Mapped[str | None] = mapped_column(Text, default=None)
    # SBAR — used by handover notes, left null on a plain progress note.
    situation: Mapped[str | None] = mapped_column(Text, default=None)
    background: Mapped[str | None] = mapped_column(Text, default=None)
    assessment: Mapped[str | None] = mapped_column(Text, default=None)
    recommendation: Mapped[str | None] = mapped_column(Text, default=None)
    outstanding: Mapped[list] = mapped_column(JSON, default=list)

    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    author_name: Mapped[str] = mapped_column(String(160))
    author_role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # Handover receipt.
    received_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    received_by: Mapped[str | None] = mapped_column(String(160), default=None)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("notes.id", ondelete="SET NULL"), default=None
    )

    case_record: Mapped[CaseRecord] = relationship(back_populates="clinical_notes")
    supersedes: Mapped["Note | None"] = relationship(remote_side="Note.id")

    @property
    def is_sbar(self) -> bool:
        return any((self.situation, self.background, self.assessment, self.recommendation))

    @property
    def awaiting_receipt(self) -> bool:
        return self.kind == NoteKind.handover and self.received_at is None


class AuditEvent(Base):
    """One tamper-evident entry in the audit log.

    Each row carries the hash of the row before it, so the log forms a chain:
    changing or removing any entry breaks every hash after it, and
    ``audit.verify_chain`` reports exactly where. Rows cannot be updated or
    deleted — the ORM refuses and, on SQLite, database triggers refuse too.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # The actor's name and role are copied in, so the log still reads correctly
    # after an account is renamed or deleted.
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    actor_name: Mapped[str] = mapped_column(String(160))
    actor_role: Mapped[str] = mapped_column(String(20))

    action: Mapped[str] = mapped_column(String(60), index=True)
    entity_type: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
