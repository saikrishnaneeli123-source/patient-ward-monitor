"""Alarm times must follow the patient's own routine, not a hard-coded clock."""

from datetime import date, datetime

import pytest

from backend import scheduler
from backend.scheduler import (
    build_day_schedule, doses_for_medicine, apply_log, due_now, next_dose,
    adherence, course_end_date, is_active_on,
)

ROUTINE = {"wake": "06:00", "breakfast": "08:00", "lunch": "13:00",
           "evening": "17:00", "dinner": "20:00", "bed": "22:00"}


def medicine(**overrides):
    base = dict(id=1, name="Metformin", form="tablet", strength="500 mg", dose_qty=1.0,
                dose_unit="tablet", slots=["morning", "night"],
                slot_qty={"morning": 1.0, "night": 1.0}, interval_hours=None, day_interval=1,
                food="any", duration_days=None, start_date="2026-08-20", prn=False,
                instructions="", active=True)
    base.update(overrides)
    return base


def test_slots_follow_the_patients_meal_times():
    doses = doses_for_medicine(medicine(), date(2026, 8, 20), ROUTINE)
    assert [d.time_label for d in doses] == ["8:00 AM", "8:00 PM"]

    late = dict(ROUTINE, breakfast="10:00", dinner="21:30")
    doses = doses_for_medicine(medicine(), date(2026, 8, 20), late)
    assert [d.time_label for d in doses] == ["10:00 AM", "9:30 PM"]


def test_after_food_pushes_the_alarm_later_and_before_food_pulls_it_earlier():
    after = doses_for_medicine(medicine(food="after_food"), date(2026, 8, 20), ROUTINE)
    before = doses_for_medicine(medicine(food="before_food"), date(2026, 8, 20), ROUTINE)
    assert after[0].time_label == "8:30 AM"
    assert before[0].time_label == "7:30 AM"


def test_empty_stomach_dose_is_45_minutes_before_the_meal():
    doses = doses_for_medicine(medicine(food="empty_stomach"), date(2026, 8, 20), ROUTINE)
    assert doses[0].time_label == "7:15 AM"


def test_bedtime_dose_ignores_food_timing():
    med = medicine(slots=["bedtime"], slot_qty={"bedtime": 1.0}, food="after_food")
    doses = doses_for_medicine(med, date(2026, 8, 20), ROUTINE)
    assert doses[0].time_label == "10:00 PM"


def test_hourly_interval_spreads_across_the_day_from_wake_time():
    med = medicine(slots=[], slot_qty={}, interval_hours=8)
    doses = doses_for_medicine(med, date(2026, 8, 20), ROUTINE)
    assert [d.time_label for d in doses] == ["6:00 AM", "2:00 PM", "10:00 PM"]


def test_per_slot_quantity_is_respected():
    med = medicine(slot_qty={"morning": 0.5, "night": 1.0})
    doses = doses_for_medicine(med, date(2026, 8, 20), ROUTINE)
    assert [d.quantity for d in doses] == [0.5, 1.0]


def test_course_length_stops_the_alarms():
    med = medicine(duration_days=3, start_date="2026-08-20")
    assert course_end_date(med) == date(2026, 8, 22)
    assert is_active_on(med, date(2026, 8, 22)) is True
    assert is_active_on(med, date(2026, 8, 23)) is False


def test_medicine_does_not_alarm_before_it_starts():
    assert is_active_on(medicine(start_date="2026-08-25"), date(2026, 8, 20)) is False


def test_alternate_day_medicine_alarms_every_other_day():
    med = medicine(day_interval=2, start_date="2026-08-20")
    assert is_active_on(med, date(2026, 8, 20)) is True
    assert is_active_on(med, date(2026, 8, 21)) is False
    assert is_active_on(med, date(2026, 8, 22)) is True


def test_weekly_medicine():
    med = medicine(day_interval=7, start_date="2026-08-20")
    assert is_active_on(med, date(2026, 8, 27)) is True
    assert is_active_on(med, date(2026, 8, 26)) is False


def test_sos_medicine_never_produces_an_alarm():
    assert doses_for_medicine(medicine(prn=True), date(2026, 8, 20), ROUTINE) == []


