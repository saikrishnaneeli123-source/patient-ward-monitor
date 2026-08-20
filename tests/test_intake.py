"""Intake: one case record per patient, no duplicates, no silent overwrites."""
from datetime import date

import pytest

from app import intake, models
from app.extraction import CaseSheetBatch, ExtractedCaseSheet, ExtractedMedication, ExtractedVitals


def sheet(**overrides) -> ExtractedCaseSheet:
    base = dict(full_name="Asha Rao", mrn="MRN-8891", age_years=54, sex="F", confidence=0.92)
    return ExtractedCaseSheet(**{**base, **overrides})


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("2024-03-11", date(2024, 3, 11)),
        ("11/03/2024", date(2024, 3, 11)),
        ("11-03-2024", date(2024, 3, 11)),
        ("11 Mar 2024", date(2024, 3, 11)),
        ("2024-03-11 09:20", date(2024, 3, 11)),
        ("illegible", None),
        (None, None),
    ],
)
def test_parse_date_is_lenient(text, expected):
    assert intake.parse_date(text) == expected


def test_normalise_name_ignores_case_accents_and_punctuation():
    assert intake.normalise_name("José  M. D'Souza") == intake.normalise_name("jose m dsouza")


def test_normalise_mrn_ignores_formatting():
    assert intake.normalise_mrn("mrn-88/91") == "MRN8891"


# --------------------------------------------------------------------------
# Record creation
# --------------------------------------------------------------------------

def test_each_sheet_creates_its_own_case_record(db):
    batch = CaseSheetBatch(
        sheets=[
            sheet(full_name="Asha Rao", mrn="A-1", page_range="1"),
            sheet(full_name="Bilal Khan", mrn="B-2", page_range="2-3"),
            sheet(full_name="Chen Wei", mrn="C-3", page_range="4"),
        ]
    )
    outcomes = intake.apply_batch(db, batch)
    db.commit()

    assert [o.action for o in outcomes] == ["created"] * 3
    assert db.query(models.Patient).count() == 3
    assert db.query(models.CaseRecord).count() == 3
    numbers = {o.case_record.case_number for o in outcomes}
    assert len(numbers) == 3, "case numbers must be unique"
    assert outcomes[1].case_record.source_pages == "2-3"


def test_auto_created_records_start_unverified(db):
    outcome = intake.apply_sheet(db, sheet())
    db.commit()
    assert outcome.case_record.verification == models.VerificationStatus.unverified
    assert outcome.case_record.extraction_confidence == pytest.approx(0.92)


def test_second_sheet_for_same_mrn_updates_rather_than_duplicates(db):
    intake.apply_sheet(db, sheet(provisional_diagnosis="Community acquired pneumonia"))
    db.commit()
    second = intake.apply_sheet(db, sheet(mrn="mrn 8891", provisional_diagnosis="CAP, resolving"))
    db.commit()

    assert second.action == "updated"
    assert db.query(models.Patient).count() == 1
    assert db.query(models.CaseRecord).count() == 1
    assert second.case_record.provisional_diagnosis == "CAP, resolving"


def test_matching_falls_back_to_name_and_dob(db):
    intake.apply_sheet(db, sheet(mrn=None, date_of_birth="1970-06-02"))
    db.commit()
    again = intake.apply_sheet(db, sheet(mrn=None, date_of_birth="02/06/1970"))
    db.commit()
    assert again.action == "updated"
    assert db.query(models.Patient).count() == 1


def test_same_name_different_dob_is_a_different_person(db):
    intake.apply_sheet(db, sheet(mrn=None, date_of_birth="1970-06-02"))
    db.commit()
    other = intake.apply_sheet(db, sheet(mrn=None, date_of_birth="1994-01-30"))
    db.commit()
    assert other.action == "created"
    assert db.query(models.Patient).count() == 2


def test_weak_name_only_match_is_flagged_for_confirmation(db):
    intake.apply_sheet(db, sheet(mrn=None, age_years=None))
    db.commit()
    again = intake.apply_sheet(db, sheet(mrn=None, age_years=None))
    db.commit()
    assert again.action == "updated"
    assert any("confirm this is the same person" in w for w in again.warnings)


def test_readmission_creates_a_second_case_record(db):
    first = intake.apply_sheet(db, sheet(admission_date="2024-01-05"))
    first.case_record.status = models.CaseStatus.discharged
    db.commit()

    second = intake.apply_sheet(db, sheet(admission_date="2024-06-20"))
    db.commit()

    assert second.action == "created"
    assert db.query(models.Patient).count() == 1
    assert db.query(models.CaseRecord).count() == 2


