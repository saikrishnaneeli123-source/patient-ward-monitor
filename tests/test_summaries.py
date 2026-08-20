"""The discharge summary: compiled from the record, signed by a doctor, then frozen."""
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest

from app import audit, models, services, summaries
from app.models import NoteKind, Role, SummaryStatus


@pytest.fixture
def case(db, a_case_id):
    return db.get(models.CaseRecord, a_case_id)


@pytest.fixture
def doctor(db):
    return db.query(models.User).filter_by(username="doctor").one()


@pytest.fixture
def rich_case(db, case, doctor):
    """A case with the things a real summary has to pull together."""
    case.admission_date = date(2026, 8, 14)
    case.discharge_date = date(2026, 8, 20)
    case.consultant = "Dr S. Iyer"
    case.department = "General Medicine"
    case.chief_complaint = "Fever and cough for five days"
    case.provisional_diagnosis = "Community acquired pneumonia"
    case.comorbidities = ["Type 2 diabetes mellitus"]
    case.allergies = ["Penicillin (rash)"]
    case.medications = [
        {"name": "Amoxicillin", "dose": "500 mg", "route": "PO", "frequency": "TDS"},
        {"dose": "unreadable"},  # no drug name — must be dropped
    ]
    case.patient.date_of_birth = date(1972, 4, 3)
    case.patient.next_of_kin = "R. Rao"
    db.commit()

    base = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
    for hours, pulse, spo2, rr in ((0, 118, 92, 24), (24, 104, 94, 22), (48, 78, 97, 16)):
        obs = models.Observation(
            case_record_id=case.id, recorded_at=base + timedelta(hours=hours),
            respiratory_rate=rr, spo2=spo2, pulse=pulse, systolic_bp=110,
            temperature_c=38.4 if hours == 0 else 36.9,
            consciousness=models.Consciousness.alert, recorded_by="Test Nurse",
        )
        services.intake.score_observation(obs) if hasattr(services, "intake") else None
        from app import intake
        intake.score_observation(obs)
        db.add(obs)
    db.commit()
    db.refresh(case)
    return case


# --------------------------------------------------------------------------
# Compilation — nothing invented
# --------------------------------------------------------------------------

def test_the_summary_copies_the_record(rich_case):
    c = summaries.compile_summary(rich_case)
    assert c["patient"]["full_name"] == rich_case.patient.full_name
    assert c["clinical"]["diagnosis"] == "Community acquired pneumonia"
    assert c["clinical"]["allergies"] == ["Penicillin (rash)"]
    assert c["clinical"]["comorbidities"] == ["Type 2 diabetes mellitus"]
    assert c["admission"]["consultant"] == "Dr S. Iyer"


def test_missing_fields_are_marked_not_recorded_rather_than_smoothed_over(db, case):
    case.provisional_diagnosis = None
    case.chief_complaint = None
    db.commit()
    c = summaries.compile_summary(case)
    assert c["clinical"]["diagnosis"] == summaries.NOT_RECORDED
    assert c["clinical"]["presenting_complaint"] == summaries.NOT_RECORDED


def test_an_empty_record_produces_no_invented_clinical_text(db, case):
    """The failure mode to design out: prose that is not in the record."""
    case.provisional_diagnosis = None
    case.chief_complaint = None
    case.history = None
    case.allergies = []
    case.comorbidities = []
    case.medications = []
    db.commit()
    c = summaries.compile_summary(case)

    assert c["clinical"]["medications"] == ["None recorded"]
    assert c["clinical"]["allergies"] == ["None recorded"]
    assert c["clinical"]["comorbidities"] == ["None recorded"]
    # The only prose is the provenance note and the course narrative, and the
    # narrative only ever states counts and scores.
    assert "Not recorded" in c["clinical"]["diagnosis"]


def test_medications_without_a_drug_name_are_dropped(rich_case):
    meds = summaries.compile_summary(rich_case)["clinical"]["medications"]
    assert meds == ["Amoxicillin 500 mg PO TDS"]


def test_no_allergies_recorded_is_stated_explicitly(db, case):
    case.allergies = []
    db.commit()
    assert summaries.compile_summary(case)["clinical"]["allergies"] == ["None recorded"]


