"""The append-only, hash-chained audit log."""
import pytest
from sqlalchemy import text

from app import audit, models
from app.db import engine
from app.models import Role


def entry(db, user, action=audit.CASE_VERIFIED, summary="did a thing", **kwargs):
    return audit.record(
        db, actor=user, action=action, entity_type="case", entity_id=1, summary=summary, **kwargs
    )


@pytest.fixture
def user(db):
    return db.query(models.User).filter_by(username="doctor").one()


# --------------------------------------------------------------------------
# The chain
# --------------------------------------------------------------------------

def test_the_first_entry_links_to_the_genesis_hash(db, user):
    first = entry(db, user)
    db.commit()
    assert first.prev_hash == audit.GENESIS
    assert len(first.entry_hash) == 64


def test_each_entry_links_to_the_one_before_it(db, user):
    a, b, c = entry(db, user), entry(db, user), entry(db, user)
    db.commit()
    assert b.prev_hash == a.entry_hash
    assert c.prev_hash == b.entry_hash


def test_identical_content_still_produces_different_hashes(db, user):
    """Because the chain position is part of the hash."""
    a = entry(db, user, summary="same")
    b = entry(db, user, summary="same")
    db.commit()
    assert a.entry_hash != b.entry_hash


def test_an_empty_log_verifies(db):
    status = audit.verify_chain(db)
    assert status.intact and status.entries == 0


def test_a_healthy_log_verifies(db, user):
    for i in range(5):
        entry(db, user, summary=f"event {i}")
    db.commit()
    status = audit.verify_chain(db)
    assert status.intact
    assert status.entries == 5
    assert status.broken_at_id is None


def test_the_actor_name_and_role_are_copied_in(db, user):
    """So the log still reads correctly after an account is renamed or removed."""
    logged = entry(db, user)
    db.commit()
    assert logged.actor_name == "Test Doctor"
    assert logged.actor_role == "doctor"


def test_a_system_action_is_logged_without_an_actor(db):
    logged = audit.record(
        db, actor=None, action=audit.UPLOAD_PROCESSED, entity_type="upload",
        entity_id=1, summary="batch job",
    )
    db.commit()
    assert logged.actor_name == "system"
    assert logged.actor_id is None


# --------------------------------------------------------------------------
# Tamper detection
# --------------------------------------------------------------------------

def _drop_triggers():
    """Simulate an attacker with database access, to prove detection works."""
    with engine.begin() as connection:
        for name in ("audit_events_no_update", "audit_events_no_delete"):
            connection.execute(text(f"DROP TRIGGER IF EXISTS {name}"))


def test_altering_an_entry_is_detected(db, user):
    for i in range(4):
        entry(db, user, summary=f"event {i}")
    db.commit()
    assert audit.verify_chain(db).intact

    _drop_triggers()
    with engine.begin() as connection:
        connection.execute(text("UPDATE audit_events SET summary = 'rewritten' WHERE id = 2"))
    db.expire_all()

    status = audit.verify_chain(db)
    assert not status.intact
    assert status.broken_at_id == 2
    assert "altered" in status.reason


def test_deleting_an_entry_is_detected(db, user):
    for i in range(4):
        entry(db, user, summary=f"event {i}")
    db.commit()

    _drop_triggers()
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM audit_events WHERE id = 2"))
    db.expire_all()

    status = audit.verify_chain(db)
    assert not status.intact
    assert status.broken_at_id == 3
    assert "removed" in status.reason


def test_entries_before_the_break_still_verify(db, user):
    for i in range(5):
        entry(db, user, summary=f"event {i}")
    db.commit()

    _drop_triggers()
    with engine.begin() as connection:
        connection.execute(text("UPDATE audit_events SET summary = 'x' WHERE id = 4"))
    db.expire_all()

    status = audit.verify_chain(db)
    assert status.broken_at_id == 4, "entries 1-3 are unaffected"


def test_changing_the_details_payload_is_detected(db, user):
    entry(db, user, details={"changes": {"bed_label": ["4", "5"]}})
    db.commit()

    _drop_triggers()
    with engine.begin() as connection:
        connection.execute(text("""UPDATE audit_events SET details = '{"changes": {}}' WHERE id = 1"""))
    db.expire_all()

    assert not audit.verify_chain(db).intact


# --------------------------------------------------------------------------
# Append-only enforcement
# --------------------------------------------------------------------------

def test_the_orm_refuses_to_update_an_entry(db, user):
    logged = entry(db, user)
    db.commit()
    logged.summary = "rewritten"
    with pytest.raises(audit.AuditLogTampered):
        db.commit()
    db.rollback()


def test_the_orm_refuses_to_delete_an_entry(db, user):
    logged = entry(db, user)
    db.commit()
    db.delete(logged)
    with pytest.raises(audit.AuditLogTampered):
        db.commit()
    db.rollback()


def test_raw_sql_cannot_update_an_entry(db, user):
    entry(db, user)
    db.commit()
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text("UPDATE audit_events SET summary = 'x' WHERE id = 1"))


def test_raw_sql_cannot_delete_an_entry(db, user):
    entry(db, user)
    db.commit()
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_events WHERE id = 1"))


# --------------------------------------------------------------------------
# What gets logged
# --------------------------------------------------------------------------

def test_field_changes_records_both_the_old_and_new_value():
    changes = audit.field_changes({"bed": "4", "ward": "A"}, {"bed": "9", "ward": "A"})
    assert changes == {"bed": ["4", "9"]}


