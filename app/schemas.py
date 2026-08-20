"""Pydantic schemas for the JSON API."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import CaseStatus, Consciousness, Sex, UploadStatus, VerificationStatus


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
    recorded_at: datetime | None = None
    recorded_by: str | None = None
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


class VerifyIn(BaseModel):
    verified_by: str = Field(min_length=1, max_length=120)


class AcknowledgeIn(BaseModel):
    acknowledged_by: str = Field(min_length=1, max_length=120)


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