def test_new_admission_date_while_still_active_starts_a_new_episode(db):
    intake.apply_sheet(db, sheet(admission_date="2024-01-05"))
    db.commit()
    second = intake.apply_sheet(db, sheet(admission_date="2024-03-09"))
    db.commit()
    assert second.action == "created"
    assert db.query(models.CaseRecord).count() == 2


def test_sheet_without_a_name_or_mrn_is_skipped_not_guessed(db):
    outcome = intake.apply_sheet(db, ExtractedCaseSheet(provisional_diagnosis="Sepsis", confidence=0.3))
    db.commit()
    assert outcome.action == "skipped"
    assert outcome.case_record is None
    assert db.query(models.CaseRecord).count() == 0


# --------------------------------------------------------------------------
# Safety rules
# --------------------------------------------------------------------------

def test_verified_clinical_data_is_never_overwritten_by_a_later_scan(db):
    first = intake.apply_sheet(db, sheet(allergies=["Penicillin"], provisional_diagnosis="Pneumonia"))
    case = first.case_record
    case.verification = models.VerificationStatus.verified
    db.commit()

    second = intake.apply_sheet(db, sheet(allergies=["None known"], provisional_diagnosis="Bronchitis"))
    db.commit()

    assert second.case_record.allergies == ["Penicillin"]
    assert second.case_record.provisional_diagnosis == "Pneumonia"
    assert any("reconcile manually" in w for w in second.warnings)
    assert second.case_record.verification == models.VerificationStatus.verified


def test_unverified_record_is_refreshed_by_a_rescan(db):
    intake.apply_sheet(db, sheet(provisional_diagnosis="Pneumonia"))
    db.commit()
    second = intake.apply_sheet(db, sheet(provisional_diagnosis="Bronchitis"))
    db.commit()
    assert second.case_record.provisional_diagnosis == "Bronchitis"


def test_existing_demographics_are_not_clobbered_by_a_blank_scan(db):
    first = intake.apply_sheet(db, sheet(phone="555-0100", date_of_birth="1970-06-02"))
    db.commit()
    intake.apply_sheet(db, sheet(phone=None, date_of_birth=None))
    db.commit()
    patient = first.case_record.patient
    assert patient.phone == "555-0100"
    assert patient.date_of_birth == date(1970, 6, 2)


def test_low_confidence_and_illegible_fields_raise_warnings(db):
    outcome = intake.apply_sheet(db, sheet(confidence=0.3, unreadable_fields=["dose of metformin"]))
    db.commit()
    warnings = " ".join(outcome.warnings)
    assert "Low transcription confidence" in warnings
    assert "dose of metformin" in warnings


def test_unparseable_admission_date_is_warned_about(db):
    outcome = intake.apply_sheet(db, sheet(admission_date="last Tuesday"))
    db.commit()
    assert any("could not be parsed" in w for w in outcome.warnings)
    assert outcome.case_record.admission_date is None


def test_medications_are_transcribed_onto_the_record(db):
    outcome = intake.apply_sheet(
        db,
        sheet(medications=[
            ExtractedMedication(name="Amoxicillin", dose="500 mg", route="PO", frequency="TDS"),
            ExtractedMedication(name=None, dose="illegible"),
        ]),
    )
    db.commit()
    meds = outcome.case_record.medications
    assert len(meds) == 1, "entries with no drug name must be dropped, not invented"
    assert meds[0]["name"] == "Amoxicillin"


# --------------------------------------------------------------------------
# Vitals on the sheet
# --------------------------------------------------------------------------

def test_vitals_on_the_sheet_become_a_scored_observation(db):
    outcome = intake.apply_sheet(
        db,
        sheet(vitals=ExtractedVitals(
            respiratory_rate=24, spo2=92, on_oxygen=True, systolic_bp=98,
            pulse=118, temperature_c=38.6, consciousness="alert",
        )),
    )
    db.commit()

    observation = outcome.case_record.latest_observation
    assert observation is not None
    assert observation.news2_score == 11
    assert observation.risk_level == "high"
    assert observation.recorded_by == "case sheet (auto-extracted)"


def test_a_deteriorating_observation_raises_an_alert(db):
    outcome = intake.apply_sheet(
        db,
        sheet(vitals=ExtractedVitals(
            respiratory_rate=26, spo2=88, on_oxygen=True, systolic_bp=88,
            pulse=132, temperature_c=39.4, consciousness="voice",
        )),
    )
    db.commit()
    alerts = outcome.case_record.alerts
    assert alerts and alerts[0].severity == "critical"
    assert alerts[0].acknowledged_at is None


