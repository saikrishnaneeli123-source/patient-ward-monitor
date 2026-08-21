"""Turn stored medicines into the actual clock times an alarm should ring.

Doses are never written into the database in advance. A dose is a pure function
of (medicine, date, the patient's daily routine), so the schedule is computed on
demand and only *deviations* -- taken, skipped, snoozed -- are stored. That keeps
an edit to a medicine or to the patient's meal times instantly correct for every
future day, with no rebuild step and no stale rows.

Every dose gets a stable, deterministic id (``<medicine_id>:<date>:<HHMM>``) so
the phone, the log and the alarm all agree on which dose they are talking about.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date as Date, datetime, time as Time, timedelta
from typing import Any, Iterable

from .parser import (
    MORNING, AFTERNOON, EVENING, NIGHT, BEDTIME,
    FOOD_AFTER, FOOD_BEFORE, FOOD_EMPTY, FOOD_WITH,
)

# The routine an elderly patient actually keeps. Every one of these is editable
# in Settings, because "morning" means 6am for one patient and 9am for another.
DEFAULT_ROUTINE: dict[str, str] = {
    "wake": "07:00",
    "breakfast": "08:00",
    "lunch": "13:00",
    "evening": "17:00",
    "dinner": "20:00",
    "bed": "22:00",
}

SLOT_ANCHOR: dict[str, str] = {
    MORNING: "breakfast",
    AFTERNOON: "lunch",
    EVENING: "evening",
    NIGHT: "dinner",
    BEDTIME: "bed",
}

SLOT_LABEL: dict[str, str] = {
    MORNING: "Morning",
    AFTERNOON: "Afternoon",
    EVENING: "Evening",
    NIGHT: "Night",
    BEDTIME: "Bedtime",
}

# Minutes to shift a dose relative to the anchor meal.
FOOD_OFFSET: dict[str, int] = {
    FOOD_BEFORE: -30,
    FOOD_EMPTY: -45,
    FOOD_AFTER: 30,
    FOOD_WITH: 0,
}

FOOD_LABEL: dict[str, str] = {
    FOOD_BEFORE: "before food",
    FOOD_EMPTY: "on an empty stomach",
    FOOD_AFTER: "after food",
    FOOD_WITH: "with food",
}

STATUS_PENDING = "pending"
STATUS_TAKEN = "taken"
STATUS_SKIPPED = "skipped"
STATUS_MISSED = "missed"
STATUS_SNOOZED = "snoozed"

# How long after its time a dose stays "due" before it counts as missed.
MISSED_AFTER_MINUTES = 90


@dataclass
class Dose:
    dose_id: str
    medicine_id: int
    medicine_name: str
    form: str
    strength: str
    slot: str
    slot_label: str
    scheduled_at: str        # ISO 8601, local time
    time_label: str          # "8:30 AM"
    quantity: float
    unit: str
    food: str
    food_label: str
    instructions: str
    status: str = STATUS_PENDING
    acted_at: str | None = None
    snooze_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_hhmm(value: str, fallback: str) -> Time:
    try:
        hours, _, minutes = str(value).partition(":")
        return Time(hour=int(hours) % 24, minute=int(minutes or 0) % 60)
    except (ValueError, TypeError):
        hours, _, minutes = fallback.partition(":")
        return Time(hour=int(hours), minute=int(minutes))


def routine_with_defaults(routine: dict[str, str] | None) -> dict[str, str]:
    merged = dict(DEFAULT_ROUTINE)
    for key, value in (routine or {}).items():
        if key in DEFAULT_ROUTINE and value:
            merged[key] = value
    return merged


def format_time(moment: datetime) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def _slot_time(slot: str, routine: dict[str, str], food: str) -> Time:
    anchor_key = SLOT_ANCHOR.get(slot, "breakfast")
    anchor = _parse_hhmm(routine.get(anchor_key, DEFAULT_ROUTINE[anchor_key]),
                         DEFAULT_ROUTINE[anchor_key])
    # A bedtime dose is anchored to sleep, not to a meal, so food timing does
    # not move it.
    offset = 0 if slot == BEDTIME else FOOD_OFFSET.get(food, 0)
    shifted = datetime.combine(Date(2000, 1, 1), anchor) + timedelta(minutes=offset)
    return shifted.time()


def make_dose_id(medicine_id: int, day: Date, moment: datetime) -> str:
    return f"{medicine_id}:{day.isoformat()}:{moment.strftime('%H%M')}"


# ---------------------------------------------------------------------------
# Course window
# ---------------------------------------------------------------------------

def course_end_date(medicine: dict[str, Any]) -> Date | None:
    """Last day of the course, or ``None`` for an ongoing medicine."""
    duration = medicine.get("duration_days")
    if not duration:
        return None
    start = _start_date(medicine)
    return start + timedelta(days=int(duration) - 1)


def _start_date(medicine: dict[str, Any]) -> Date:
    raw = medicine.get("start_date")
    if isinstance(raw, Date):
        return raw
    try:
        return Date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return Date.today()


def is_active_on(medicine: dict[str, Any], day: Date) -> bool:
    """Is this medicine due on this date at all?"""
    if not medicine.get("active", True):
        return False
    if medicine.get("prn"):
        return False                      # only when needed: never alarmed
    start = _start_date(medicine)
    if day < start:
        return False
    end = course_end_date(medicine)
    if end and day > end:
        return False
    step = int(medicine.get("day_interval") or 1)
    if step > 1 and (day - start).days % step != 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Building a day
# ---------------------------------------------------------------------------

def _interval_times(interval_hours: int, routine: dict[str, str]) -> list[Time]:
    wake = _parse_hhmm(routine.get("wake", DEFAULT_ROUTINE["wake"]), DEFAULT_ROUTINE["wake"])
    count = max(1, 24 // max(1, interval_hours))
    times: list[Time] = []
    for step in range(count):
        moment = datetime.combine(Date(2000, 1, 1), wake) + timedelta(hours=interval_hours * step)
        times.append(moment.time())
    return times


def doses_for_medicine(medicine: dict[str, Any], day: Date,
                       routine: dict[str, str] | None = None) -> list[Dose]:
    """Every dose of one medicine on one day, in clock order."""
    if not is_active_on(medicine, day):
        return []

    routine = routine_with_defaults(routine)
    food = medicine.get("food") or "any"
    slot_qty: dict[str, float] = medicine.get("slot_qty") or {}
    unit = medicine.get("dose_unit") or "tablet"
    default_qty = float(medicine.get("dose_qty") or 1)
    doses: list[Dose] = []

    interval_hours = medicine.get("interval_hours")
    if interval_hours:
        entries = [("", moment) for moment in _interval_times(int(interval_hours), routine)]
    else:
        entries = []
        for slot in medicine.get("slots") or []:
            entries.append((slot, _slot_time(slot, routine, food)))

    for slot, clock in entries:
        when = datetime.combine(day, clock)
        quantity = float(slot_qty.get(slot, default_qty)) if slot else default_qty
        if quantity <= 0:
            continue
        doses.append(Dose(
            dose_id=make_dose_id(int(medicine["id"]), day, when),
            medicine_id=int(medicine["id"]),
            medicine_name=medicine.get("name", "Medicine"),
            form=medicine.get("form", "tablet"),
            strength=medicine.get("strength", ""),
            slot=slot or "interval",
            slot_label=SLOT_LABEL.get(slot, format_time(when)),
            scheduled_at=when.isoformat(timespec="minutes"),
            time_label=format_time(when),
            quantity=quantity,
            unit=unit,
            food=food,
            food_label=FOOD_LABEL.get(food, ""),
            instructions=medicine.get("instructions") or "",
        ))

    doses.sort(key=lambda dose: dose.scheduled_at)
    return doses


def build_day_schedule(medicines: Iterable[dict[str, Any]], day: Date,
                       routine: dict[str, str] | None = None) -> list[Dose]:
    """The whole day's doses for a patient, across all their medicines."""
    doses: list[Dose] = []
    for medicine in medicines:
        doses.extend(doses_for_medicine(medicine, day, routine))
    doses.sort(key=lambda dose: (dose.scheduled_at, dose.medicine_name))
    return doses


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def apply_log(doses: list[Dose], log: dict[str, dict[str, Any]], now: datetime) -> list[Dose]:
    """Overlay what actually happened, and age un-acted past doses into 'missed'."""
    for dose in doses:
        entry = log.get(dose.dose_id)
        if entry:
            dose.status = entry.get("status", STATUS_PENDING)
            dose.acted_at = entry.get("acted_at")
            dose.snooze_until = entry.get("snooze_until")

        if dose.status in (STATUS_TAKEN, STATUS_SKIPPED):
            continue

        due_at = datetime.fromisoformat(dose.scheduled_at)
        if dose.status == STATUS_SNOOZED and dose.snooze_until:
            due_at = max(due_at, datetime.fromisoformat(dose.snooze_until))
        if now > due_at + timedelta(minutes=MISSED_AFTER_MINUTES):
            dose.status = STATUS_MISSED
    return doses