# --------------------------------------------------------------------------
# The clinical course
# --------------------------------------------------------------------------

def test_the_course_reports_first_peak_and_last_scores(rich_case):
    course = summaries.compile_summary(rich_case)["course"]
    assert course["recorded"] == 3
    assert course["first"]["score"] >= course["last"]["score"]
    assert course["peak"]["score"] >= course["first"]["score"]


def test_an_improving_patient_is_described_as_improved(rich_case):
    assert "improved" in summaries.compile_summary(rich_case)["course"]["narrative"]


def test_a_deteriorating_patient_is_described_as_deteriorated(db, case):
    from app import intake

    base = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
    for hours, pulse in ((0, 70), (12, 135)):
        obs = models.Observation(case_record_id=case.id, recorded_at=base + timedelta(hours=hours),
                                 pulse=pulse, spo2=97, consciousness=models.Consciousness.alert)
        intake.score_observation(obs)
        db.add(obs)
    db.commit()
    db.refresh(case)
    assert "deteriorated" in summaries.compile_summary(case)["course"]["narrative"]


def test_a_case_with_no_observations_says_so(db, case):
    for obs in list(case.observations):
        db.delete(obs)
    db.commit()
    db.refresh(case)
    course = summaries.compile_summary(case)["course"]
    assert course["recorded"] == 0
    assert "No scored observations" in course["narrative"]


def test_length_of_stay_is_computed_from_the_dates():
    assert summaries._length_of_stay(date(2026, 8, 14), date(2026, 8, 20)) == "6 days"
    assert summaries._length_of_stay(date(2026, 8, 14), date(2026, 8, 15)) == "1 day"
    assert summaries._length_of_stay(date(2026, 8, 14), date(2026, 8, 14)) == "Same-day"
    assert summaries._length_of_stay(None, date(2026, 8, 20)) == summaries.NOT_RECORDED


def test_inconsistent_dates_are_flagged_not_silently_negative():
    result = summaries._length_of_stay(date(2026, 8, 20), date(2026, 8, 14))
    assert "inconsistent" in result.lower()


# --------------------------------------------------------------------------
# Notes, escalations and outstanding tasks carry through
# --------------------------------------------------------------------------

def test_handover_notes_and_outstanding_tasks_reach_the_summary(db, case, doctor):
    services.add_note(db, case, doctor, kind=NoteKind.handover,
                      situation="Settled.", outstanding=["Chase bloods", "OT review"])
    services.add_note(db, case, doctor, body="Seen on the round.")
    db.refresh(case)
    c = summaries.compile_summary(case)
    assert len(c["notes"]) == 2
    assert c["outstanding"] == ["Chase bloods", "OT review"]


def test_duplicate_outstanding_tasks_are_listed_once(db, case, doctor):
    services.add_note(db, case, doctor, kind=NoteKind.handover, situation="A", outstanding=["Chase bloods"])
    services.add_note(db, case, doctor, kind=NoteKind.handover, situation="B", outstanding=["Chase bloods"])
    db.refresh(case)
    assert summaries.compile_summary(case)["outstanding"] == ["Chase bloods"]


def test_an_unverified_record_carries_a_caveat(db, case):
    assert case.verification == models.VerificationStatus.unverified
    assert summaries.compile_summary(case)["verification"]["caveat"] is not None


def test_a_verified_record_carries_no_caveat(db, case, doctor):
    services.verify_case(db, case, doctor)
    assert summaries.compile_summary(case)["verification"]["caveat"] is None


# --------------------------------------------------------------------------
# Versioning, signing and freezing
# --------------------------------------------------------------------------

