"""Clinical notes and shift handover."""
import pytest

from app import audit, models, services
from app.models import NoteKind, Role, Shift


@pytest.fixture
def case(db, a_case_id):
    return db.get(models.CaseRecord, a_case_id)


@pytest.fixture
def nurse(db):
    return db.query(models.User).filter_by(username="nurse").one()


# --------------------------------------------------------------------------
# Writing notes
# --------------------------------------------------------------------------

def test_a_progress_note_records_its_author_and_role(db, case, nurse):
    note = services.add_note(db, case, nurse, body="Settled overnight, eating well.")
    assert note.author_name == "Test Nurse"
    assert note.author_role == "nurse"
    assert note.kind == NoteKind.progress
    assert note.created_at is not None


def test_a_handover_note_carries_sbar_and_outstanding_tasks(db, case, nurse):
    note = services.add_note(
        db, case, nurse,
        kind=NoteKind.handover,
        shift=Shift.night,
        situation="Breathless overnight, now settled on 2L.",
        background="COPD, admitted 3 days ago.",
        assessment="NEWS2 improving, 6 down from 9.",
        recommendation="Continue nebs, review sats on air in the morning.",
        outstanding=["Chase morning bloods", "  ", "Physio review"],
    )
    assert note.is_sbar
    assert note.shift == Shift.night
    # Blank lines are dropped rather than stored as empty tasks.
    assert note.outstanding == ["Chase morning bloods", "Physio review"]


def test_a_plain_progress_note_is_not_sbar(db, case, nurse):
    assert services.add_note(db, case, nurse, body="Ate lunch.").is_sbar is False


def test_notes_are_listed_newest_first(db, case, nurse):
    services.add_note(db, case, nurse, body="first")
    services.add_note(db, case, nurse, body="second")
    db.refresh(case)
    assert [n.body for n in case.clinical_notes] == ["second", "first"]


def test_writing_a_note_is_audited(db, case, nurse):
    services.add_note(db, case, nurse, kind=NoteKind.escalation, body="Called the registrar.")
    logged = db.query(models.AuditEvent).filter_by(action=audit.NOTE_ADDED).one()
    assert logged.actor_name == "Test Nurse"
    assert logged.entity_id == case.id
    assert logged.details["kind"] == "escalation"


# --------------------------------------------------------------------------
# Append-only
# --------------------------------------------------------------------------

def test_a_note_cannot_be_edited(db, case, nurse):
    note = services.add_note(db, case, nurse, body="Original wording.")
    note.body = "Quietly rewritten."
    with pytest.raises(audit.AuditLogTampered, match="append-only"):
        db.commit()
    db.rollback()


def test_a_note_cannot_be_deleted(db, case, nurse):
    note = services.add_note(db, case, nurse, body="Something inconvenient.")
    db.delete(note)
    with pytest.raises(audit.AuditLogTampered):
        db.commit()
    db.rollback()


def test_raw_sql_cannot_rewrite_a_note(db, case, nurse):
    from sqlalchemy import text

    from app.db import engine

    note = services.add_note(db, case, nurse, body="Original wording.")
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text(f"UPDATE notes SET body = 'x' WHERE id = {note.id}"))


def test_a_correction_supersedes_without_hiding_the_original(db, case, nurse):
    original = services.add_note(db, case, nurse, body="BP 120/80.")
    correction = services.add_note(
        db, case, nurse, kind=NoteKind.correction,
        body="BP was 102/80, not 120/80 — transcription error.",
        supersedes_id=original.id,
    )
    db.refresh(case)
    assert correction.supersedes_id == original.id
    # Both remain readable, the way a paper chart works.
    assert {n.id for n in case.clinical_notes} == {original.id, correction.id}


# --------------------------------------------------------------------------
# Handover receipt
# --------------------------------------------------------------------------

def test_a_new_handover_is_awaiting_receipt(db, case, nurse):
    note = services.add_note(db, case, nurse, kind=NoteKind.handover, situation="Stable.")
    assert note.awaiting_receipt


def test_a_progress_note_is_never_awaiting_receipt(db, case, nurse):
    assert services.add_note(db, case, nurse, body="Note.").awaiting_receipt is False


def test_receiving_a_handover_records_who_took_it(db, case, nurse):
    doctor = db.query(models.User).filter_by(username="doctor").one()
    note = services.add_note(db, case, nurse, kind=NoteKind.handover, situation="Stable.")
    services.receive_handover(db, note, doctor)

    assert note.received_by == "Test Doctor"
    assert note.received_at is not None
    assert not note.awaiting_receipt


def test_receiving_a_handover_is_audited(db, case, nurse):
    doctor = db.query(models.User).filter_by(username="doctor").one()
    note = services.add_note(db, case, nurse, kind=NoteKind.handover, situation="Stable.")
    services.receive_handover(db, note, doctor)
    logged = db.query(models.AuditEvent).filter_by(action=audit.HANDOVER_RECEIVED).one()
    assert "received by Test Doctor" in logged.summary


def test_outstanding_handovers_lists_only_unreceived_ones(db, case, nurse):
    doctor = db.query(models.User).filter_by(username="doctor").one()
    taken = services.add_note(db, case, nurse, kind=NoteKind.handover, situation="One.")
    waiting = services.add_note(db, case, nurse, kind=NoteKind.handover, situation="Two.")
    services.add_note(db, case, nurse, body="A progress note, not a handover.")
    services.receive_handover(db, taken, doctor)

    assert [n.id for n in services.outstanding_handovers(db)] == [waiting.id]


