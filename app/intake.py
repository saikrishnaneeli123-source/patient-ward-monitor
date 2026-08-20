"""Turn extracted case sheets into patient and case records.

The rule that matters: one case record per patient per admission. Uploading a
sheet for someone already on the ward must update their open episode, not create
a second patient — otherwise the ward board silently double-counts beds.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, models
from app.extraction import (
    CaseSheetBatch,
    ExtractedCaseSheet,
    ExtractionError,
    ExtractionUnavailable,
    get_extractor,
)
from app.scoring import alerts_for, calculate_news2

logger = logging.getLogger(__name__)

# Marks an observation as transcribed off a scan rather than taken at the bedside.
SHEET_SOURCE = "case sheet (auto-extracted)"

DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d.%m.%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
    "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y",
]


def parse_date(value: str | None) -> date | None:
    """Parse a handwritten-ish date. Returns None rather than raising."""
    if not value:
        return None
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Last resort: ISO-ish prefix, e.g. "2024-03-11 09:20"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(*(int(g) for g in match.groups()))
        except ValueError:
            return None
    return None


def normalise_name(name: str | None) -> str:
    """Casefold, strip accents/punctuation and collapse whitespace for matching."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Apostrophes are dropped rather than spaced, so "D'Souza" and "DSouza" match;
    # every other separator becomes a space, so "Anne-Marie" matches "Anne Marie".
    without_apostrophes = re.sub(r"['\u2018\u2019\u02bc]", "", ascii_only.lower())
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", without_apostrophes)
    return " ".join(cleaned.split())


def normalise_mrn(mrn: str | None) -> str | None:
    if not mrn:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", mrn).upper()
    return cleaned or None


def parse_sex(value: str | None) -> models.Sex:
    if not value:
        return models.Sex.unknown
    token = value.strip().lower()
    if token in ("m", "male", "man", "boy"):
        return models.Sex.male
    if token in ("f", "female", "woman", "girl"):
        return models.Sex.female
    if token in ("other", "o", "intersex", "non-binary", "nonbinary"):
        return models.Sex.other
    return models.Sex.unknown


def parse_consciousness(value: str | None) -> models.Consciousness:
    if not value:
        return models.Consciousness.alert
    token = value.strip().lower()
    for member in models.Consciousness:
        if token == member.value or token[:1] == member.value[:1]:
            return member
    return models.Consciousness.alert


