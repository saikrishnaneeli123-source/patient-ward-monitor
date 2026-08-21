"""HTTP API for the medicine reminder.

The flow the app is built around:

    photo of prescription -> OCR -> parser -> *human confirms* -> medicines
                                                                     |
                                              scheduler -> alarms -> taken / snoozed / skipped

The confirmation step is deliberate. OCR on a phone photo of a prescription is
never certain enough to start alarming an 80-year-old about a dose, so the parser
scores every line and the UI makes the caregiver approve the plan before a single
alarm is armed.
"""

from __future__ import annotations

import uuid
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, ocr, scheduler
from .parser import parse_prescription

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="MediRemind - prescription-driven medicine alarms",
              version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class PatientIn(BaseModel):
    name: str
    age: int | None = None
    phone: str = ""
    caregiver_name: str = ""
    caregiver_phone: str = ""
    routine: dict[str, str] = Field(default_factory=dict)
    language: str = "en-IN"


class MedicineIn(BaseModel):
    name: str
    form: str = "tablet"
    strength: str = ""
    dose_qty: float = 1.0
    dose_unit: str = "tablet"
    slots: list[str] = Field(default_factory=list)
    slot_qty: dict[str, float] = Field(default_factory=dict)
    interval_hours: int | None = None
    day_interval: int = 1
    frequency_label: str = ""
    food: str = "any"
    duration_days: int | None = None
    start_date: str | None = None
    prn: bool = False
    instructions: str = ""
    confidence: float = 1.0
    raw_line: str = ""
    active: bool = True


class ConfirmIn(BaseModel):
    patient_id: int
    medicines: list[MedicineIn]
    raw_text: str = ""
    source: str = "text"
    doctor: str = ""
    image_path: str = ""
    start_date: str | None = None


class TextIn(BaseModel):
    text: str


class DoseActionIn(BaseModel):
    patient_id: int
    dose_id: str
    action: str                      # taken | skip | snooze | undo
    minutes: int = 10
    note: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now(value: str | None = None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now().replace(second=0, microsecond=0)


def _day(value: str | None = None) -> Date:
    if value:
        try:
            return Date.fromisoformat(value)
        except ValueError:
            pass
    return Date.today()


def _get_patient(patient_id: int) -> dict[str, Any]:
    patient = db.query_one("SELECT * FROM patients WHERE id = ?", (patient_id,))
    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient


def _active_medicines(patient_id: int) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM medicines WHERE patient_id = ? AND active = 1 ORDER BY name",
        (patient_id,),
    )


def _log_for(patient_id: int, start: Date, end: Date) -> dict[str, dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM dose_log WHERE patient_id = ? AND scheduled_at >= ? AND scheduled_at <= ?",
        (patient_id, start.isoformat(), f"{end.isoformat()}T23:59"),
    )
    return {row["dose_id"]: row for row in rows}


def _schedule(patient: dict[str, Any], day: Date, now: datetime) -> list[scheduler.Dose]:
    medicines = _active_medicines(int(patient["id"]))
    doses = scheduler.build_day_schedule(medicines, day, patient.get("routine"))
    return scheduler.apply_log(doses, _log_for(int(patient["id"]), day, day), now)


# ---------------------------------------------------------------------------
# Health & patients
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "ocr_available": ocr.ocr_available(),
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "default_routine": scheduler.DEFAULT_ROUTINE,
    }


@app.get("/api/patients")
def list_patients() -> list[dict[str, Any]]:
    return db.query("SELECT * FROM patients ORDER BY name")


