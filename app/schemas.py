"""Pydantic schemas for the JSON API."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    CaseStatus,
    Consciousness,
    NoteKind,
    Role,
    Sex,
    Shift,
    SummaryStatus,
    UploadStatus,
    VerificationStatus,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PatientOut(ORMModel):
    id: int
    mrn: str | None
    full_name: str
    date_of_birth: date | None
    age_years: int | None
    sex: Sex
    phone: str | None
    next_of_kin: str | None
    next_of_kin_phone: str | None


class ObservationIn(BaseModel):
    # `recorded_by` is deliberately absent: it is taken from the authenticated
    # user so an observation cannot be attributed to another member of staff.
    model_config = ConfigDict(extra="forbid")

    recorded_at: datetime | None = None
    respiratory_rate: int | None = Field(default=None, ge=0, le=90)
    spo2: int | None = Field(default=None, ge=0, le=100)
    on_oxygen: bool = False
    spo2_scale: int = Field(default=1, ge=1, le=2)
    systolic_bp: int | None = Field(default=None, ge=0, le=300)
    diastolic_bp: int | None = Field(default=None, ge=0, le=250)
    pulse: int | None = Field(default=None, ge=0, le=300)
    temperature_c: float | None = Field(default=None, ge=20.0, le=45.0)
    consciousness: Consciousness = Consciousness.alert
    pain_score: int | None = Field(default=None, ge=0, le=10)
    note: str | None = None


class ObservationOut(ORMModel):
    id: int
    case_record_id: int
    recorded_at: datetime
    recorded_by: str | None
    respiratory_rate: int | None
    spo2: int | None
    on_oxygen: bool
    spo2_scale: int
    systolic_bp: int | None
    diastolic_bp: int | None
    pulse: int | None
    temperature_c: float | None
    consciousness: Consciousness
    pain_score: int | None
    note: str | None
    news2_score: int | None
    news2_breakdown: dict | None
    risk_level: str | None


class AlertOut(ORMModel):
    id: int
    case_record_id: int
    severity: str
    message: str
    created_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: str | None


class CaseRecordOut(ORMModel):
    id: int
    case_number: str
    patient: PatientOut
    ward_name: str | None
    bed_label: str | None
    admission_date: date | None
    discharge_date: date | None
    consultant: str | None
    department: str | None
    chief_complaint: str | None
    provisional_diagnosis: str | None
    history: str | None
    allergies: list
    comorbidities: list
    medications: list
    notes: str | None
    status: CaseStatus
    verification: VerificationStatus
    verified_by: str | None
    verified_at: datetime | None
    source_upload_id: int | None
    source_pages: str | None
    extraction_confidence: float | None
    extraction_warnings: list
    created_at: datetime


class CaseRecordUpdate(BaseModel):
    """Fields a clinician may correct after an automatic extraction."""

    ward_name: str | None = None
    bed_label: str | None = None
    admission_date: date | None = None
    consultant: str | None = None
    department: str | None = None
    chief_complaint: str | None = None
    provisional_diagnosis: str | None = None
    history: str | None = None
    allergies: list[str] | None = None
    comorbidities: list[str] | None = None
    medications: list[dict] | None = None
    notes: str | None = None
    status: CaseStatus | None = None


class CaseRecordCreate(BaseModel):
    """Manual entry — used when a scan cannot be read or no API key is set."""

    full_name: str
    mrn: str | None = None
    date_of_birth: date | None = None
    age_years: int | None = None
    sex: Sex = Sex.unknown
    ward_name: str | None = None
    bed_label: str | None = None
    admission_date: date | None = None
    consultant: str | None = None
    department: str | None = None
    chief_complaint: str | None = None
    provisional_diagnosis: str | None = None
    allergies: list[str] = Field(default_factory=list)
    comorbidities: list[str] = Field(default_factory=list)
    medications: list[dict] = Field(default_factory=list)
    notes: str | None = None


class UserOut(ORMModel):
    id: int
    username: str
    full_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    full_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=8, max_length=200)
    role: Role
    issue_api_token: bool = False


class UserCreated(BaseModel):
    user: UserOut
    # Shown exactly once — only its SHA-256 is stored.
    api_token: str | None = None


class MeOut(BaseModel):
    user: UserOut
    permissions: list[str]


class SheetOutcomeOut(BaseModel):
    action: str
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    case_record_id: int | None = None
    case_number: str | None = None
    patient_name: str | None = None
    page_range: str | None = None
    confidence: float | None = None


class UploadOut(ORMModel):
    id: int
    original_filename: str
    content_type: str
    size_bytes: int
    page_count: int | None
    status: UploadStatus
    error: str | None
    sheets_detected: int
    records_created: int
    records_updated: int
    uploaded_by: str | None
    uploaded_at: datetime
    processed_at: datetime | None


class IntakeResultOut(BaseModel):
    upload: UploadOut
    document_notes: str | None = None
    error: str | None = None
    outcomes: list[SheetOutcomeOut] = Field(default_factory=list)


class BoardRow(BaseModel):
    case_record_id: int
    case_number: str
    patient_name: str
    mrn: str | None
    ward_name: str | None
    bed_label: str | None
    age_years: int | None
    sex: Sex
    provisional_diagnosis: str | None
    verification: VerificationStatus
    news2_score: int | None
    risk_level: str | None
    last_observed_at: datetime | None
    open_alerts: int
    allergies: list


# --------------------------------------------------------------------------
# Clinical notes and handover
# --------------------------------------------------------------------------


class NoteIn(BaseModel):
    kind: NoteKind = NoteKind.progress
    shift: Shift | None = None
    body: str | None = None
    # SBAR — the standard shape of a clinical handover.
    situation: str | None = None
    background: str | None = None
    assessment: str | None = None
    recommendation: str | None = None
    outstanding: list[str] = Field(default_factory=list, description="Tasks the next shift picks up.")
    supersedes_id: int | None = Field(
        default=None, description="Id of the note this one corrects; notes are never edited."
    )


class NoteOut(ORMModel):
    id: int
    case_record_id: int
    kind: NoteKind
    shift: Shift | None
    body: str | None
    situation: str | None
    background: str | None
    assessment: str | None
    recommendation: str | None
    outstanding: list
    author_name: str
    author_role: str
    created_at: datetime
    received_by: str | None
    received_at: datetime | None
    supersedes_id: int | None


class AuditEventOut(ORMModel):
    id: int
    occurred_at: datetime
    actor_name: str
    actor_role: str
    action: str
    entity_type: str
    entity_id: int | None
    summary: str
    details: dict
    entry_hash: str


class ChainStatusOut(BaseModel):
    entries: int
    intact: bool
    broken_at_id: int | None = None
    reason: str | None = None
    checked_through: datetime | None = None
    first_entry_at: datetime | None = None


# --------------------------------------------------------------------------
# Discharge summary
# --------------------------------------------------------------------------


class SummaryGenerateIn(BaseModel):
    """The only prose in a summary is written here, by a clinician."""

    follow_up: str | None = None
    discharge_destination: str | None = None


class DischargeSummaryOut(ORMModel):
    id: int
    case_record_id: int
    version: int
    status: SummaryStatus
    content: dict
    follow_up: str | None
    discharge_destination: str | None
    generated_at: datetime
    generated_by: str
    signed_at: datetime | None
    signed_by: str | None
