"""A tamper-evident, append-only audit log.

Every entry stores the hash of the entry before it, so the log is a chain. Change
one field of one row, or delete a row, and every hash after it stops matching —
``verify_chain`` walks the log and reports the first entry that fails, along with
what it was.

Two layers stop the log being edited in the first place:

* the ORM refuses ``UPDATE`` and ``DELETE`` on audit rows and on clinical notes;
* on SQLite, database triggers refuse them too, so a stray ``sqlite3`` session or
  a raw ``db.execute`` cannot quietly rewrite history either.

Neither layer stops someone with filesystem access replacing the whole database —
nothing in-process can. What the chain gives you is *detection*: a rewritten log
cannot be made self-consistent without also recomputing every subsequent hash,
and a log whose entries were selectively removed announces itself.

Concurrency: the chain is linear, so two simultaneous writers could read the same
tail and fork it. SQLite serialises writers, so this holds as deployed. On
Postgres, take an advisory lock (or use SERIALIZABLE) around ``record``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session

from app.models import AuditEvent, DischargeSummary, Note, SummaryStatus, User

logger = logging.getLogger(__name__)

GENESIS = "0" * 64

# Actions worth naming, so the log is greppable and the UI can label it.
CASE_CREATED = "case.created"
CASE_UPDATED = "case.updated"
CASE_VERIFIED = "case.verified"
CASE_DISCHARGED = "case.discharged"
OBSERVATION_RECORDED = "observation.recorded"
ALERT_ACKNOWLEDGED = "alert.acknowledged"
UPLOAD_PROCESSED = "upload.processed"
NOTE_ADDED = "note.added"
SUMMARY_GENERATED = "summary.generated"
SUMMARY_UPDATED = "summary.updated"
SUMMARY_SIGNED = "summary.signed"
HANDOVER_RECEIVED = "handover.received"
USER_CREATED = "user.created"
USER_DEACTIVATED = "user.deactivated"
LOGIN_SUCCEEDED = "auth.login"
LOGIN_FAILED = "auth.login_failed"

ACTION_LABELS = {
    CASE_CREATED: "Case record created",
    CASE_UPDATED: "Case record edited",
    CASE_VERIFIED: "Case record verified",
    CASE_DISCHARGED: "Patient discharged",
    OBSERVATION_RECORDED: "Observations recorded",
    ALERT_ACKNOWLEDGED: "Alert acknowledged",
    UPLOAD_PROCESSED: "Case sheet processed",
    NOTE_ADDED: "Note added",
    SUMMARY_GENERATED: "Discharge summary generated",
    SUMMARY_UPDATED: "Discharge summary edited",
    SUMMARY_SIGNED: "Discharge summary signed",
    HANDOVER_RECEIVED: "Handover received",
    USER_CREATED: "Staff account created",
    USER_DEACTIVATED: "Staff account deactivated",
    LOGIN_SUCCEEDED: "Signed in",
    LOGIN_FAILED: "Failed sign-in attempt",
}

# The fields the hash covers. Anything outside this list is not protected, so
# keep every meaningful field in it.
HASHED_FIELDS = (
    "occurred_at", "actor_id", "actor_name", "actor_role",
    "action", "entity_type", "entity_id", "summary", "details",
)


class AuditLogTampered(RuntimeError):
    """Raised when something attempts to modify or delete an audit entry."""


def _canonical(payload: dict[str, Any]) -> str:
    """A stable serialisation, so the same content always hashes the same way."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash}:{_canonical(payload)}".encode("utf-8")).hexdigest()


def _stamp(value: datetime) -> str:
    """A timestamp representation that survives a database round trip.

    SQLite has no native timezone type, so a value written as tz-aware UTC comes
    back naive. Hashing ``isoformat()`` directly would therefore verify while the
    object is in memory and fail after every restart. Everything is written in
    UTC, so a naive value read back is UTC — normalise both forms to the same
    naive-UTC string before hashing.
    """
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _payload(entry: AuditEvent) -> dict[str, Any]:
    value = {}
    for name in HASHED_FIELDS:
        raw = getattr(entry, name)
        value[name] = _stamp(raw) if isinstance(raw, datetime) else raw
    return value