def due_now(doses: Iterable[Dose], now: datetime,
            grace_minutes: int = MISSED_AFTER_MINUTES) -> list[Dose]:
    """Doses whose alarm should be ringing right now."""
    ringing: list[Dose] = []
    for dose in doses:
        if dose.status in (STATUS_TAKEN, STATUS_SKIPPED, STATUS_MISSED):
            continue
        due_at = datetime.fromisoformat(dose.scheduled_at)
        if dose.status == STATUS_SNOOZED and dose.snooze_until:
            due_at = datetime.fromisoformat(dose.snooze_until)
        if due_at <= now <= due_at + timedelta(minutes=grace_minutes):
            ringing.append(dose)
    return ringing


def next_dose(doses: Iterable[Dose], now: datetime) -> Dose | None:
    """The next dose still ahead of the patient today."""
    upcoming = [
        dose for dose in doses
        if dose.status not in (STATUS_TAKEN, STATUS_SKIPPED, STATUS_MISSED)
        and datetime.fromisoformat(dose.scheduled_at) >= now
    ]
    upcoming.sort(key=lambda dose: dose.scheduled_at)
    return upcoming[0] if upcoming else None


def adherence(medicines: Iterable[dict[str, Any]], log: dict[str, dict[str, Any]],
              start: Date, end: Date, routine: dict[str, str] | None,
              now: datetime) -> dict[str, Any]:
    """How well the patient kept to the plan between two dates, inclusive."""
    medicines = list(medicines)
    days: list[dict[str, Any]] = []
    totals = {STATUS_TAKEN: 0, STATUS_SKIPPED: 0, STATUS_MISSED: 0, STATUS_PENDING: 0}

    day = start
    while day <= end:
        doses = apply_log(build_day_schedule(medicines, day, routine), log, now)
        counts = {STATUS_TAKEN: 0, STATUS_SKIPPED: 0, STATUS_MISSED: 0, STATUS_PENDING: 0}
        for dose in doses:
            key = STATUS_PENDING if dose.status == STATUS_SNOOZED else dose.status
            counts[key] = counts.get(key, 0) + 1
            totals[key] = totals.get(key, 0) + 1
        days.append({
            "date": day.isoformat(),
            "scheduled": len(doses),
            **{f"{k}": v for k, v in counts.items()},
        })
        day += timedelta(days=1)

    settled = totals[STATUS_TAKEN] + totals[STATUS_SKIPPED] + totals[STATUS_MISSED]
    percent = round(100 * totals[STATUS_TAKEN] / settled) if settled else None
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "taken": totals[STATUS_TAKEN],
        "skipped": totals[STATUS_SKIPPED],
        "missed": totals[STATUS_MISSED],
        "pending": totals[STATUS_PENDING],
        "adherence_percent": percent,
        "days": days,
    }