@dataclass
class SheetOutcome:
    """What happened to one extracted sheet."""

    sheet: ExtractedCaseSheet
    case_record: models.CaseRecord | None = None
    action: str = "created"  # created | updated | skipped
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class IntakeResult:
    upload: models.Upload
    outcomes: list[SheetOutcome] = field(default_factory=list)
    document_notes: str | None = None
    error: str | None = None

    @property
    def created(self) -> int:
        return sum(1 for o in self.outcomes if o.action == "created")

    @property
    def updated(self) -> int:
        return sum(1 for o in self.outcomes if o.action == "updated")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.action == "skipped")


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def next_case_number(db: Session) -> str:
    year = datetime.now(timezone.utc).year
    prefix = f"CR-{year}-"
    latest = db.execute(
        select(models.CaseRecord.case_number)
        .where(models.CaseRecord.case_number.like(f"{prefix}%"))
        .order_by(models.CaseRecord.case_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    sequence = int(latest.removeprefix(prefix)) + 1 if latest else 1
    return f"{prefix}{sequence:05d}"


# --------------------------------------------------------------------------
# Identity matching
# --------------------------------------------------------------------------

def find_matching_patient(db: Session, sheet: ExtractedCaseSheet) -> tuple[models.Patient | None, str | None]:
    """Find the existing patient this sheet belongs to.

    Returns (patient, how_it_matched). MRN is authoritative; name+DOB is a strong
    secondary; name+age is weaker and is reported so the record can be flagged.
    """
    mrn = normalise_mrn(sheet.mrn)
    if mrn:
        by_mrn = db.execute(
            select(models.Patient).where(func.upper(models.Patient.mrn) == mrn)
        ).scalar_one_or_none()
        if by_mrn:
            return by_mrn, "mrn"

    name_key = normalise_name(sheet.full_name)
    if not name_key:
        return None, None

    dob = parse_date(sheet.date_of_birth)
    candidates = db.execute(select(models.Patient)).scalars().all()
    same_name = [p for p in candidates if normalise_name(p.full_name) == name_key]
    if not same_name:
        return None, None

    if dob:
        for patient in same_name:
            if patient.date_of_birth == dob:
                return patient, "name+dob"
        # Same name, different recorded DOB — a different person, most likely.
        return None, None

    if sheet.age_years is not None:
        for patient in same_name:
            if patient.age_years is not None and abs(patient.age_years - sheet.age_years) <= 1:
                return patient, "name+age"

    if len(same_name) == 1:
        return same_name[0], "name-only"
    return None, None


def _find_open_case(db: Session, patient: models.Patient, sheet: ExtractedCaseSheet) -> models.CaseRecord | None:
    """Return the patient's current episode if this sheet belongs to it."""
    open_cases = [c for c in patient.case_records if c.status == models.CaseStatus.active]
    if not open_cases:
        return None
    admission = parse_date(sheet.admission_date)
    if admission is None:
        # No date on the sheet — assume it is paperwork for the current episode.
        return open_cases[0]
    for case in open_cases:
        if case.admission_date is None or case.admission_date == admission:
            return case
    return None  # Different admission date -> a genuinely new episode.


# --------------------------------------------------------------------------
# Record building
# --------------------------------------------------------------------------

def _medications(sheet: ExtractedCaseSheet) -> list[dict]:
    return [m.model_dump(exclude_none=True) for m in sheet.medications if m.name]


def _sheet_warnings(sheet: ExtractedCaseSheet) -> list[str]:
    warnings: list[str] = []
    if not sheet.full_name:
        warnings.append("No patient name could be read from this sheet.")
    if not normalise_mrn(sheet.mrn):
        warnings.append("No hospital number on the sheet — matched on name/DOB only.")
    if sheet.confidence < 0.5:
        warnings.append(f"Low transcription confidence ({sheet.confidence:.0%}); check every field.")
    if sheet.unreadable_fields:
        warnings.append("Illegible on the scan: " + ", ".join(sheet.unreadable_fields))
    if sheet.date_of_birth and parse_date(sheet.date_of_birth) is None:
        warnings.append(f"Date of birth '{sheet.date_of_birth}' could not be parsed; stored as written.")
    if sheet.admission_date and parse_date(sheet.admission_date) is None:
        warnings.append(f"Admission date '{sheet.admission_date}' could not be parsed; stored as written.")
    return warnings


def _build_patient(sheet: ExtractedCaseSheet) -> models.Patient:
    return models.Patient(
        mrn=normalise_mrn(sheet.mrn) and sheet.mrn.strip(),
        full_name=(sheet.full_name or "Unidentified patient").strip(),
        date_of_birth=parse_date(sheet.date_of_birth),
        age_years=sheet.age_years,
        sex=parse_sex(sheet.sex),
        phone=sheet.phone,
        address=sheet.address,
        next_of_kin=sheet.next_of_kin,
        next_of_kin_phone=sheet.next_of_kin_phone,
    )


def _fill_patient_blanks(patient: models.Patient, sheet: ExtractedCaseSheet) -> None:
    """Only ever fill in gaps — never overwrite existing demographics from a scan."""
    if not patient.mrn and normalise_mrn(sheet.mrn):
        patient.mrn = sheet.mrn.strip()
    if patient.date_of_birth is None:
        patient.date_of_birth = parse_date(sheet.date_of_birth)
    if patient.age_years is None:
        patient.age_years = sheet.age_years
    if patient.sex == models.Sex.unknown:
        patient.sex = parse_sex(sheet.sex)
    for attr in ("phone", "address", "next_of_kin", "next_of_kin_phone"):
        if getattr(patient, attr) is None:
            setattr(patient, attr, getattr(sheet, attr))


CLINICAL_FIELDS = ("chief_complaint", "provisional_diagnosis", "history", "notes")


def _apply_clinical(case: models.CaseRecord, sheet: ExtractedCaseSheet, *, overwrite: bool) -> list[str]:
    """Copy clinical content onto the case record.

    A *verified* record is only ever gap-filled: a later scan must not silently
    rewrite a diagnosis or allergy list a clinician has already signed off.
    """
    warnings: list[str] = []
    for attr in CLINICAL_FIELDS:
        incoming = getattr(sheet, attr)
        if incoming is None:
            continue
        current = getattr(case, attr)
        if current is None or overwrite:
            setattr(case, attr, incoming)
        elif current.strip() != incoming.strip():
            warnings.append(f"Scan has a different {attr.replace('_', ' ')}; verified value kept.")

    for attr, incoming in (
        ("allergies", sheet.allergies),
        ("comorbidities", sheet.comorbidities),
        ("medications", _medications(sheet)),
    ):
        if not incoming:
            continue
        current = getattr(case, attr) or []
        if not current or overwrite:
            setattr(case, attr, incoming)
        elif current != incoming:
            warnings.append(f"Scan lists different {attr}; verified value kept — reconcile manually.")

    for attr in ("ward_name", "bed_label", "consultant", "department"):
        incoming = getattr(sheet, attr)
        if incoming and (getattr(case, attr) is None or overwrite):
            setattr(case, attr, incoming)

    admission = parse_date(sheet.admission_date)
    if admission and (case.admission_date is None or overwrite):
        case.admission_date = admission
    return warnings


VITALS_FIELDS = (
    "respiratory_rate", "spo2", "on_oxygen", "systolic_bp",
    "diastolic_bp", "pulse", "temperature_c",
)


def _already_transcribed(case: models.CaseRecord, vitals) -> bool:
    """Has this exact set of sheet vitals already been recorded for this case?

    Re-scanning a sheet — or re-running extraction on a stored one — must not
    add a second copy of the same reading. A duplicate would put a phantom point
    on the trend chart and raise the same alert twice.
    """
    incoming = {field: getattr(vitals, field) for field in VITALS_FIELDS}
    incoming["on_oxygen"] = bool(incoming["on_oxygen"])
    for existing in case.observations:
        if existing.recorded_by != SHEET_SOURCE:
            continue
        if all(getattr(existing, field) == incoming[field] for field in VITALS_FIELDS):
            return True
    return False


def record_observation_from_sheet(
    db: Session, case: models.CaseRecord, sheet: ExtractedCaseSheet
) -> models.Observation | None:
    """Store the vitals printed on the sheet as an observation, once."""
    vitals = sheet.vitals
    if vitals is None:
        return None
    values = vitals.model_dump(exclude_none=True)
    if not values:
        return None
    if _already_transcribed(case, vitals):
        return None

    observation = models.Observation(
        case_record=case,
        recorded_by=SHEET_SOURCE,
        respiratory_rate=vitals.respiratory_rate,
        spo2=vitals.spo2,
        on_oxygen=bool(vitals.on_oxygen),
        systolic_bp=vitals.systolic_bp,
        diastolic_bp=vitals.diastolic_bp,
        pulse=vitals.pulse,
        temperature_c=vitals.temperature_c,
        consciousness=parse_consciousness(vitals.consciousness),
        note="Transcribed from the uploaded case sheet.",
    )
    score_observation(observation)
    db.add(observation)
    db.flush()
    raise_alerts(db, observation)
    return observation


def _news2_args(observation: models.Observation) -> dict:
    """Read scoring inputs off an observation that may not be flushed yet.

    Column defaults are applied on flush, so an in-memory object can still have
    None where the database would have a default — score it the same either way.
    """
    consciousness = observation.consciousness or models.Consciousness.alert
    return dict(
        respiratory_rate=observation.respiratory_rate,
        spo2=observation.spo2,
        on_oxygen=bool(observation.on_oxygen),
        spo2_scale=observation.spo2_scale or 1,
        systolic_bp=observation.systolic_bp,
        pulse=observation.pulse,
        temperature_c=observation.temperature_c,
        consciousness=consciousness.value,
    )


def score_observation(observation: models.Observation) -> None:
    result = calculate_news2(**_news2_args(observation))
    observation.news2_score = result.score
    observation.risk_level = result.risk
    observation.news2_breakdown = result.as_dict()


def raise_alerts(db: Session, observation: models.Observation) -> list[models.Alert]:
    """Create alert rows for an observation that breaches escalation thresholds."""
    if not observation.news2_breakdown:
        return []
    result = calculate_news2(**_news2_args(observation))
    created = []
    for severity, message in alerts_for(result):
        alert = models.Alert(
            case_record_id=observation.case_record_id,
            observation_id=observation.id,
            severity=severity,
            message=message,
        )
        db.add(alert)
        created.append(alert)
    return created


def apply_sheet(db: Session, sheet: ExtractedCaseSheet, upload: models.Upload | None = None) -> SheetOutcome:
    """Create or update the case record for one extracted sheet."""
    outcome = SheetOutcome(sheet=sheet)

    if not sheet.full_name and not normalise_mrn(sheet.mrn):
        outcome.action = "skipped"
        outcome.reason = "No name and no hospital number — cannot identify a patient."
        return outcome

    patient, match_kind = find_matching_patient(db, sheet)
    if patient is None:
        patient = _build_patient(sheet)
        db.add(patient)
        db.flush()
    else:
        _fill_patient_blanks(patient, sheet)
        if match_kind in ("name-only", "name+age"):
            outcome.warnings.append(
                f"Matched an existing patient on {match_kind.replace('+', ' + ')} "
                "(no hospital number) — confirm this is the same person."
            )

    warnings = _sheet_warnings(sheet) + outcome.warnings
    existing_case = _find_open_case(db, patient, sheet)

    if existing_case is not None:
        # Never rewrite clinician-verified content from a scan.
        overwrite = existing_case.verification != models.VerificationStatus.verified
        warnings += _apply_clinical(existing_case, sheet, overwrite=overwrite)
        if upload is not None:
            existing_case.source_upload_id = upload.id
            existing_case.source_pages = sheet.page_range
        existing_case.extraction_confidence = sheet.confidence
        existing_case.extraction_warnings = warnings
        existing_case.extraction_payload = sheet.model_dump()
        if overwrite:
            existing_case.verification = models.VerificationStatus.unverified
        outcome.case_record = existing_case
        outcome.action = "updated"
    else:
        case = models.CaseRecord(
            case_number=next_case_number(db),
            patient_id=patient.id,
            source_upload_id=upload.id if upload else None,
            source_pages=sheet.page_range,
            extraction_confidence=sheet.confidence,
            extraction_payload=sheet.model_dump(),
            verification=models.VerificationStatus.unverified,
            allergies=[],
            comorbidities=[],
            medications=[],
            extraction_warnings=[],
        )
        warnings += _apply_clinical(case, sheet, overwrite=True)
        case.extraction_warnings = warnings
        db.add(case)
        db.flush()
        outcome.case_record = case
        outcome.action = "created"

    outcome.warnings = warnings
    record_observation_from_sheet(db, outcome.case_record, sheet)
    db.flush()
    return outcome


def apply_batch(db: Session, batch: CaseSheetBatch, upload: models.Upload | None = None) -> list[SheetOutcome]:
    return [apply_sheet(db, sheet, upload) for sheet in batch.sheets]


def process_upload(
    db: Session, upload: models.Upload, data: bytes, actor: models.User | None = None
) -> IntakeResult:
    """Run extraction for a stored upload and materialise the case records."""
    result = IntakeResult(upload=upload)
    upload.status = models.UploadStatus.processing
    db.flush()

    try:
        batch = get_extractor().extract(
            data=data, content_type=upload.content_type, filename=upload.original_filename
        )
    except (ExtractionUnavailable, ExtractionError) as exc:
        upload.status = models.UploadStatus.failed
        upload.error = str(exc)
        upload.processed_at = datetime.now(timezone.utc)
        db.commit()
        result.error = str(exc)
        return result

    result.outcomes = apply_batch(db, batch, upload)
    result.document_notes = batch.document_notes

    upload.sheets_detected = len(batch.sheets)
    upload.records_created = result.created
    upload.records_updated = result.updated
    upload.status = models.UploadStatus.completed
    upload.error = None
    upload.processed_at = datetime.now(timezone.utc)

    if actor is not None:
        audit.record(
            db,
            actor=actor,
            action=audit.UPLOAD_PROCESSED,
            entity_type="upload",
            entity_id=upload.id,
            summary=(
                f"'{upload.original_filename}': {len(batch.sheets)} sheet(s) detected, "
                f"{result.created} record(s) created, {result.updated} updated."
            ),
            details={
                "sheets": [
                    {
                        "action": o.action,
                        "case_record_id": o.case_record.id if o.case_record else None,
                        "case_number": o.case_record.case_number if o.case_record else None,
                        "patient": o.sheet.full_name,
                        "pages": o.sheet.page_range,
                        "confidence": o.sheet.confidence,
                        "warnings": o.warnings,
                        "reason": o.reason,
                    }
                    for o in result.outcomes
                ],
                "document_notes": batch.document_notes,
            },
        )
        # One audit entry per created record, so a case's own trail starts at
        # its creation rather than only inside the upload's entry.
        for outcome in result.outcomes:
            if outcome.case_record is None:
                continue
            audit.record(
                db,
                actor=actor,
                action=audit.CASE_CREATED if outcome.action == "created" else audit.CASE_UPDATED,
                entity_type="case",
                entity_id=outcome.case_record.id,
                summary=(
                    f"{outcome.case_record.case_number} {outcome.action} from "
                    f"'{upload.original_filename}' page(s) {outcome.sheet.page_range or '?'}."
                ),
                details={
                    "source": "case sheet extraction",
                    "upload_id": upload.id,
                    "confidence": outcome.sheet.confidence,
                    "warnings": outcome.warnings,
                },
            )

    db.commit()
    return result