def test_a_sheet_without_vitals_creates_no_observation(db):
    outcome = intake.apply_sheet(db, sheet(vitals=None))
    db.commit()
    assert outcome.case_record.observations == []


# --------------------------------------------------------------------------
# Whole-upload processing
# --------------------------------------------------------------------------

def test_process_upload_records_the_outcome_on_the_upload_row(db, fake_extractor):
    fake_extractor(CaseSheetBatch(
        sheets=[sheet(full_name="Asha Rao", mrn="A-1"), sheet(full_name="Bilal Khan", mrn="B-2")],
        document_notes="Page 5 was blank.",
    ))
    upload = models.Upload(
        original_filename="ward-round.pdf", stored_path="/tmp/x.pdf",
        content_type="application/pdf", size_bytes=100, sha256="deadbeef",
    )
    db.add(upload)
    db.commit()

    result = intake.process_upload(db, upload, b"%PDF-fake")

    assert result.created == 2
    assert result.document_notes == "Page 5 was blank."
    assert upload.status == models.UploadStatus.completed
    assert upload.sheets_detected == 2
    assert upload.records_created == 2
    assert upload.processed_at is not None


def test_extraction_failure_is_recorded_without_losing_the_scan(db):
    from app import extraction

    class Broken:
        available = True

        def extract(self, **_):
            raise extraction.ExtractionError("model unavailable")

    extraction.set_extractor(Broken())
    upload = models.Upload(
        original_filename="bad.pdf", stored_path="/tmp/bad.pdf",
        content_type="application/pdf", size_bytes=10, sha256="cafe",
    )
    db.add(upload)
    db.commit()

    result = intake.process_upload(db, upload, b"x")

    assert result.error == "model unavailable"
    assert upload.status == models.UploadStatus.failed
    assert upload.error == "model unavailable"
    assert db.query(models.CaseRecord).count() == 0


# --------------------------------------------------------------------------
# Re-scanning the same sheet
# --------------------------------------------------------------------------

def test_rescanning_a_sheet_does_not_duplicate_its_vitals(db):
    """Found by running the app: a re-scan added a phantom observation and a
    second copy of the same alert."""
    vitals = ExtractedVitals(respiratory_rate=22, spo2=96, on_oxygen=False,
                             systolic_bp=106, pulse=104, temperature_c=38.9,
                             consciousness="alert")
    first = intake.apply_sheet(db, sheet(vitals=vitals))
    db.commit()
    assert len(first.case_record.observations) == 1

    again = intake.apply_sheet(db, sheet(vitals=vitals))
    db.commit()

    assert again.action == "updated"
    assert len(again.case_record.observations) == 1, "the same reading must not be stored twice"


def test_rescanning_does_not_raise_the_same_alert_twice(db):
    vitals = ExtractedVitals(respiratory_rate=28, spo2=88, on_oxygen=True,
                             systolic_bp=88, pulse=132, temperature_c=39.4,
                             consciousness="voice")
    intake.apply_sheet(db, sheet(vitals=vitals))
    db.commit()
    outcome = intake.apply_sheet(db, sheet(vitals=vitals))
    db.commit()
    assert len(outcome.case_record.alerts) == 1


def test_a_sheet_with_genuinely_new_vitals_is_still_recorded(db):
    """Only an identical reading is suppressed — a later round must still land."""
    outcome = intake.apply_sheet(db, sheet(vitals=ExtractedVitals(pulse=80, spo2=98)))
    db.commit()
    updated = intake.apply_sheet(db, sheet(vitals=ExtractedVitals(pulse=120, spo2=91)))
    db.commit()
    assert len(updated.case_record.observations) == 2


def test_a_bedside_observation_with_the_same_values_is_never_suppressed(db):
    """Two identical readings taken by a nurse are real data, not a duplicate."""
    outcome = intake.apply_sheet(db, sheet(vitals=ExtractedVitals(pulse=80, spo2=98)))
    case = outcome.case_record
    manual = models.Observation(case_record_id=case.id, pulse=80, spo2=98, recorded_by="N. Patel")
    intake.score_observation(manual)
    db.add(manual)
    db.commit()
    db.refresh(case)
    assert len(case.observations) == 2


def test_scoring_works_on_an_observation_that_is_not_flushed_yet():
    """Column defaults land on flush, so an in-memory object can hold None."""
    observation = models.Observation(case_record_id=1, pulse=130, spo2=90)
    assert observation.consciousness is None
    intake.score_observation(observation)
    assert observation.news2_score == 5
    assert observation.risk_level == "medium"
