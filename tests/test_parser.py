"""The parser is the safety-critical part: a misread dose becomes a wrong alarm."""

import pytest

from backend.parser import (
    parse_line, parse_prescription, MORNING, AFTERNOON, NIGHT, BEDTIME,
    FOOD_AFTER, FOOD_BEFORE, FOOD_EMPTY,
)


def parse_one(line):
    med = parse_line(line)
    assert med is not None, f"expected a medicine from: {line!r}"
    return med


@pytest.mark.parametrize("line,name,strength", [
    ("Tab. Metformin 500mg 1-0-1", "Metformin", "500 mg"),
    ("Cap Omeprazole 20 mg OD", "Omeprazole", "20 mg"),
    ("T. Amlodipine 5mg 0-0-1", "Amlodipine", "5 mg"),
    ("2) Tab Atorvastatin 10mg HS", "Atorvastatin", "10 mg"),
    ("Tab Vitamin D3 60000 IU once a week", "Vitamin D3", "60000 IU"),
])
def test_name_and_strength(line, name, strength):
    med = parse_one(line)
    assert med.name == name
    assert med.strength == strength


def test_dose_pattern_becomes_slots_and_quantities():
    med = parse_one("Tab. Metformin 500mg 1-0-1 x 30 days after food")
    assert med.slots == [MORNING, NIGHT]
    assert med.slot_qty == {MORNING: 1.0, NIGHT: 1.0}
    assert med.duration_days == 30
    assert med.food == FOOD_AFTER


def test_four_part_pattern_includes_evening():
    med = parse_one("Tab Paracetamol 500mg 1-1-1-1 x 3 days")
    assert len(med.slots) == 4
    assert sum(med.slot_qty.values()) == 4


def test_half_tablet_pattern():
    med = parse_one("Tab Ecosprin 75mg 1/2-0-1/2")
    assert med.slot_qty == {MORNING: 0.5, NIGHT: 0.5}


@pytest.mark.parametrize("line,slots", [
    ("Tab Azee 500 OD", [MORNING]),
    ("Tab Pan 40 BD", [MORNING, NIGHT]),
    ("Syp Ascoril 5ml TDS", [MORNING, AFTERNOON, NIGHT]),
    ("Tab Crocin 650 QID", [MORNING, AFTERNOON, "evening", NIGHT]),
    ("Tab Melatonin 3mg HS", [BEDTIME]),
])
def test_frequency_abbreviations(line, slots):
    assert parse_one(line).slots == slots


def test_hourly_interval():
    med = parse_one("Tab Azithromycin 500mg q8h x 3 days")
    assert med.interval_hours == 8
    assert med.duration_days == 3
    assert med.slots == []


def test_every_six_hours_written_out():
    assert parse_one("Tab Paracetamol 650mg every 6 hours").interval_hours == 6


def test_sos_medicine_is_not_alarmed():
    med = parse_one("Tab Paracetamol 650mg SOS")
    assert med.prn is True
    assert med.slots == []
    # A correctly-read SOS medicine is a note, not something to send back for review.
    assert any("when needed" in note for note in med.notes)
    assert med.warnings == []


def test_alternate_day_and_weekly():
    assert parse_one("Tab Telmisartan 40mg 1-0-0 alternate day").day_interval == 2
    assert parse_one("Tab Methotrexate 7.5mg once weekly").day_interval == 7


@pytest.mark.parametrize("line,food", [
    ("Tab Pan 40mg OD before food", FOOD_BEFORE),
    ("Tab Metformin 500mg BD after food", FOOD_AFTER),
    ("Tab Thyronorm 50mcg OD empty stomach", FOOD_EMPTY),
])
def test_food_timing(line, food):
    assert parse_one(line).food == food


@pytest.mark.parametrize("line,days", [
    ("Tab Azee 500 OD x 5 days", 5),
    ("Tab Azee 500 OD for 10 days", 10),
    ("Tab Azee 500 OD 5/7", 5),
    ("Cap Pregabalin 75mg HS x 2 weeks", 14),
    ("Tab Shelcal OD x 3 months", 90),
])
def test_duration(line, days):
    assert parse_one(line).duration_days == days


def test_continue_means_ongoing():
    med = parse_one("T. Amlodipine 5mg 0-0-1 continue")
    assert med.duration_days is None
    assert med.slots == [NIGHT]


def test_missing_frequency_is_flagged_not_guessed_silently():
    med = parse_one("Tab Shelcal 500")
    assert med.warnings, "a medicine with no frequency must be flagged for review"
    assert med.confidence < 0.8


def test_ocr_digit_confusion_in_dose_pattern():
    med = parse_one("Tab. Metformin 500mg 1-O-l")
    assert med.slot_qty == {MORNING: 1.0, NIGHT: 1.0}


def test_letterhead_and_admin_lines_are_ignored():
    text = """
    Dr. R. Sharma, MBBS, MD
    City Care Hospital, MG Road
    Patient Name: Kamala Devi   Age: 72
    Date: 12-05-2025
    Rx
    Tab. Metformin 500mg 1-0-1 x 30 days
    Follow up after 2 weeks
    """
    names = [m["name"] for m in parse_prescription(text)["medicines"]]
    assert names == ["Metformin"]


def test_a_date_is_never_read_as_a_dose_pattern():
    assert parse_line("Date: 12-05-2025") is None


def test_full_prescription():
    text = """Rx
    1. Tab. Metformin 500mg  1-0-1  x 30 days (after food)
    2. Cap. Omeprazole 20mg OD before food x 14 days
    3. Tab Paracetamol 650mg SOS
    4. Syp. Ascoril 5ml TDS x 5 days
    5. Tab Atorvastatin 10mg HS
    """
    result = parse_prescription(text)
    names = [m["name"] for m in result["medicines"]]
    assert names == ["Metformin", "Omeprazole", "Paracetamol", "Ascoril", "Atorvastatin"]
    assert all(m["confidence"] >= 0.6 for m in result["medicines"])


def test_liquid_dose_is_read_in_millilitres():
    med = parse_one("Syp. Ascoril 5ml TDS x 5 days")
    assert med.dose_qty == 5.0
    assert med.dose_unit == "ml"
    assert med.slot_qty[MORNING] == 5.0


def test_empty_prescription_warns_instead_of_inventing_medicines():
    result = parse_prescription("")
    assert result["medicines"] == []
    assert result["warnings"]


def test_duplicate_lines_are_merged():
    text = "Tab Metformin 500mg 1-0-1 x 30 days\nTab Metformin 500mg"
    assert len(parse_prescription(text)["medicines"]) == 1


@pytest.mark.parametrize("line", [
    "Patient: Kamala Devi    Age: 72",
    "Name: Ram Prasad   Sex: M",
    "Date: 12/05/2025",
    "Age 68 yrs",
])
def test_demographic_lines_never_become_medicines(line):
    """OCR flattens a printed form into one row; none of it is a drug."""
    assert parse_line(line) is None
