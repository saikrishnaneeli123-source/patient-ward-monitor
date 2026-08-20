"""Populate the database with a demo ward so the UI can be explored without an API key.

    python -m scripts.seed_demo

Simulates an upload whose extraction found six patients on one scanned PDF.
"""
from __future__ import annotations

from datetime import date, timedelta

from app import intake, models
from app.db import SessionLocal, init_db
from app.extraction import ExtractedCaseSheet, ExtractedMedication, ExtractedVitals

TODAY = date.today()


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

        outcomes = [intake.apply_sheet(db, s, upload) for s in sheets()]
        upload.sheets_detected = len(outcomes)
        upload.records_created = sum(1 for o in outcomes if o.action == "created")

        # One record has already been checked by a clinician.
        first = outcomes[0].case_record
        if first is not None:
            first.verification = models.VerificationStatus.verified
            first.verified_by = "Dr S. Iyer"
            from datetime import datetime, timezone

            first.verified_at = datetime.now(timezone.utc)

        db.commit()
        print(f"Seeded {upload.records_created} case records across 2 wards.")
        print("Run:  uvicorn app.main:app --reload   then open http://127.0.0.1:8000/")
    finally:
        db.close()


if __name__ == "__main__":
    main()