def test_stopped_medicine_produces_no_alarm():
    assert doses_for_medicine(medicine(active=False), date(2026, 8, 20), ROUTINE) == []


def test_dose_ids_are_stable_across_rebuilds():
    first = doses_for_medicine(medicine(), date(2026, 8, 20), ROUTINE)
    second = doses_for_medicine(medicine(), date(2026, 8, 20), ROUTINE)
    assert [d.dose_id for d in first] == [d.dose_id for d in second]
    assert first[0].dose_id == "1:2026-08-20:0800"


def test_whole_day_is_sorted_by_clock_time():
    meds = [
        medicine(id=1, name="Metformin"),
        medicine(id=2, name="Atorvastatin", slots=["bedtime"], slot_qty={"bedtime": 1.0}),
    ]
    doses = build_day_schedule(meds, date(2026, 8, 20), ROUTINE)
    assert [d.scheduled_at for d in doses] == sorted(d.scheduled_at for d in doses)


def test_untouched_past_dose_becomes_missed():
    doses = build_day_schedule([medicine()], date(2026, 8, 20), ROUTINE)
    apply_log(doses, {}, datetime(2026, 8, 20, 12, 0))
    assert doses[0].status == "missed"      # 8am, long past
    assert doses[1].status == "pending"     # 8pm, still ahead


def test_taken_dose_is_never_reclassified_as_missed():
    doses = build_day_schedule([medicine()], date(2026, 8, 20), ROUTINE)
    log = {doses[0].dose_id: {"status": "taken", "acted_at": "2026-08-20T08:05"}}
    apply_log(doses, log, datetime(2026, 8, 20, 23, 0))
    assert doses[0].status == "taken"


def test_alarm_rings_within_the_grace_window_only():
    doses = build_day_schedule([medicine()], date(2026, 8, 20), ROUTINE)
    apply_log(doses, {}, datetime(2026, 8, 20, 8, 5))
    assert [d.medicine_name for d in due_now(doses, datetime(2026, 8, 20, 8, 5))] == ["Metformin"]
    assert due_now(doses, datetime(2026, 8, 20, 7, 30)) == []


def test_snooze_moves_the_alarm_forward():
    doses = build_day_schedule([medicine()], date(2026, 8, 20), ROUTINE)
    log = {doses[0].dose_id: {"status": "snoozed", "snooze_until": "2026-08-20T08:30"}}
    apply_log(doses, log, datetime(2026, 8, 20, 8, 10))
    assert due_now(doses, datetime(2026, 8, 20, 8, 10)) == []
    assert [d.medicine_name for d in due_now(doses, datetime(2026, 8, 20, 8, 30))] == ["Metformin"]


def test_next_dose_skips_doses_already_handled():
    doses = build_day_schedule([medicine()], date(2026, 8, 20), ROUTINE)
    now = datetime(2026, 8, 20, 7, 0)
    apply_log(doses, {doses[0].dose_id: {"status": "taken"}}, now)
    assert next_dose(doses, now).time_label == "8:00 PM"


def test_adherence_percentage_counts_only_settled_doses():
    med = medicine(duration_days=2, start_date="2026-08-19")
    doses_day1 = build_day_schedule([med], date(2026, 8, 19), ROUTINE)
    log = {
        doses_day1[0].dose_id: {"status": "taken"},
        doses_day1[1].dose_id: {"status": "skipped"},
    }
    report = adherence([med], log, date(2026, 8, 19), date(2026, 8, 20), ROUTINE,
                       datetime(2026, 8, 20, 23, 59))
    assert report["taken"] == 1
    assert report["skipped"] == 1
    assert report["missed"] == 2                     # both of day two went untouched
    assert report["adherence_percent"] == 25
    assert len(report["days"]) == 2


def test_adherence_is_none_when_nothing_is_settled_yet():
    report = adherence([medicine()], {}, date(2026, 8, 20), date(2026, 8, 20), ROUTINE,
                       datetime(2026, 8, 20, 6, 0))
    assert report["adherence_percent"] is None


def test_bad_routine_values_fall_back_to_defaults():
    doses = doses_for_medicine(medicine(), date(2026, 8, 20), {"breakfast": "not-a-time"})
    assert doses[0].time_label == "8:00 AM"