def latest(db: Session) -> AuditEvent | None:
    return db.execute(
        select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)
    ).scalar_one_or_none()


def record(
    db: Session,
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    summary: str,
    details: dict | None = None,
) -> AuditEvent:
    """Append one entry. Flushes, but leaves committing to the caller.

    The caller commits, so an audit entry lands in the same transaction as the
    change it describes — the change and its record either both happen or
    neither does.
    """
    tail = latest(db)
    entry = AuditEvent(
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "system",
        actor_role=actor.role.value if actor else "system",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        summary=summary,
        details=details or {},
        prev_hash=tail.entry_hash if tail else GENESIS,
    )
    # occurred_at has a Python-side default that is applied on flush; set it now
    # so the value that gets hashed is the value that gets stored.
    if entry.occurred_at is None:
        from app.models import utcnow

        entry.occurred_at = utcnow()
    entry.entry_hash = compute_hash(entry.prev_hash, _payload(entry))
    db.add(entry)
    db.flush()
    return entry


@dataclass
class ChainStatus:
    """The outcome of walking the log."""

    entries: int = 0
    intact: bool = True
    broken_at_id: int | None = None
    reason: str | None = None
    checked_through: datetime | None = None
    first_entry_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "entries": self.entries,
            "intact": self.intact,
            "broken_at_id": self.broken_at_id,
            "reason": self.reason,
            "checked_through": self.checked_through,
            "first_entry_at": self.first_entry_at,
        }


def verify_chain(db: Session) -> ChainStatus:
    """Walk the whole log and report the first entry that does not verify."""
    entries = list(db.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all())
    status = ChainStatus(entries=len(entries))
    if not entries:
        return status

    status.first_entry_at = entries[0].occurred_at
    expected_prev = GENESIS
    for entry in entries:
        if entry.prev_hash != expected_prev:
            status.intact = False
            status.broken_at_id = entry.id
            status.reason = (
                "Entry does not follow the one before it — an entry was removed, "
                "reordered, or inserted."
            )
            return status
        if compute_hash(entry.prev_hash, _payload(entry)) != entry.entry_hash:
            status.intact = False
            status.broken_at_id = entry.id
            status.reason = "Entry contents do not match its hash — this entry was altered."
            return status
        expected_prev = entry.entry_hash
        status.checked_through = entry.occurred_at
    return status


def field_changes(before: dict, after: dict) -> dict[str, list]:
    """The {field: [old, new]} diff recorded when a case record is edited."""
    changes = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if old_value != new_value:
            changes[key] = [old_value, new_value]
    return changes


# --------------------------------------------------------------------------
# Append-only enforcement
# --------------------------------------------------------------------------

APPEND_ONLY_MODELS = (AuditEvent, Note, DischargeSummary)

# Acknowledging a handover is the one thing that legitimately happens to a note
# after it is written, so those three columns — and nothing else — may change.
RECEIPT_COLUMNS = frozenset({"received_by_id", "received_by", "received_at"})


def _changed_columns(target) -> set[str]:
    state = inspect(target)
    return {
        attr.key
        for attr in state.mapper.column_attrs
        if state.attrs[attr.key].history.has_changes()
    }


def _refuse(mapper, connection, target):  # noqa: ARG001 - SQLAlchemy event signature
    name = type(target).__tablename__
    raise AuditLogTampered(
        f"{name} is append-only — entries cannot be changed or deleted. "
        "Record a new entry instead."
    )


