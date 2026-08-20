"""Shared query helpers used by both the JSON API and the HTML views."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import models
from app.schemas import BoardRow

RISK_ORDER = {"high": 0, "medium": 1, "low-medium": 2, "low": 3, None: 4}


def case_query(
    db: Session,
    *,
    status: models.CaseStatus | None = models.CaseStatus.active,
    ward: str | None = None,
    verification: models.VerificationStatus | None = None,
) -> list[models.CaseRecord]:
    stmt = select(models.CaseRecord).options(
        selectinload(models.CaseRecord.patient),
        selectinload(models.CaseRecord.observations),
        selectinload(models.CaseRecord.alerts),
    )
    if status is not None:
        stmt = stmt.where(models.CaseRecord.status == status)
    if ward:
        stmt = stmt.where(models.CaseRecord.ward_name == ward)
    if verification is not None:
        stmt = stmt.where(models.CaseRecord.verification == verification)
    return list(db.execute(stmt.order_by(models.CaseRecord.id.desc())).scalars().all())


def to_board_row(case: models.CaseRecord) -> BoardRow:
    latest = case.latest_observation
    return BoardRow(
        case_record_id=case.id,
        case_number=case.case_number,
        patient_name=case.patient.full_name,
        mrn=case.patient.mrn,
        ward_name=case.ward_name,
        bed_label=case.bed_label,
        age_years=case.patient.age_years,
        sex=case.patient.sex,
        provisional_diagnosis=case.provisional_diagnosis,
        verification=case.verification,
        news2_score=latest.news2_score if latest else None,
        risk_level=latest.risk_level if latest else None,
        last_observed_at=latest.recorded_at if latest else None,
        open_alerts=sum(1 for a in case.alerts if a.acknowledged_at is None),
        allergies=case.allergies or [],
    )


def board(db: Session, *, ward: str | None = None) -> list[BoardRow]:
    """Ward board sorted sickest-first, then by bed."""
    rows = [to_board_row(c) for c in case_query(db, ward=ward)]
    rows.sort(key=lambda r: (RISK_ORDER.get(r.risk_level, 4), _bed_key(r.bed_label)))
    return rows


def _bed_key(label: str | None) -> tuple:
    """Sort bed 2 before bed 10 while tolerating labels like '3A'."""
    if not label:
        return (1, 0, "")
    digits = "".join(c for c in label if c.isdigit())
    return (0, int(digits) if digits else 0, label)


def open_alerts(db: Session, limit: int = 50) -> list[models.Alert]:
    stmt = (
        select(models.Alert)
        .where(models.Alert.acknowledged_at.is_(None))
        .order_by(models.Alert.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def wards(db: Session) -> list[str]:
    stmt = (
        select(models.CaseRecord.ward_name)
        .where(models.CaseRecord.ward_name.is_not(None))
        .distinct()
        .order_by(models.CaseRecord.ward_name)
    )
    return [w for w in db.execute(stmt).scalars().all() if w]


def get_case(db: Session, case_id: int) -> models.CaseRecord | None:
    return db.get(models.CaseRecord, case_id)


def verify_case(db: Session, case: models.CaseRecord, user: models.User) -> models.CaseRecord:
    """Record who confirmed this record against the scan, and when."""
    case.verification = models.VerificationStatus.verified
    case.verified_by = user.full_name
    case.verified_by_id = user.id
    case.verified_at = datetime.now(timezone.utc)
    db.commit()
    return case


def acknowledge(db: Session, alert: models.Alert, user: models.User) -> models.Alert:
    alert.acknowledged_at = datetime.now(timezone.utc)
    alert.acknowledged_by = user.full_name
    alert.acknowledged_by_id = user.id
    db.commit()
    return alert


# Values clinicians write to mean "no allergies" — these must not render as a
# red allergy warning on the board, or the flag stops meaning anything.
_NO_ALLERGY_TOKENS = {
    "none", "none known", "no known allergies", "nka", "nkda", "nil",
    "nil known", "no", "n/a", "na", "not known", "none reported",
}


def real_allergies(allergies: list | None) -> list[str]:
    """Filter out 'none known'-style entries, keeping genuine allergies only."""
    if not allergies:
        return []
    kept = []
    for entry in allergies:
        text = entry if isinstance(entry, str) else str(entry)
        normalised = text.strip().lower().rstrip(".").replace("-", " ")
        if normalised and normalised not in _NO_ALLERGY_TOKENS:
            kept.append(text)
    return kept
