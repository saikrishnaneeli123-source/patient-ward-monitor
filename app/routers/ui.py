"""Server-rendered pages for ward staff."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import intake, models, services
from app.db import get_db
from app.extraction import get_extractor
from app.routers.api import store_upload

router = APIRouter(tags=["ui"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.filters["real_allergies"] = services.real_allergies


def _context(request: Request, db: Session, **extra) -> dict:
    return {
        "request": request,
        "wards": services.wards(db),
        "extraction_enabled": getattr(get_extractor(), "available", False),
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
def board_page(request: Request, ward: str | None = None, db: Session = Depends(get_db)):
    rows = services.board(db, ward=ward)
    return templates.TemplateResponse(
        request,
        "board.html",
        _context(
            request,
            db,
            rows=rows,
            active_ward=ward,
            alerts=services.open_alerts(db, limit=10),
            unverified=sum(1 for r in rows if r.verification == models.VerificationStatus.unverified),
        ),
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, db: Session = Depends(get_db)):
    uploads = db.execute(select(models.Upload).order_by(models.Upload.id.desc()).limit(15)).scalars().all()
    return templates.TemplateResponse(request, "upload.html", _context(request, db, uploads=uploads, results=None))


@router.post("/upload", response_class=HTMLResponse)
async def upload_submit(
    request: Request,
    files: list[UploadFile] = File(...),
    uploaded_by: str = Form(default=""),
    db: Session = Depends(get_db),
):
    results = []
    for file in files:
        data = await file.read()
        if not data:
            continue
        upload = store_upload(db, file, data, uploaded_by or None)
        results.append(intake.process_upload(db, upload, data))
    recent = db.execute(select(models.Upload).order_by(models.Upload.id.desc()).limit(15)).scalars().all()
    return templates.TemplateResponse(
        request, "upload.html", _context(request, db, uploads=recent, results=results)
    )


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, db: Session = Depends(get_db)):
    """The queue of auto-created records still waiting on a clinician."""
    cases = services.case_query(db, status=None, verification=models.VerificationStatus.unverified)
    return templates.TemplateResponse(request, "review.html", _context(request, db, cases=cases))


@router.get("/cases/{case_id}", response_class=HTMLResponse)
def case_page(request: Request, case_id: int, db: Session = Depends(get_db)):
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")
    return templates.TemplateResponse(request, "case.html", _context(request, db, case=case))


@router.post("/cases/{case_id}/verify")
def verify_from_ui(case_id: int, verified_by: str = Form(...), db: Session = Depends(get_db)):
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")
    services.verify_case(db, case, verified_by)
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id}/observations")
def add_observation_from_ui(
    case_id: int,
    recorded_by: str = Form(default=""),
    respiratory_rate: str = Form(default=""),
    spo2: str = Form(default=""),
    on_oxygen: str = Form(default=""),
    spo2_scale: str = Form(default="1"),
    systolic_bp: str = Form(default=""),
    diastolic_bp: str = Form(default=""),
    pulse: str = Form(default=""),
    temperature_c: str = Form(default=""),
    consciousness: str = Form(default="alert"),
    note: str = Form(default=""),
    db: Session = Depends(get_db),
):
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")

    def as_int(value: str) -> int | None:
        value = value.strip()
        return int(value) if value else None

    def as_float(value: str) -> float | None:
        value = value.strip()
        return float(value) if value else None

    observation = models.Observation(
        case_record_id=case.id,
        recorded_by=recorded_by or None,
        respiratory_rate=as_int(respiratory_rate),
        spo2=as_int(spo2),
        on_oxygen=on_oxygen == "on",
        spo2_scale=int(spo2_scale or 1),
        systolic_bp=as_int(systolic_bp),
        diastolic_bp=as_int(diastolic_bp),
        pulse=as_int(pulse),
        temperature_c=as_float(temperature_c),
        consciousness=models.Consciousness(consciousness),
        note=note or None,
    )
    intake.score_observation(observation)
    db.add(observation)
    db.flush()
    intake.raise_alerts(db, observation)
    db.commit()
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_from_ui(
    alert_id: int,
    acknowledged_by: str = Form(default="ward staff"),
    next_url: str = Form(default="/"),
    db: Session = Depends(get_db),
):
    from datetime import datetime, timezone

    alert = db.get(models.Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found.")
    alert.acknowledged_at = datetime.now(timezone.utc)
    alert.acknowledged_by = acknowledged_by
    db.commit()
    return RedirectResponse(next_url, status_code=303)