def test_signing_in_is_logged(db, client):
    events = [e.action for e in audit.verify_chain(db) and db.query(models.AuditEvent).all()]
    assert audit.LOGIN_SUCCEEDED in events


def test_a_failed_sign_in_is_logged_without_naming_an_actor(db, anon_client):
    secret = "hunter2-should-never-be-logged"
    anon_client.post("/login", data={"username": "doctor", "password": secret})
    logged = db.query(models.AuditEvent).filter_by(action=audit.LOGIN_FAILED).one()
    assert logged.actor_id is None
    assert "doctor" in logged.summary
    # The attempted password must never reach the log.
    assert secret not in str(logged.details)
    assert secret not in logged.summary


def test_verifying_a_case_is_logged(client, db, a_case_id):
    client.post(f"/api/cases/{a_case_id}/verify")
    logged = db.query(models.AuditEvent).filter_by(action=audit.CASE_VERIFIED).one()
    assert logged.actor_name == "Test Doctor"
    assert logged.entity_id == a_case_id


def test_editing_a_case_logs_the_before_and_after(client, db, a_case_id):
    client.patch(f"/api/cases/{a_case_id}", json={"bed_label": "12", "consultant": "Dr Reed"})
    logged = db.query(models.AuditEvent).filter_by(action=audit.CASE_UPDATED).order_by(
        models.AuditEvent.id.desc()
    ).first()
    changes = logged.details["changes"]
    assert changes["bed_label"][1] == "12"
    assert changes["consultant"] == [None, "Dr Reed"]


def test_an_unchanged_edit_logs_nothing(client, db, a_case_id):
    before = db.query(models.AuditEvent).count()
    case = client.get(f"/api/cases/{a_case_id}").json()
    client.patch(f"/api/cases/{a_case_id}", json={"bed_label": case["bed_label"]})
    assert db.query(models.AuditEvent).count() == before


def test_discharging_is_logged_as_a_discharge_not_an_edit(client, db, a_case_id):
    client.patch(f"/api/cases/{a_case_id}", json={"status": "discharged"})
    assert db.query(models.AuditEvent).filter_by(action=audit.CASE_DISCHARGED).count() == 1


def test_recording_observations_is_logged(client, db, a_case_id):
    client.post(f"/api/cases/{a_case_id}/observations", json={"pulse": 130, "spo2": 88})
    logged = db.query(models.AuditEvent).filter_by(action=audit.OBSERVATION_RECORDED).one()
    assert logged.details["news2"] is not None
    assert logged.details["alerts_raised"] >= 0


def test_uploading_logs_the_batch_and_each_record(client, db, png_bytes, fake_extractor):
    from app.extraction import CaseSheetBatch, ExtractedCaseSheet

    fake_extractor(CaseSheetBatch(sheets=[
        ExtractedCaseSheet(full_name="A One", mrn="A-1", confidence=0.9),
        ExtractedCaseSheet(full_name="B Two", mrn="B-2", confidence=0.8),
    ]))
    client.post("/api/uploads", files=[("files", ("s.png", png_bytes, "image/png"))])

    assert db.query(models.AuditEvent).filter_by(action=audit.UPLOAD_PROCESSED).count() == 1
    assert db.query(models.AuditEvent).filter_by(action=audit.CASE_CREATED).count() == 2


def test_the_log_stays_intact_across_a_realistic_session(client, db, a_case_id):
    client.post(f"/api/cases/{a_case_id}/verify")
    client.post(f"/api/cases/{a_case_id}/observations", json={"pulse": 120, "spo2": 90})
    client.patch(f"/api/cases/{a_case_id}", json={"bed_label": "7"})
    client.post(f"/api/cases/{a_case_id}/notes", json={"body": "Reviewed on the round."})

    status = audit.verify_chain(db)
    assert status.intact
    assert status.entries >= 5


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------

def test_only_an_admin_may_read_the_log(client_as):
    assert client_as(Role.admin).get("/api/audit").status_code == 200
    for role in (Role.doctor, Role.nurse, Role.clerk, Role.readonly):
        assert client_as(role).get("/api/audit").status_code == 403


def test_anyone_who_can_view_a_case_sees_its_own_trail(client_as, a_case_id):
    response = client_as(Role.readonly).get(f"/api/cases/{a_case_id}/audit")
    assert response.status_code == 200
    assert any(e["action"] == audit.CASE_CREATED for e in response.json())


def test_the_verify_endpoint_reports_the_chain_status(client_as, a_case_id):
    body = client_as(Role.admin).get("/api/audit/verify").json()
    assert body["intact"] is True
    assert body["entries"] > 0


def test_the_chain_still_verifies_after_a_reload_from_the_database(db, user):
    """SQLite returns naive datetimes; the hash must not depend on that.

    Without this the log verifies in memory and fails after every restart.
    """
    from app.db import SessionLocal

    for i in range(3):
        entry(db, user, summary=f"event {i}")
    db.commit()
    db.close()

    fresh = SessionLocal()  # a new session, nothing cached
    try:
        status = audit.verify_chain(fresh)
        assert status.intact, status.reason
        assert status.entries == 3
    finally:
        fresh.close()


def test_hashing_is_stable_whether_the_timestamp_is_aware_or_naive():
    from datetime import datetime, timezone

    aware = datetime(2026, 3, 11, 8, 30, 15, 123456, tzinfo=timezone.utc)
    naive = datetime(2026, 3, 11, 8, 30, 15, 123456)
    assert audit._stamp(aware) == audit._stamp(naive)
