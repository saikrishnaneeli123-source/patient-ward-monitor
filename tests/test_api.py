"""End-to-end: a prescription goes in, alarms come out, doses get logged."""

import io
import os
from datetime import date, timedelta

import pytest

from backend import ocr

PRESCRIPTION = """Dr. R. Sharma, MBBS, MD
City Care Hospital
Patient Name: Kamala Devi   Age: 72
Rx
1. Tab. Metformin 500mg 1-0-1 x 30 days after food
2. Cap. Omeprazole 20mg OD before food x 14 days
3. Tab Atorvastatin 10mg HS
4. Tab Paracetamol 650mg SOS
"""


def confirm(client, patient, text=PRESCRIPTION, start=None):
    parsed = client.post("/api/prescriptions/parse-text", json={"text": text}).json()
    body = {
        "patient_id": patient["id"],
        "medicines": parsed["medicines"],
        "raw_text": text,
        "source": "text",
    }
    if start:
        body["start_date"] = start
    response = client.post("/api/prescriptions/confirm", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_reports_ocr_capability(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "ocr_available" in body
    assert body["default_routine"]["breakfast"] == "08:00"


def test_patient_crud(client):
    created = client.post("/api/patients", json={"name": "Ram Prasad", "age": 80}).json()
    assert created["routine"]["dinner"] == "20:00"      # defaults filled in

    updated = client.put(f"/api/patients/{created['id']}", json={
        "name": "Ram Prasad", "age": 80, "routine": {"dinner": "19:00"},
    }).json()
    assert updated["routine"]["dinner"] == "19:00"

    assert len(client.get("/api/patients").json()) == 1
    assert client.delete(f"/api/patients/{created['id']}").status_code == 200
    assert client.get("/api/patients").json() == []


def test_patient_name_is_required(client):
    assert client.post("/api/patients", json={"name": "   "}).status_code == 400


def test_parse_text_returns_a_draft_plan_without_saving_anything(client, patient):
    parsed = client.post("/api/prescriptions/parse-text", json={"text": PRESCRIPTION}).json()
    names = [m["name"] for m in parsed["medicines"]]
    assert names == ["Metformin", "Omeprazole", "Atorvastatin", "Paracetamol"]
    # Nothing is armed until a human confirms.
    assert client.get(f"/api/patients/{patient['id']}/medicines").json() == []


def test_confirming_a_prescription_arms_todays_alarms(client, patient):
    result = confirm(client, patient)
    assert len(result["medicine_ids"]) == 4
    # Metformin twice, Omeprazole once, Atorvastatin once; the SOS one never alarms.
    assert len(result["alarms_today"]) == 4
    assert "4 medicine(s) saved" in result["message"]


def test_schedule_orders_the_day_and_applies_food_timing(client, patient):
    confirm(client, patient, start=date.today().isoformat())
    schedule = client.get(f"/api/patients/{patient['id']}/schedule",
                          params={"date": date.today().isoformat(),
                                  "now": f"{date.today().isoformat()}T06:00"}).json()
    times = [(d["medicine_name"], d["time_label"]) for d in schedule["doses"]]
    assert times == [
        ("Omeprazole", "7:30 AM"),     # OD, before food -> 30 min before breakfast
        ("Metformin", "8:30 AM"),      # 1-0-1, after food
        ("Metformin", "8:30 PM"),
        ("Atorvastatin", "10:00 PM"),  # HS -> bedtime
    ]
    assert schedule["next_dose"]["medicine_name"] == "Omeprazole"
    assert schedule["counts"]["total"] == 4


def test_sos_medicine_is_saved_but_never_scheduled(client, patient):
    confirm(client, patient)
    medicines = client.get(f"/api/patients/{patient['id']}/medicines").json()
    paracetamol = next(m for m in medicines if m["name"] == "Paracetamol")
    assert paracetamol["prn"] is True

    schedule = client.get(f"/api/patients/{patient['id']}/schedule").json()
    assert all(d["medicine_name"] != "Paracetamol" for d in schedule["doses"])


def test_alarm_becomes_due_at_its_time(client, patient):
    confirm(client, patient, start=date.today().isoformat())
    today = date.today().isoformat()

    quiet = client.get(f"/api/patients/{patient['id']}/due",
                       params={"now": f"{today}T05:00"}).json()
    assert quiet["ringing"] == []
    assert quiet["next_dose"]["medicine_name"] == "Omeprazole"

    # A dose keeps ringing for its whole grace window, so the 7:30 Omeprazole is
    # still outstanding when the 8:30 Metformin comes due.
    ringing = client.get(f"/api/patients/{patient['id']}/due",
                         params={"now": f"{today}T08:30"}).json()
    assert [d["medicine_name"] for d in ringing["ringing"]] == ["Omeprazole", "Metformin"]


def test_an_alarm_stops_ringing_once_its_grace_window_passes(client, patient):
    confirm(client, patient, start=date.today().isoformat())
    today = date.today().isoformat()
    late = client.get(f"/api/patients/{patient['id']}/due",
                      params={"now": f"{today}T11:00"}).json()
    assert late["ringing"] == []
    schedule = client.get(f"/api/patients/{patient['id']}/schedule",
                          params={"now": f"{today}T11:00"}).json()
    assert schedule["counts"]["missed"] == 2


def test_marking_a_dose_taken_stops_it_ringing(client, patient):
    confirm(client, patient, start=date.today().isoformat())
    today = date.today().isoformat()
    due = client.get(f"/api/patients/{patient['id']}/due", params={"now": f"{today}T08:30"}).json()
    dose_ids = [dose["dose_id"] for dose in due["ringing"]]
    assert dose_ids

    for dose_id in dose_ids:
        result = client.post("/api/doses/action", json={
            "patient_id": patient["id"], "dose_id": dose_id, "action": "taken"}).json()
    assert result["counts"]["taken"] == len(dose_ids)

    again = client.get(f"/api/patients/{patient['id']}/due", params={"now": f"{today}T08:35"}).json()
    assert again["ringing"] == []


def test_snooze_pushes_the_alarm_and_undo_restores_it(client, patient):
    confirm(client, patient, start=date.today().isoformat())
    today = date.today().isoformat()
    due = client.get(f"/api/patients/{patient['id']}/due", params={"now": f"{today}T08:30"}).json()
    dose_id = next(d["dose_id"] for d in due["ringing"] if d["medicine_name"] == "Metformin")

    client.post("/api/doses/action", json={
        "patient_id": patient["id"], "dose_id": dose_id, "action": "snooze", "minutes": 15})
    snoozed = client.get(f"/api/patients/{patient['id']}/schedule").json()
    dose = next(d for d in snoozed["doses"] if d["dose_id"] == dose_id)
    assert dose["status"] == "snoozed"
    assert dose["snooze_until"] is not None

    client.post("/api/doses/action", json={
        "patient_id": patient["id"], "dose_id": dose_id, "action": "undo"})
    restored = client.get(f"/api/patients/{patient['id']}/schedule").json()
    dose = next(d for d in restored["doses"] if d["dose_id"] == dose_id)
    assert dose["status"] in ("pending", "missed")


def test_dose_action_rejects_a_bad_id_and_a_bad_action(client, patient):
    confirm(client, patient)
    assert client.post("/api/doses/action", json={
        "patient_id": patient["id"], "dose_id": "nonsense", "action": "taken"}).status_code == 400
    schedule = client.get(f"/api/patients/{patient['id']}/schedule").json()
    dose_id = schedule["doses"][0]["dose_id"]
    assert client.post("/api/doses/action", json={
        "patient_id": patient["id"], "dose_id": dose_id, "action": "eaten"}).status_code == 400


def test_a_dose_cannot_be_logged_against_another_patient(client, patient):
    confirm(client, patient)
    other = client.post("/api/patients", json={"name": "Someone Else"}).json()
    dose_id = client.get(f"/api/patients/{patient['id']}/schedule").json()["doses"][0]["dose_id"]
    response = client.post("/api/doses/action", json={
        "patient_id": other["id"], "dose_id": dose_id, "action": "taken"})
    assert response.status_code == 404


def test_changing_meal_times_moves_every_future_alarm(client, patient):
    confirm(client, patient, start=date.today().isoformat())
    client.put(f"/api/patients/{patient['id']}", json={
        "name": patient["name"], "routine": {"breakfast": "10:00"}})
    schedule = client.get(f"/api/patients/{patient['id']}/schedule").json()
    metformin = next(d for d in schedule["doses"] if d["medicine_name"] == "Metformin")
    assert metformin["time_label"] == "10:30 AM"


def test_stopping_a_medicine_removes_its_alarms(client, patient):
    confirm(client, patient)
    medicines = client.get(f"/api/patients/{patient['id']}/medicines").json()
    metformin = next(m for m in medicines if m["name"] == "Metformin")
    client.delete(f"/api/medicines/{metformin['id']}")

    schedule = client.get(f"/api/patients/{patient['id']}/schedule").json()
    assert all(d["medicine_name"] != "Metformin" for d in schedule["doses"])
    assert len(client.get(f"/api/patients/{patient['id']}/medicines").json()) == 3
    assert len(client.get(f"/api/patients/{patient['id']}/medicines",
                          params={"include_stopped": True}).json()) == 4


def test_editing_a_medicine_changes_its_alarms(client, patient):
    confirm(client, patient)
    medicines = client.get(f"/api/patients/{patient['id']}/medicines").json()
    atorva = next(m for m in medicines if m["name"] == "Atorvastatin")
    atorva.update({"slots": ["morning"], "slot_qty": {"morning": 2.0}, "food": "after_food"})
    client.put(f"/api/medicines/{atorva['id']}", json=atorva)

    schedule = client.get(f"/api/patients/{patient['id']}/schedule").json()
    dose = next(d for d in schedule["doses"] if d["medicine_name"] == "Atorvastatin")
    assert dose["time_label"] == "8:30 AM"
    assert dose["quantity"] == 2.0


def test_manual_medicine_can_be_added_without_a_prescription(client, patient):
    response = client.post(f"/api/patients/{patient['id']}/medicines", json={
        "name": "Vitamin D3", "strength": "60000 IU", "slots": ["morning"],
        "slot_qty": {"morning": 1}, "day_interval": 7})
    assert response.status_code == 201
    assert response.json()["name"] == "Vitamin D3"


def test_adherence_report(client, patient):
    confirm(client, patient, start=date.today().isoformat())
    today = date.today().isoformat()
    schedule = client.get(f"/api/patients/{patient['id']}/schedule",
                          params={"now": f"{today}T23:59"}).json()
    for dose in schedule["doses"][:2]:
        client.post("/api/doses/action", json={
            "patient_id": patient["id"], "dose_id": dose["dose_id"], "action": "taken"})

    report = client.get(f"/api/patients/{patient['id']}/adherence",
                        params={"days": 1, "now": f"{today}T23:59"}).json()
    assert report["taken"] == 2
    assert report["missed"] == 2
    assert report["adherence_percent"] == 50
    assert report["caregiver"]["phone"] == "9990001111"


def test_refill_alert_fires_near_the_end_of_a_course(client, patient):
    text = "Tab Azithromycin 500mg OD x 3 days"
    start = (date.today() - timedelta(days=1)).isoformat()
    confirm(client, patient, text=text, start=start)
    alerts = client.get(f"/api/patients/{patient['id']}/refills").json()
    assert alerts[0]["name"] == "Azithromycin"
    assert alerts[0]["days_left"] == 2


def test_ongoing_medicine_never_raises_a_refill_alert(client, patient):
    confirm(client, patient, text="Tab Amlodipine 5mg 0-0-1 continue")
    assert client.get(f"/api/patients/{patient['id']}/refills").json() == []


def test_confirm_requires_at_least_one_medicine(client, patient):
    response = client.post("/api/prescriptions/confirm",
                           json={"patient_id": patient["id"], "medicines": []})
    assert response.status_code == 400


def test_unknown_patient_returns_a_friendly_error(client):
    response = client.get("/api/patients/999/schedule")
    assert response.status_code == 404
    assert response.json()["error"] == "Patient not found"


def test_uploading_a_non_image_is_rejected(client):
    response = client.post("/api/prescriptions/parse",
                           files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
    assert response.status_code == 400


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]


def _load_font(size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return None


def _render_prescription_image() -> bytes:
    """A printed prescription, rendered the way a phone camera would see one."""
    from PIL import Image, ImageDraw
    lines = [
        "Rx",
        "1. Tab. Metformin 500mg 1-0-1 x 30 days",
        "2. Cap. Omeprazole 20mg OD before food",
        "3. Tab Atorvastatin 10mg HS",
    ]
    font = _load_font(34)
    image = Image.new("L", (1100, 340), color=255)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((40, 30 + index * 70), line, fill=20, font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.skipif(not ocr.ocr_available(), reason="tesseract is not installed")
def test_photo_of_a_prescription_becomes_alarms(client, patient):
    """The headline flow: upload an image, get a schedule."""
    response = client.post("/api/prescriptions/parse", files={
        "file": ("rx.png", io.BytesIO(_render_prescription_image()), "image/png")})
    assert response.status_code == 200
    parsed = response.json()
    assert parsed["raw_text"].strip(), "OCR returned no text"

    names = " ".join(m["name"].lower() for m in parsed["medicines"])
    assert "metformin" in names, f"OCR read: {parsed['raw_text']!r}"

    confirmed = client.post("/api/prescriptions/confirm", json={
        "patient_id": patient["id"], "medicines": parsed["medicines"],
        "raw_text": parsed["raw_text"], "source": "image",
        "image_path": parsed["image_path"], "start_date": date.today().isoformat()}).json()
    assert confirmed["alarms_today"], "confirming a photographed prescription set no alarms"

    stored = client.get(f"/api/prescriptions/image/{parsed['image_path']}")
    assert stored.status_code == 200