def test_generating_produces_a_draft(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    assert summary.status == SummaryStatus.draft
    assert summary.version == 1
    assert summary.generated_by == "Test Doctor"
    assert not summary.is_signed


def test_regenerating_creates_a_new_version_and_keeps_the_old(db, rich_case, doctor):
    """Regression: version numbers collided at 1 when generated in one session."""
    first = services.generate_summary(db, rich_case, doctor)
    second = services.generate_summary(db, rich_case, doctor)
    db.refresh(rich_case)
    assert (first.version, second.version) == (1, 2)
    assert len(rich_case.discharge_summaries) == 2
    assert rich_case.latest_summary.id == second.id


def test_the_clinicians_own_text_carries_forward_to_a_new_version(db, rich_case, doctor):
    services.generate_summary(db, rich_case, doctor, follow_up="Clinic in 2 weeks.")
    second = services.generate_summary(db, rich_case, doctor)
    assert second.follow_up == "Clinic in 2 weeks."


def test_signing_records_who_and_when(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)
    assert summary.is_signed
    assert summary.signed_by == "Test Doctor"
    assert summary.signed_at is not None


def test_a_summary_cannot_be_signed_twice(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)
    with pytest.raises(services.SummaryError, match="Already signed"):
        services.sign_summary(db, summary, doctor)


def test_a_signed_summary_cannot_be_edited(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)
    with pytest.raises(services.SummaryError, match="cannot be edited"):
        services.update_summary(db, summary, doctor, follow_up="quietly changed")


def test_the_orm_refuses_to_alter_a_signed_summary(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)
    summary.content = {"clinical": {"diagnosis": "something else"}}
    with pytest.raises(audit.AuditLogTampered, match="signed"):
        db.commit()
    db.rollback()


def test_the_orm_refuses_to_delete_a_signed_summary(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)
    db.delete(summary)
    with pytest.raises(audit.AuditLogTampered):
        db.commit()
    db.rollback()


def test_raw_sql_cannot_rewrite_a_signed_summary(db, rich_case, doctor):
    from sqlalchemy import text

    from app.db import engine

    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text(f"UPDATE discharge_summaries SET follow_up = 'x' WHERE id = {summary.id}")
            )


def test_a_draft_can_still_be_edited_and_deleted(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    services.update_summary(db, summary, doctor, follow_up="GP in one week.")
    assert summary.follow_up == "GP in one week."
    db.delete(summary)
    db.commit()  # a draft is not frozen


def test_a_signed_summary_keeps_saying_what_was_signed(db, rich_case, doctor):
    """The content is a snapshot, not a live view of the record."""
    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)

    rich_case.provisional_diagnosis = "Changed after signing"
    db.commit()
    db.refresh(summary)
    assert summary.content["clinical"]["diagnosis"] == "Community acquired pneumonia"


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

def test_generating_and_signing_are_audited(db, rich_case, doctor):
    summary = services.generate_summary(db, rich_case, doctor)
    services.sign_summary(db, summary, doctor)
    actions = [e.action for e in db.query(models.AuditEvent).all()]
    assert audit.SUMMARY_GENERATED in actions
    assert audit.SUMMARY_SIGNED in actions
    assert audit.verify_chain(db).intact


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------

def test_generate_read_sign_through_the_api(client, a_case_id):
    created = client.post(f"/api/cases/{a_case_id}/discharge-summary",
                          json={"follow_up": "GP review in one week."})
    assert created.status_code == 201
    summary = created.json()
    assert summary["status"] == "draft"
    assert summary["content"]["patient"]["full_name"] == "Asha Rao"

    fetched = client.get(f"/api/cases/{a_case_id}/discharge-summary").json()
    assert fetched["id"] == summary["id"]

    signed = client.post(f"/api/discharge-summaries/{summary['id']}/sign").json()
    assert signed["status"] == "signed"
    assert signed["signed_by"] == "Test Doctor"


def test_editing_a_signed_summary_through_the_api_is_refused(client, a_case_id):
    summary = client.post(f"/api/cases/{a_case_id}/discharge-summary").json()
    client.post(f"/api/discharge-summaries/{summary['id']}/sign")
    response = client.patch(f"/api/discharge-summaries/{summary['id']}", json={"follow_up": "x"})
    assert response.status_code == 409


def test_a_case_with_no_summary_returns_404(client, a_case_id):
    assert client.get(f"/api/cases/{a_case_id}/discharge-summary").status_code == 404