@app.post("/api/patients", status_code=201)
def create_patient(body: PatientIn) -> dict[str, Any]:
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Please enter the patient's name")
    patient_id = db.execute(
        """INSERT INTO patients (name, age, phone, caregiver_name, caregiver_phone,
                                 routine, language, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (body.name.strip(), body.age, body.phone, body.caregiver_name, body.caregiver_phone,
         db.dumps(scheduler.routine_with_defaults(body.routine)), body.language,
         datetime.now().isoformat(timespec="seconds")),
    )
    return _get_patient(patient_id)


@app.get("/api/patients/{patient_id}")
def get_patient(patient_id: int) -> dict[str, Any]:
    return _get_patient(patient_id)


@app.put("/api/patients/{patient_id}")
def update_patient(patient_id: int, body: PatientIn) -> dict[str, Any]:
    _get_patient(patient_id)
    db.execute(
        """UPDATE patients SET name=?, age=?, phone=?, caregiver_name=?, caregiver_phone=?,
                               routine=?, language=? WHERE id=?""",
        (body.name.strip(), body.age, body.phone, body.caregiver_name, body.caregiver_phone,
         db.dumps(scheduler.routine_with_defaults(body.routine)), body.language, patient_id),
    )
    return _get_patient(patient_id)


@app.delete("/api/patients/{patient_id}")
def delete_patient(patient_id: int) -> dict[str, str]:
    _get_patient(patient_id)
    db.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Prescription intake
# ---------------------------------------------------------------------------

@app.post("/api/prescriptions/parse")
async def parse_upload(file: UploadFile | None = File(default=None),
                       text: str = Form(default="")) -> dict[str, Any]:
    """Read a prescription (photo or typed) and return a *draft* plan to confirm."""
    raw_text = text or ""
    image_path = ""
    source = "text"
    ocr_error = ""

    if file is not None:
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="That photo is too large. Please use a smaller image.")
        if not data:
            raise HTTPException(status_code=400, detail="The uploaded file was empty.")
        source = "image"
        suffix = Path(file.filename or "upload.jpg").suffix.lower() or ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
            raise HTTPException(status_code=400, detail="Please upload a photo (JPG, PNG or WEBP).")
        stored = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(data)
        image_path = stored.name

        result = ocr.image_to_text(data)
        if result["ok"]:
            raw_text = result["text"]
        else:
            ocr_error = result["error"]

    parsed = parse_prescription(raw_text)
    if ocr_error:
        parsed["warnings"].insert(0, ocr_error)

    parsed["source"] = source
    parsed["image_path"] = image_path
    parsed["raw_text"] = raw_text
    parsed["ocr_available"] = ocr.ocr_available()
    return parsed


@app.post("/api/prescriptions/parse-text")
def parse_text(body: TextIn) -> dict[str, Any]:
    parsed = parse_prescription(body.text)
    parsed.update({"source": "text", "image_path": "", "raw_text": body.text})
    return parsed


@app.post("/api/prescriptions/confirm", status_code=201)
def confirm_prescription(body: ConfirmIn) -> dict[str, Any]:
    """Save a confirmed plan. This is the moment alarms start being armed."""
    patient = _get_patient(body.patient_id)
    if not body.medicines:
        raise HTTPException(status_code=400, detail="Add at least one medicine before saving.")

    start_date = body.start_date or Date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    prescription_id = db.execute(
        """INSERT INTO prescriptions (patient_id, source, doctor, raw_text, image_path, created_at)
           VALUES (?,?,?,?,?,?)""",
        (body.patient_id, body.source, body.doctor, body.raw_text, body.image_path, now),
    )

    saved: list[int] = []
    for medicine in body.medicines:
        saved.append(_insert_medicine(body.patient_id, prescription_id, medicine, start_date, now))

    medicines = db.query(
        f"SELECT * FROM medicines WHERE id IN ({','.join('?' * len(saved))})", saved
    ) if saved else []

    today = Date.today()
    doses = scheduler.build_day_schedule(medicines, today, patient.get("routine"))
    return {
        "prescription_id": prescription_id,
        "medicine_ids": saved,
        "medicines": medicines,
        "alarms_today": [dose.to_dict() for dose in doses],
        "message": f"{len(saved)} medicine(s) saved. {len(doses)} alarm(s) set for today.",
    }


def _insert_medicine(patient_id: int, prescription_id: int | None, medicine: MedicineIn,
                     start_date: str, now: str) -> int:
    slot_qty = {slot: float(qty) for slot, qty in (medicine.slot_qty or {}).items() if float(qty) > 0}
    slots = [slot for slot in (medicine.slots or []) if slot in slot_qty] or list(medicine.slots or [])
    if slots and not slot_qty:
        slot_qty = {slot: float(medicine.dose_qty or 1) for slot in slots}
    return db.execute(
        """INSERT INTO medicines (patient_id, prescription_id, name, form, strength, dose_qty,
               dose_unit, slots, slot_qty, interval_hours, day_interval, frequency_label, food,
               duration_days, start_date, prn, instructions, confidence, raw_line, active, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (patient_id, prescription_id, medicine.name.strip(), medicine.form, medicine.strength,
         medicine.dose_qty, medicine.dose_unit, db.dumps(slots), db.dumps(slot_qty),
         medicine.interval_hours, max(1, medicine.day_interval), medicine.frequency_label,
         medicine.food, medicine.duration_days, medicine.start_date or start_date,
         int(medicine.prn), medicine.instructions, medicine.confidence, medicine.raw_line,
         int(medicine.active), now),
    )


@app.get("/api/prescriptions/{patient_id}")
def list_prescriptions(patient_id: int) -> list[dict[str, Any]]:
    _get_patient(patient_id)
    return db.query(
        "SELECT * FROM prescriptions WHERE patient_id = ? ORDER BY created_at DESC", (patient_id,)
    )


@app.get("/api/prescriptions/image/{filename}")
def prescription_image(filename: str) -> FileResponse:
    path = (UPLOAD_DIR / Path(filename).name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Medicines
# ---------------------------------------------------------------------------

@app.get("/api/patients/{patient_id}/medicines")
def patient_medicines(patient_id: int, include_stopped: bool = False) -> list[dict[str, Any]]:
    _get_patient(patient_id)
    sql = "SELECT * FROM medicines WHERE patient_id = ?"
    if not include_stopped:
        sql += " AND active = 1"
    medicines = db.query(sql + " ORDER BY active DESC, name", (patient_id,))
    for medicine in medicines:
        end = scheduler.course_end_date(medicine)
        medicine["course_end_date"] = end.isoformat() if end else None
        medicine["days_left"] = (end - Date.today()).days + 1 if end else None
    return medicines


@app.post("/api/patients/{patient_id}/medicines", status_code=201)
def add_medicine(patient_id: int, body: MedicineIn) -> dict[str, Any]:
    _get_patient(patient_id)
    now = datetime.now().isoformat(timespec="seconds")
    medicine_id = _insert_medicine(patient_id, None, body, Date.today().isoformat(), now)
    return db.query_one("SELECT * FROM medicines WHERE id = ?", (medicine_id,)) or {}


@app.put("/api/medicines/{medicine_id}")
def update_medicine(medicine_id: int, body: MedicineIn) -> dict[str, Any]:
    existing = db.query_one("SELECT * FROM medicines WHERE id = ?", (medicine_id,))
    if not existing:
        raise HTTPException(status_code=404, detail="Medicine not found")
    slot_qty = {slot: float(qty) for slot, qty in (body.slot_qty or {}).items() if float(qty) > 0}
    slots = list(body.slots or [])
    if slots and not slot_qty:
        slot_qty = {slot: float(body.dose_qty or 1) for slot in slots}
    db.execute(
        """UPDATE medicines SET name=?, form=?, strength=?, dose_qty=?, dose_unit=?, slots=?,
               slot_qty=?, interval_hours=?, day_interval=?, frequency_label=?, food=?,
               duration_days=?, start_date=?, prn=?, instructions=?, active=? WHERE id=?""",
        (body.name.strip(), body.form, body.strength, body.dose_qty, body.dose_unit,
         db.dumps(slots), db.dumps(slot_qty), body.interval_hours, max(1, body.day_interval),
         body.frequency_label, body.food, body.duration_days,
         body.start_date or existing["start_date"], int(body.prn), body.instructions,
         int(body.active), medicine_id),
    )
    return db.query_one("SELECT * FROM medicines WHERE id = ?", (medicine_id,)) or {}


@app.delete("/api/medicines/{medicine_id}")
def stop_medicine(medicine_id: int) -> dict[str, str]:
    if not db.query_one("SELECT id FROM medicines WHERE id = ?", (medicine_id,)):
        raise HTTPException(status_code=404, detail="Medicine not found")
    db.execute("UPDATE medicines SET active = 0 WHERE id = ?", (medicine_id,))
    return {"status": "stopped"}


# ---------------------------------------------------------------------------
# Schedule, alarms, and the dose log
# ---------------------------------------------------------------------------

@app.get("/api/patients/{patient_id}/schedule")
def day_schedule(patient_id: int, date: str | None = None, now: str | None = None) -> dict[str, Any]:
    patient = _get_patient(patient_id)
    day, moment = _day(date), _now(now)
    doses = _schedule(patient, day, moment)
    upcoming = scheduler.next_dose(doses, moment)
    return {
        "date": day.isoformat(),
        "patient": {"id": patient["id"], "name": patient["name"]},
        "doses": [dose.to_dict() for dose in doses],
        "next_dose": upcoming.to_dict() if upcoming else None,
        "due_now": [dose.to_dict() for dose in scheduler.due_now(doses, moment)],
        "counts": _counts(doses),
    }


def _counts(doses: list[scheduler.Dose]) -> dict[str, int]:
    counts = {"total": len(doses), "taken": 0, "missed": 0, "skipped": 0, "pending": 0}
    for dose in doses:
        key = "pending" if dose.status in (scheduler.STATUS_PENDING, scheduler.STATUS_SNOOZED) else dose.status
        counts[key] = counts.get(key, 0) + 1
    return counts


@app.get("/api/patients/{patient_id}/due")
def alarms_due(patient_id: int, now: str | None = None) -> dict[str, Any]:
    """Polled by the phone every minute: 'is anything ringing right now?'"""
    patient = _get_patient(patient_id)
    moment = _now(now)
    doses = _schedule(patient, moment.date(), moment)
    ringing = scheduler.due_now(doses, moment)
    upcoming = scheduler.next_dose(doses, moment)
    return {
        "now": moment.isoformat(timespec="minutes"),
        "ringing": [dose.to_dict() for dose in ringing],
        "next_dose": upcoming.to_dict() if upcoming else None,
    }


@app.post("/api/doses/action")
def dose_action(body: DoseActionIn) -> dict[str, Any]:
    patient = _get_patient(body.patient_id)
    try:
        medicine_id, day_text, hhmm = body.dose_id.split(":")
        scheduled_at = f"{day_text}T{hhmm[:2]}:{hhmm[2:]}"
        Date.fromisoformat(day_text)
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="That dose id is not valid.")

    medicine = db.query_one("SELECT * FROM medicines WHERE id = ? AND patient_id = ?",
                            (medicine_id, body.patient_id))
    if not medicine:
        raise HTTPException(status_code=404, detail="Medicine not found for this patient")

    now = datetime.now()
    action = body.action.lower()

    if action == "undo":
        db.execute("DELETE FROM dose_log WHERE dose_id = ?", (body.dose_id,))
    else:
        status_map = {"taken": scheduler.STATUS_TAKEN,
                      "skip": scheduler.STATUS_SKIPPED,
                      "skipped": scheduler.STATUS_SKIPPED,
                      "snooze": scheduler.STATUS_SNOOZED}
        if action not in status_map:
            raise HTTPException(status_code=400, detail="Action must be taken, skip, snooze or undo.")
        snooze_until = None
        if status_map[action] == scheduler.STATUS_SNOOZED:
            minutes = min(max(int(body.minutes or 10), 1), 240)
            snooze_until = (now + timedelta(minutes=minutes)).isoformat(timespec="minutes")
        db.execute(
            """INSERT INTO dose_log (dose_id, medicine_id, patient_id, scheduled_at, status,
                                     acted_at, snooze_until, note)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(dose_id) DO UPDATE SET
                   status=excluded.status, acted_at=excluded.acted_at,
                   snooze_until=excluded.snooze_until, note=excluded.note""",
            (body.dose_id, int(medicine_id), body.patient_id, scheduled_at, status_map[action],
             now.isoformat(timespec="minutes"), snooze_until, body.note),
        )

    day = Date.fromisoformat(day_text)
    doses = _schedule(patient, day, now)
    return {
        "status": "ok",
        "dose_id": body.dose_id,
        "medicine": medicine["name"],
        "doses": [dose.to_dict() for dose in doses],
        "counts": _counts(doses),
    }


@app.get("/api/patients/{patient_id}/adherence")
def patient_adherence(patient_id: int, days: int = 7, now: str | None = None) -> dict[str, Any]:
    patient = _get_patient(patient_id)
    moment = _now(now)
    days = min(max(days, 1), 90)
    end = moment.date()
    start = end - timedelta(days=days - 1)
    medicines = _active_medicines(patient_id)
    log = _log_for(patient_id, start, end)
    report = scheduler.adherence(medicines, log, start, end, patient.get("routine"), moment)
    report["patient"] = {"id": patient["id"], "name": patient["name"]}
    report["caregiver"] = {"name": patient.get("caregiver_name", ""),
                           "phone": patient.get("caregiver_phone", "")}
    return report


@app.get("/api/patients/{patient_id}/refills")
def refill_alerts(patient_id: int, within_days: int = 5) -> list[dict[str, Any]]:
    """Courses about to run out, so someone can buy a refill in time."""
    _get_patient(patient_id)
    alerts = []
    today = Date.today()
    for medicine in _active_medicines(patient_id):
        end = scheduler.course_end_date(medicine)
        if not end:
            continue
        days_left = (end - today).days + 1
        if days_left <= within_days:
            alerts.append({
                "medicine_id": medicine["id"],
                "name": medicine["name"],
                "strength": medicine["strength"],
                "days_left": max(days_left, 0),
                "course_end_date": end.isoformat(),
                "finished": days_left <= 0,
            })
    alerts.sort(key=lambda alert: alert["days_left"])
    return alerts


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


@app.exception_handler(HTTPException)
def friendly_errors(request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content={"error": exc.detail, "status": exc.status_code})