def _refuse_note_content_change(mapper, connection, target):  # noqa: ARG001
    changed = _changed_columns(target)
    if changed - RECEIPT_COLUMNS:
        raise AuditLogTampered(
            "A note's content is append-only — "
            f"cannot change {', '.join(sorted(changed - RECEIPT_COLUMNS))}. "
            "Write a correction note instead."
        )


def _refuse_signed_summary_change(mapper, connection, target):  # noqa: ARG001
    """A draft may be edited and regenerated; a signed summary is frozen."""
    state = inspect(target)
    was_signed = state.attrs["status"].history.deleted or [target.status]
    if SummaryStatus.signed in was_signed:
        raise AuditLogTampered(
            "A signed discharge summary cannot be changed. Generate a new version instead."
        )
    if target.status == SummaryStatus.signed and _changed_columns(target) - SIGNING_COLUMNS:
        raise AuditLogTampered("Signing a summary must not change its content.")


def _refuse_signed_summary_delete(mapper, connection, target):  # noqa: ARG001
    if target.status == SummaryStatus.signed:
        raise AuditLogTampered("A signed discharge summary cannot be deleted.")


# The only columns that change when a draft is signed.
SIGNING_COLUMNS = frozenset({"status", "signed_at", "signed_by", "signed_by_id"})


def install_guards() -> None:
    """Refuse UPDATE and DELETE on append-only tables at the ORM layer."""
    update_guards = {
        AuditEvent: _refuse,
        Note: _refuse_note_content_change,
        DischargeSummary: _refuse_signed_summary_change,
    }
    delete_guards = {
        AuditEvent: _refuse,
        Note: _refuse,
        DischargeSummary: _refuse_signed_summary_delete,
    }
    for model in APPEND_ONLY_MODELS:
        on_update = update_guards[model]
        on_delete = delete_guards[model]
        if not event.contains(model, "before_update", on_update):
            event.listen(model, "before_update", on_update)
        if not event.contains(model, "before_delete", on_delete):
            event.listen(model, "before_delete", on_delete)


def install_db_triggers(engine) -> None:
    """Refuse UPDATE/DELETE in the database itself (SQLite).

    The UPDATE trigger allows only the handover-receipt columns to change, so a
    note's clinical content stays immutable even to raw SQL.
    """
    if engine.dialect.name != "sqlite":
        logger.warning(
            "Append-only database triggers are only installed on SQLite. On %s, apply "
            "the equivalent rules (revoke UPDATE/DELETE on audit_events and notes) "
            "before going live.",
            engine.dialect.name,
        )
        return

    unchanged = " AND ".join(
        f"(OLD.{col} IS NEW.{col})"
        for col in (
            "case_record_id", "kind", "shift", "body", "situation", "background",
            "assessment", "recommendation", "outstanding", "author_id",
            "author_name", "author_role", "created_at", "supersedes_id",
        )
    )
    statements = [
        """CREATE TRIGGER IF NOT EXISTS audit_events_no_update
           BEFORE UPDATE ON audit_events
           BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;""",
        """CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
           BEFORE DELETE ON audit_events
           BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;""",
        f"""CREATE TRIGGER IF NOT EXISTS notes_no_content_update
            BEFORE UPDATE ON notes
            WHEN NOT ({unchanged})
            BEGIN SELECT RAISE(ABORT, 'note content is append-only'); END;""",
        """CREATE TRIGGER IF NOT EXISTS notes_no_delete
           BEFORE DELETE ON notes
           BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;""",
        """CREATE TRIGGER IF NOT EXISTS signed_summary_no_update
           BEFORE UPDATE ON discharge_summaries
           WHEN OLD.status = 'signed'
           BEGIN SELECT RAISE(ABORT, 'a signed discharge summary is append-only'); END;""",
        """CREATE TRIGGER IF NOT EXISTS signed_summary_no_delete
           BEFORE DELETE ON discharge_summaries
           WHEN OLD.status = 'signed'
           BEGIN SELECT RAISE(ABORT, 'a signed discharge summary is append-only'); END;""",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