# --------------------------------------------------------------------------
# Through the API
# --------------------------------------------------------------------------

def test_post_and_list_notes(client, a_case_id):
    created = client.post(f"/api/cases/{a_case_id}/notes", json={
        "kind": "handover", "shift": "night",
        "situation": "Breathless overnight.", "recommendation": "Review sats on air.",
        "outstanding": ["Chase bloods"],
    })
    assert created.status_code == 201
    body = created.json()
    assert body["author_name"] == "Test Doctor"
    assert body["outstanding"] == ["Chase bloods"]

    listed = client.get(f"/api/cases/{a_case_id}/notes").json()
    assert len(listed) == 1


def test_an_empty_note_is_rejected(client, a_case_id):
    assert client.post(f"/api/cases/{a_case_id}/notes", json={"body": ""}).status_code == 400


def test_a_correction_must_belong_to_the_same_case(client, a_case_id):
    response = client.post(f"/api/cases/{a_case_id}/notes",
                           json={"body": "x", "kind": "correction", "supersedes_id": 9999})
    assert response.status_code == 400


def test_only_handover_notes_can_be_received(client, a_case_id):
    note = client.post(f"/api/cases/{a_case_id}/notes", json={"body": "Progress."}).json()
    assert client.post(f"/api/notes/{note['id']}/receive").status_code == 400


def test_a_handover_cannot_be_received_twice(client, client_as, a_case_id):
    note = client.post(f"/api/cases/{a_case_id}/notes",
                       json={"kind": "handover", "situation": "Stable."}).json()
    nurse = client_as(Role.nurse)
    assert nurse.post(f"/api/notes/{note['id']}/receive").status_code == 200
    assert nurse.post(f"/api/notes/{note['id']}/receive").status_code == 409


def test_the_author_cannot_receive_their_own_handover(client, a_case_id):
    """A receipt records that care passed to someone else."""
    note = client.post(f"/api/cases/{a_case_id}/notes",
                       json={"kind": "handover", "situation": "Stable."}).json()
    response = client.post(f"/api/notes/{note['id']}/receive")
    assert response.status_code == 400
    assert "incoming staff member" in response.json()["detail"]


def test_the_ui_does_not_offer_the_author_their_own_handover(client, client_as, a_case_id):
    client.post(f"/api/cases/{a_case_id}/notes", json={"kind": "handover", "situation": "Stable."})
    author_page = client.get(f"/cases/{a_case_id}").text
    assert "Take this handover" not in author_page
    assert "Waiting to be received by the incoming shift" in author_page

    incoming_page = client_as(Role.nurse).get(f"/cases/{a_case_id}").text
    assert "Take this handover" in incoming_page


def test_the_outstanding_handover_endpoint(client, a_case_id):
    client.post(f"/api/cases/{a_case_id}/notes", json={"kind": "handover", "situation": "Stable."})
    assert len(client.get("/api/handovers/outstanding").json()) == 1


@pytest.mark.parametrize(
    "role,expected",
    [(Role.admin, 201), (Role.doctor, 201), (Role.nurse, 201), (Role.clerk, 403), (Role.readonly, 403)],
    ids=lambda v: str(getattr(v, "value", v)),
)
def test_who_may_write_a_note(client_as, a_case_id, role, expected):
    response = client_as(role).post(f"/api/cases/{a_case_id}/notes", json={"body": "Seen."})
    assert response.status_code == expected


def test_a_clerk_may_read_notes_but_not_write_them(client_as, client, a_case_id):
    client.post(f"/api/cases/{a_case_id}/notes", json={"body": "Seen on the round."})
    clerk = client_as(Role.clerk)
    assert clerk.get(f"/api/cases/{a_case_id}/notes").status_code == 200
    assert clerk.post(f"/api/cases/{a_case_id}/notes", json={"body": "x"}).status_code == 403


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def test_the_case_page_shows_notes_and_flags_an_unreceived_handover(client, a_case_id):
    client.post(f"/api/cases/{a_case_id}/notes", json={
        "kind": "handover", "situation": "Breathless overnight.", "outstanding": ["Chase bloods"],
    })
    page = client.get(f"/cases/{a_case_id}").text
    assert "Breathless overnight." in page
    assert "Chase bloods" in page
    assert "not yet received" in page


def test_the_ui_hides_the_note_form_from_a_clerk(client_as, a_case_id):
    assert "Write a note" not in client_as(Role.clerk).get(f"/cases/{a_case_id}").text
    assert "Write a note" in client_as(Role.nurse).get(f"/cases/{a_case_id}").text


def test_writing_a_note_through_the_form(client, a_case_id):
    client.post(
        f"/cases/{a_case_id}/notes",
        data={"kind": "handover", "shift": "night", "situation": "Settled.",
              "outstanding": "Chase bloods\n\nPhysio review\n"},
        follow_redirects=True,
    )
    notes = client.get(f"/api/cases/{a_case_id}/notes").json()
    assert notes[0]["situation"] == "Settled."
    assert notes[0]["outstanding"] == ["Chase bloods", "Physio review"]


def test_the_audit_page_renders_for_an_admin(client_as, a_case_id):
    page = client_as(Role.admin).get("/audit")
    assert page.status_code == 200
    assert "Chain intact" in page.text
