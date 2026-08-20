"""Populate the database with a demo ward so the UI can be explored without an API key.

    python -m scripts.seed_demo

Simulates an upload whose extraction found six patients on one scanned PDF.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from app import audit, auth, intake, models, services
from app.db import SessionLocal, init_db
from app.extraction import ExtractedCaseSheet, ExtractedMedication, ExtractedVitals

TODAY = date.today()

DEMO_PASSWORD = "ward-demo-password"
DEMO_STAFF = [
    ("admin", "Ward Administrator", models.Role.admin),
    ("siyer", "Dr S. Iyer", models.Role.doctor),
    ("mthomas", "Sr. Mary Thomas", models.Role.nurse),
    ("clerk", "Ward Clerk", models.Role.clerk),
]


def seed_staff(db) -> models.User:
    """Create the demo accounts and return the nurse who records observations."""
    for username, full_name, role in DEMO_STAFF:
        try:
            auth.create_user(
                db, username=username, full_name=full_name, password=DEMO_PASSWORD, role=role
            )
        except ValueError:
            pass  # already seeded
    return db.query(models.User).filter_by(username="mthomas").one()


def seed_observation_history(db, case: models.CaseRecord, nurse: models.User) -> None:
    """Back-fill four-hourly rounds so the trend chart has something to show.

    The extracted sheet already provided the newest reading, so this walks
    backwards from it with small plausible drifts.
    """
    latest = case.latest_observation
    if latest is None:
        return
    rng = random.Random(case.id)  # deterministic per case, so reseeding looks the same
    for step in range(6, 0, -1):
        drift = step / 6
        observation = models.Observation(
            case_record_id=case.id,
            recorded_at=latest.recorded_at - timedelta(hours=4 * step),
            recorded_by=nurse.full_name,
            recorded_by_id=nurse.id,
            respiratory_rate=_drift(latest.respiratory_rate, drift, 4, rng),
            spo2=_drift(latest.spo2, -drift, 3, rng, cap=100),
            on_oxygen=latest.on_oxygen and step <= 2,
            systolic_bp=_drift(latest.systolic_bp, -drift, 10, rng),
            pulse=_drift(latest.pulse, drift, 8, rng),
            temperature_c=round(latest.temperature_c - drift * 0.8 + rng.uniform(-0.2, 0.2), 1)
            if latest.temperature_c else None,
            consciousness=models.Consciousness.alert,
            note="Routine four-hourly round.",
        )
        # Score before adding: iterating case.observations afterwards would walk
        # an already-loaded collection and silently miss these.
        intake.score_observation(observation)
        db.add(observation)
    db.flush()


def _drift(value, direction, spread, rng, cap=None):
    """Nudge a reading back towards normal, with a little noise."""
    if value is None:
        return None
    drifted = round(value - direction * spread + rng.uniform(-2, 2))
    return min(drifted, cap) if cap else max(drifted, 0)


def sheets() -> list[ExtractedCaseSheet]:
    return [
        ExtractedCaseSheet(
            page_range="1", mrn="MRN-100234", full_name="Asha Rao", age_years=54, sex="F",
            ward_name="Medical A", bed_label="1", admission_date=(TODAY - timedelta(days=3)).isoformat(),
            consultant="Dr S. Iyer", department="General Medicine",
            chief_complaint="Fever and productive cough for 5 days",
            provisional_diagnosis="Community acquired pneumonia, right lower lobe",
            allergies=["Penicillin (rash)"], comorbidities=["Type 2 diabetes mellitus"],
            medications=[
                ExtractedMedication(name="Azithromycin", dose="500 mg", route="PO", frequency="OD"),
                ExtractedMedication(name="Metformin", dose="500 mg", route="PO", frequency="BD"),
            ],
            vitals=ExtractedVitals(respiratory_rate=22, spo2=94, on_oxygen=False, systolic_bp=118,
                                   diastolic_bp=76, pulse=98, temperature_c=38.4, consciousness="alert"),
            confidence=0.94,
        ),
        ExtractedCaseSheet(
            page_range="2-3", mrn="MRN-100571", full_name="Bilal Khan", age_years=67, sex="M",
            ward_name="Medical A", bed_label="2", admission_date=(TODAY - timedelta(days=1)).isoformat(),
            consultant="Dr S. Iyer", department="General Medicine",
            chief_complaint="Breathlessness, worsening over 2 days",
            provisional_diagnosis="Infective exacerbation of COPD",
            allergies=[], comorbidities=["COPD", "Hypertension"],
            medications=[ExtractedMedication(name="Salbutamol", dose="2.5 mg", route="NEB", frequency="QDS")],
            vitals=ExtractedVitals(respiratory_rate=28, spo2=88, on_oxygen=True, systolic_bp=92,
                                   diastolic_bp=58, pulse=124, temperature_c=38.9, consciousness="confusion"),
            confidence=0.71, unreadable_fields=["oxygen flow rate"],
        ),
        ExtractedCaseSheet(
            page_range="4", mrn="MRN-100588", full_name="Chen Wei", age_years=31, sex="M",
            ward_name="Medical A", bed_label="3", admission_date=TODAY.isoformat(),
            consultant="Dr N. Bhat", department="General Medicine",
            chief_complaint="Vomiting and abdominal pain", provisional_diagnosis="Acute gastroenteritis",
            allergies=["None known"],
            vitals=ExtractedVitals(respiratory_rate=18, spo2=98, on_oxygen=False, systolic_bp=108,
                                   diastolic_bp=70, pulse=88, temperature_c=37.4, consciousness="alert"),
            confidence=0.88,
        ),
        ExtractedCaseSheet(
            page_range="5", mrn=None, full_name="Devi Menon", age_years=78, sex="F",
            ward_name="Medical B", bed_label="1", admission_date=(TODAY - timedelta(days=6)).isoformat(),
            consultant="Dr A. Pillai", department="Geriatrics",
            chief_complaint="Fall at home, confusion", provisional_diagnosis="Urinary tract infection with delirium",
            allergies=["Sulfonamides"], comorbidities=["Dementia", "Osteoporosis"],
            vitals=ExtractedVitals(respiratory_rate=20, spo2=95, on_oxygen=False, systolic_bp=104,
                                   diastolic_bp=64, pulse=92, temperature_c=37.9, consciousness="confusion"),
            confidence=0.42, unreadable_fields=["hospital number", "next of kin phone"],
        ),
        ExtractedCaseSheet(
            page_range="6", mrn="MRN-100604", full_name="Eshan Gupta", age_years=45, sex="M",
            ward_name="Medical B", bed_label="2", admission_date=(TODAY - timedelta(days=2)).isoformat(),
            consultant="Dr A. Pillai", department="General Medicine",
            chief_complaint="Chest pain on exertion", provisional_diagnosis="Unstable angina, for stress testing",
            comorbidities=["Hyperlipidaemia"],
            medications=[ExtractedMedication(name="Aspirin", dose="75 mg", route="PO", frequency="OD")],
            vitals=ExtractedVitals(respiratory_rate=16, spo2=97, on_oxygen=False, systolic_bp=132,
                                   diastolic_bp=84, pulse=74, temperature_c=36.6, consciousness="alert"),
            confidence=0.96,
        ),
        ExtractedCaseSheet(
            page_range="7", mrn="MRN-100612", full_name="Fatima Sheikh", age_years=23, sex="F",
            ward_name="Medical B", bed_label="3", admission_date=TODAY.isoformat(),
            consultant="Dr N. Bhat", department="General Medicine",
            chief_complaint="Severe headache and photophobia", provisional_diagnosis="? Meningitis — LP pending",
            allergies=["None known"],
            vitals=ExtractedVitals(respiratory_rate=24, spo2=96, on_oxygen=False, systolic_bp=98,
                                   diastolic_bp=62, pulse=118, temperature_c=39.3, consciousness="alert"),
            confidence=0.9,
        ),
    ]


HANDOVERS = {
    "Bilal Khan": dict(
        situation="Increasingly breathless overnight, now on 2L via nasal cannula.",
        background="COPD, admitted 2 days ago with an infective exacerbation.",
        assessment="NEWS2 climbing — 10 to 16 across the night. New confusion at 04:00.",
        recommendation="Medical review before 09:00. ABG if no better. Keep sats 88-92%.",
        outstanding=["Chase morning ABG", "Medical review before 09:00", "Repeat obs hourly"],
    ),
    "Devi Menon": dict(
        situation="Settled overnight, no further wandering.",
        background="Admitted 6 days ago, UTI with delirium. Dementia, lives alone.",
        assessment="Still intermittently confused but orientated to place this morning.",
        recommendation="Continue antibiotics, OT assessment before any discharge planning.",
        outstanding=["OT assessment", "Speak to daughter about discharge"],
    ),
}


def seed_notes(db, outcomes, nurse) -> None:
    """A night-shift handover on the two patients who need one."""
    for outcome in outcomes:
        case = outcome.case_record
        if case is None:
            continue
        sbar = HANDOVERS.get(case.patient.full_name)
        if sbar is None:
            continue
        services.add_note(
            db, case, nurse, kind=models.NoteKind.handover, shift=models.Shift.night, **sbar
        )


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(models.CaseRecord).count():
            print("Database already has case records — nothing seeded.")
            return

        upload = models.Upload(
            original_filename="ward-round-scan.pdf", stored_path="(demo — no file on disk)",
            content_type="application/pdf", size_bytes=0, sha256="demo-seed",
            page_count=7, status=models.UploadStatus.completed, uploaded_by="Sr. Mary Thomas",
        )
        db.add(upload)
        db.flush()

        nurse = seed_staff(db)
        outcomes = [intake.apply_sheet(db, s, upload) for s in sheets()]

        # The seeder bypasses process_upload, so log the same entries it would.
        audit.record(
            db, actor=nurse, action=audit.UPLOAD_PROCESSED, entity_type="upload",
            entity_id=upload.id,
            summary=f"'{upload.original_filename}': {len(outcomes)} sheet(s) detected.",
            details={"seeded": True},
        )
        for outcome in outcomes:
            if outcome.case_record is not None:
                audit.record(
                    db, actor=nurse, action=audit.CASE_CREATED, entity_type="case",
                    entity_id=outcome.case_record.id,
                    summary=(
                        f"{outcome.case_record.case_number} created from "
                        f"'{upload.original_filename}' page(s) {outcome.sheet.page_range}."
                    ),
                    details={"source": "case sheet extraction", "confidence": outcome.sheet.confidence},
                )
        for outcome in outcomes:
            if outcome.case_record is not None:
                seed_observation_history(db, outcome.case_record, nurse)
        db.commit()
        seed_notes(db, outcomes, nurse)
        upload.sheets_detected = len(outcomes)
        upload.records_created = sum(1 for o in outcomes if o.action == "created")

        # One record has already been checked by a clinician.
        first = outcomes[0].case_record
        doctor = db.query(models.User).filter_by(username="siyer").one()
        if first is not None:
            services.verify_case(db, first, doctor)

        db.commit()
        print(f"Seeded {upload.records_created} case records across 2 wards.")
        print(f"Staff accounts (password '{DEMO_PASSWORD}'):")
        for username, full_name, role in DEMO_STAFF:
            print(f"  {username:9} {role.value:9} {full_name}")
        print("Run:  uvicorn app.main:app --reload   then open http://127.0.0.1:8000/")
    finally:
        db.close()


if __name__ == "__main__":
    main()
