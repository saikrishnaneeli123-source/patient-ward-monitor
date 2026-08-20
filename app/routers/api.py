"""JSON API for the ward monitor."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import auth, intake, models, services
from app.config import get_settings
from app.db import get_db
from app.extraction import ExtractedCaseSheet, detect_content_type, page_count
from app.schemas import (
    AlertOut,
    BoardRow,
    CaseRecordCreate,
    CaseRecordOut,
    CaseRecordUpdate,
    IntakeResultOut,
    MeOut,
    ObservationIn,
    ObservationOut,
    PatientOut,
    SheetOutcomeOut,
    UploadOut,
    UserCreate,
    UserCreated,
    UserOut,
)

router = APIRouter(prefix="/api", tags=["api"])

ACCEPTED_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/tiff",
    "image/bmp",
}


# --------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------

def store_upload(db: Session, file: UploadFile, data: bytes, user: models.User) -> models.Upload:
    """Persist the scan to disk and register it, de-duplicating by content hash."""
    settings = get_settings()
    content_type = detect_content_type(file.filename or "scan", file.content_type)
    if content_type not in ACCEPTED_TYPES:
        raise HTTPException(415, f"Unsupported file type '{content_type}'. Upload a PDF or an image.")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"File exceeds the {settings.max_upload_mb} MB limit.")

    digest = intake.file_digest(data)
    existing = db.execute(
        select(models.Upload).where(models.Upload.sha256 == digest)
    ).scalars().first()
    if existing is not None:
        return existing

    suffix = Path(file.filename or "scan").suffix or ".bin"
    stored = settings.upload_dir / f"{digest[:16]}{suffix}"
    stored.write_bytes(data)

    upload = models.Upload(
        original_filename=file.filename or "scan",
        stored_path=str(stored),
        content_type=content_type,
        size_bytes=len(data),
        sha256=digest,
        page_count=page_count(data, content_type),
        uploaded_by=user.full_name,
        uploaded_by_id=user.id,
    )
    db.add(upload)
    db.commit()
    return upload


def _outcome_out(outcome: intake.SheetOutcome) -> SheetOutcomeOut:
    case = outcome.case_record
    return SheetOutcomeOut(
        action=outcome.action,
        reason=outcome.reason,
        warnings=outcome.warnings,
        case_record_id=case.id if case else None,
        case_number=case.case_number if case else None,
        patient_name=case.patient.full_name if case else outcome.sheet.full_name,
        page_range=outcome.sheet.page_range,
        confidence=outcome.sheet.confidence,
    )


def _result_out(result: intake.IntakeResult) -> IntakeResultOut:
    return IntakeResultOut(
        upload=UploadOut.model_validate(result.upload),
        document_notes=result.document_notes,
        error=result.error,
        outcomes=[_outcome_out(o) for o in result.outcomes],
    )


@router.post("/uploads", response_model=list[IntakeResultOut], status_code=201)
async def upload_case_sheets(
    files: list[UploadFile] = File(..., description="Scanned case sheets (PDF or image)."),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.UPLOAD)),
) -> list[IntakeResultOut]:
    """Upload one or more scans; each detected patient gets its own case record."""
    results = []
    for file in files:
        data = await file.read()
        if not data:
            raise HTTPException(400, f"'{file.filename}' is empty.")
        upload = store_upload(db, file, data, user)
        results.append(_result_out(intake.process_upload(db, upload, data)))
    return results


@router.post("/uploads/{upload_id}/reprocess", response_model=IntakeResultOut)
def reprocess_upload(
    upload_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.UPLOAD)),
) -> IntakeResultOut:
    """Re-run extraction, e.g. after configuring an API key or a model change."""
    upload = db.get(models.Upload, upload_id)
    if upload is None:
        raise HTTPException(404, "Upload not found.")
    path = Path(upload.stored_path)
    if not path.exists():
        raise HTTPException(410, "The stored scan is no longer on disk.")
    return _result_out(intake.process_upload(db, upload, path.read_bytes()))


@router.get("/uploads", response_model=list[UploadOut])
def list_uploads(
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> list[models.Upload]:
    stmt = select(models.Upload).order_by(models.Upload.id.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


@router.get("/uploads/{upload_id}", response_model=UploadOut)
def get_upload(
    upload_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> models.Upload:
    upload = db.get(models.Upload, upload_id)
    if upload is None:
        raise HTTPException(404, "Upload not found.")
    return upload


# --------------------------------------------------------------------------
# Patients and case records
# --------------------------------------------------------------------------

@router.get("/patients", response_model=list[PatientOut])
def list_patients(
    q: str | None = Query(default=None, description="Search by name or hospital number."),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> list[models.Patient]:
    stmt = select(models.Patient).order_by(models.Patient.full_name).limit(limit)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(models.Patient.full_name.ilike(pattern), models.Patient.mrn.ilike(pattern)))
    return list(db.execute(stmt).scalars().all())


@router.get("/patients/{patient_id}", response_model=PatientOut)
def get_patient(
    patient_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> models.Patient:
    patient = db.get(models.Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "Patient not found.")
    return patient


@router.get("/cases", response_model=list[CaseRecordOut])
def list_cases(
    status: models.CaseStatus | None = models.CaseStatus.active,
    ward: str | None = None,
    verification: models.VerificationStatus | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> list[models.CaseRecord]:
    return services.case_query(db, status=status, ward=ward, verification=verification)


@router.post("/cases", response_model=CaseRecordOut, status_code=201)
def create_case(
    payload: CaseRecordCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.EDIT_CASE)),
) -> models.CaseRecord:
    """Create a case record by hand (illegible scan, or extraction disabled)."""
    sheet = ExtractedCaseSheet(
        full_name=payload.full_name,
        mrn=payload.mrn,
        date_of_birth=payload.date_of_birth.isoformat() if payload.date_of_birth else None,
        age_years=payload.age_years,
        sex=payload.sex.value,
        ward_name=payload.ward_name,
        bed_label=payload.bed_label,
        admission_date=payload.admission_date.isoformat() if payload.admission_date else None,
        consultant=payload.consultant,
        department=payload.department,
        chief_complaint=payload.chief_complaint,
        provisional_diagnosis=payload.provisional_diagnosis,
        allergies=payload.allergies,
        comorbidities=payload.comorbidities,
        medications=[],
        notes=payload.notes,
        confidence=1.0,
    )
    outcome = intake.apply_sheet(db, sheet)
    if outcome.case_record is None:
        raise HTTPException(400, outcome.reason or "Could not create the case record.")
    case = outcome.case_record
    case.medications = payload.medications
    # Typed in by a human, so it is verified on creation — by them.
    case.verification = models.VerificationStatus.verified
    case.verified_by = user.full_name
    case.verified_by_id = user.id
    case.verified_at = datetime.now(timezone.utc)
    case.extraction_confidence = None
    case.extraction_warnings = []
    case.extraction_payload = None
    db.commit()
    return case


@router.get("/cases/{case_id}", response_model=CaseRecordOut)
def get_case(
    case_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> models.CaseRecord:
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")
    return case


@router.patch("/cases/{case_id}", response_model=CaseRecordOut)
def update_case(
    case_id: int,
    payload: CaseRecordUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.EDIT_CASE)),
) -> models.CaseRecord:
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(case, field, value)
    if case.status == models.CaseStatus.discharged and case.discharge_date is None:
        case.discharge_date = datetime.now(timezone.utc).date()
    db.commit()
    return case


@router.post("/cases/{case_id}/verify", response_model=CaseRecordOut)
def verify_case(
    case_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VERIFY_CASE)),
) -> models.CaseRecord:
    """Confirm an auto-extracted record against the original scan.

    Attributed to the authenticated user — verification is an audit event, so it
    is never taken from a name typed into the request.
    """
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")
    return services.verify_case(db, case, user)


# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------

@router.post("/cases/{case_id}/observations", response_model=ObservationOut, status_code=201)
def add_observation(
    case_id: int,
    payload: ObservationIn,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.RECORD_OBSERVATIONS)),
) -> models.Observation:
    """Record a set of vitals; NEWS2 is scored and alerts raised automatically."""
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")

    observation = models.Observation(
        case_record_id=case.id,
        recorded_by=user.full_name,
        recorded_by_id=user.id,
        **payload.model_dump(exclude_none=True),
    )
    intake.score_observation(observation)
    db.add(observation)
    db.flush()
    intake.raise_alerts(db, observation)
    db.commit()
    return observation


@router.get("/cases/{case_id}/observations", response_model=list[ObservationOut])
def list_observations(
    case_id: int,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> list[models.Observation]:
    stmt = (
        select(models.Observation)
        .where(models.Observation.case_record_id == case_id)
        .order_by(models.Observation.recorded_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


@router.get("/board", response_model=list[BoardRow])
def get_board(
    ward: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> list[BoardRow]:
    """The ward board: every active case, sickest first."""
    return services.board(db, ward=ward)


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    open_only: bool = True,
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
) -> list[models.Alert]:
    if open_only:
        return services.open_alerts(db, limit)
    stmt = select(models.Alert).order_by(models.Alert.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertOut)
def acknowledge_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.ACKNOWLEDGE_ALERT)),
) -> models.Alert:
    alert = db.get(models.Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found.")
    services.acknowledge(db, alert, user)
    return alert


# --------------------------------------------------------------------------
# Identity and user administration
# --------------------------------------------------------------------------

@router.get("/me", response_model=MeOut)
def whoami(user: models.User = Depends(auth.require_login)) -> MeOut:
    """The caller's identity and what their role allows."""
    return MeOut(user=UserOut.model_validate(user), permissions=sorted(auth.permissions_for(user.role)))


@router.get("/users", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.MANAGE_USERS)),
) -> list[models.User]:
    return list(db.execute(select(models.User).order_by(models.User.username)).scalars().all())


@router.post("/users", response_model=UserCreated, status_code=201)
def create_staff_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.MANAGE_USERS)),
) -> UserCreated:
    try:
        created, token = auth.create_user(
            db,
            username=payload.username,
            full_name=payload.full_name,
            password=payload.password,
            role=payload.role,
            with_token=payload.issue_api_token,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return UserCreated(user=UserOut.model_validate(created), api_token=token)


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.MANAGE_USERS)),
) -> models.User:
    """Deactivating revokes sessions and any API token immediately."""
    target = db.get(models.User, user_id)
    if target is None:
        raise HTTPException(404, "User not found.")
    if target.id == user.id:
        raise HTTPException(400, "You cannot deactivate your own account.")
    target.is_active = False
    db.commit()
    return target