def test_all_versions_are_listed_newest_first(client, a_case_id):
    client.post(f"/api/cases/{a_case_id}/discharge-summary")
    client.post(f"/api/cases/{a_case_id}/discharge-summary")
    versions = client.get(f"/api/cases/{a_case_id}/discharge-summaries").json()
    assert [v["version"] for v in versions] == [2, 1]


@pytest.mark.parametrize(
    "role,expected",
    [(Role.admin, 201), (Role.doctor, 201), (Role.nurse, 403), (Role.clerk, 403), (Role.readonly, 403)],
    ids=lambda v: str(getattr(v, "value", v)),
)
def test_only_discharging_roles_may_generate(client_as, a_case_id, role, expected):
    assert client_as(role).post(f"/api/cases/{a_case_id}/discharge-summary").status_code == expected


def test_a_nurse_may_read_a_summary_but_not_sign_it(client, client_as, a_case_id):
    summary = client.post(f"/api/cases/{a_case_id}/discharge-summary").json()
    nurse = client_as(Role.nurse)
    assert nurse.get(f"/api/cases/{a_case_id}/discharge-summary").status_code == 200
    assert nurse.post(f"/api/discharge-summaries/{summary['id']}/sign").status_code == 403


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def test_the_summary_page_renders_and_is_marked_draft(client, a_case_id):
    client.post(f"/api/cases/{a_case_id}/discharge-summary")
    page = client.get(f"/cases/{a_case_id}/summary")
    assert page.status_code == 200
    assert "DRAFT" in page.text
    assert "Not to be issued" in page.text


def test_a_signed_summary_page_shows_the_signature(client, a_case_id):
    summary = client.post(f"/api/cases/{a_case_id}/discharge-summary").json()
    client.post(f"/api/discharge-summaries/{summary['id']}/sign")
    page = client.get(f"/cases/{a_case_id}/summary").text
    assert "Signed by Test Doctor" in page
    assert "DRAFT" not in page


def test_the_case_page_offers_generation_to_a_doctor_only(client, client_as, a_case_id):
    assert "Generate a discharge summary" in client.get(f"/cases/{a_case_id}").text
    nurse_page = client_as(Role.nurse).get(f"/cases/{a_case_id}").text
    assert "Generate a discharge summary" not in nurse_page
    assert "A doctor generates and signs it" in nurse_page


def test_generating_through_the_form_redirects_to_the_summary(client, a_case_id):
    response = client.post(f"/cases/{a_case_id}/summary",
                           data={"follow_up": "GP in a week."}, follow_redirects=True)
    assert response.status_code == 200
    assert "GP in a week." in response.text


def test_the_printable_page_hides_the_app_chrome(client, a_case_id):
    client.post(f"/api/cases/{a_case_id}/discharge-summary")
    page = client.get(f"/cases/{a_case_id}/summary").text
    assert "no-print" in page, "print-hidden chrome must be marked"


def test_an_unknown_version_is_a_404(client, a_case_id):
    client.post(f"/api/cases/{a_case_id}/discharge-summary")
    assert client.get(f"/cases/{a_case_id}/summary?version=99").status_code == 404


def test_an_unrecorded_next_of_kin_reads_once_not_twice(db, case):
    case.patient.next_of_kin = None
    case.patient.next_of_kin_phone = None
    db.commit()
    assert summaries.compile_summary(case)["patient"]["next_of_kin"] == summaries.NOT_RECORDED


def test_next_of_kin_joins_name_and_phone(db, case):
    case.patient.next_of_kin = "R. Rao"
    case.patient.next_of_kin_phone = "98450 11223"
    db.commit()
    assert summaries.compile_summary(case)["patient"]["next_of_kin"] == "R. Rao · 98450 11223"


def test_the_signature_block_is_not_hidden_when_printing():
    """Regression: the print rule hid every <footer>, including the document's
    own signature block, so the printed summary came out unsigned."""
    css = (pathlib.Path("app/static/style.css")).read_text()
    print_block = css.split("@media print")[1]
    hidden = print_block.split("display: none")[0]
    assert ".app-footer" in hidden
    assert " footer," not in hidden, "a bare `footer` selector would hide the signature"
