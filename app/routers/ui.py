"""Server-rendered pages for ward staff."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth, charts, intake, models, services
from app.db import get_db
from app.extraction import get_extractor
from app.routers.api import store_upload

router = APIRouter(tags=["ui"], include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.filters["real_allergies"] = services.real_allergies
templates.env.globals["can"] = auth.can
templates.env.globals["PERMS"] = {
    name: getattr(auth, name)
    for name in ("VIEW", "UPLOAD", "RECORD_OBSERVATIONS", "VERIFY_CASE",
                 "EDIT_CASE", "DISCHARGE", "ACKNOWLEDGE_ALERT", "MANAGE_USERS")
}


def _context(request: Request, db: Session, user: models.User | None = None, **extra) -> dict:
    return {
        "request": request,
        "user": user,
        "wards": services.wards(db),
        "extraction_enabled": getattr(get_extractor(), "available", False),
        **extra,
    }


def _safe_next(target: str | None) -> str:
    """Only ever redirect within this app — never to an attacker-supplied host."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str | None = None, db: Session = Depends(get_db)):
    if auth.current_user(request, db) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", _context(request, db, next=_safe_next(next), error=error)
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    db: Session = Depends(get_db),
):
    user = auth.authenticate(db, username, password)
    if user is None:
        # One message for every failure mode, so it cannot be used to enumerate
        # usernames or probe which accounts are locked.
        return RedirectResponse(
            f"/login?next={_safe_next(next)}&error=Invalid+username+or+password.", status_code=303
        )
    request.session.clear()  # new session id on login
    request.session["user_id"] = user.id
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/denied", response_class=HTMLResponse)
def denied_page(
    request: Request,
    reason: str = "perform this action",
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require_login),
):
    return templates.TemplateResponse(
        request, "denied.html", _context(request, db, user, reason=reason), status_code=403
    )


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    created: str | None = None,
    token: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.MANAGE_USERS)),
):
    staff = db.execute(select(models.User).order_by(models.User.username)).scalars().all()
    return templates.TemplateResponse(
        request,
        "users.html",
        _context(request, db, user, staff=staff, created=created, token=token, error=error,
                 roles=list(models.Role), role_permissions=auth.ROLE_PERMISSIONS),
    )


@router.post("/users")
def create_user_from_ui(
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    issue_api_token: str = Form(default=""),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.MANAGE_USERS)),
):
    try:
        created, token = auth.create_user(
            db, username=username, full_name=full_name, password=password,
            role=models.Role(role), with_token=issue_api_token == "on",
        )
    except ValueError as exc:
        return RedirectResponse(f"/users?error={exc}", status_code=303)
    suffix = f"&token={token}" if token else ""
    return RedirectResponse(f"/users?created={created.username}{suffix}", status_code=303)


@router.post("/users/{user_id}/deactivate")
def deactivate_from_ui(
    user_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.MANAGE_USERS)),
):
    target = db.get(models.User, user_id)
    if target is None:
        raise HTTPException(404, "User not found.")
    if target.id == user.id:
        return RedirectResponse("/users?error=You+cannot+deactivate+your+own+account.", status_code=303)
    target.is_active = False
    db.commit()
    return RedirectResponse("/users", status_code=303)


@router.get("/", response_class=HTMLResponse)
def board_page(
    request: Request,
    ward: str | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
):
    rows = services.board(db, ward=ward)
    return templates.TemplateResponse(
        request,
        "board.html",
        _context(
            request,
            db,
            user,
            rows=rows,
            active_ward=ward,
            alerts=services.open_alerts(db, limit=10),
            unverified=sum(1 for r in rows if r.verification == models.VerificationStatus.unverified),
        ),
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_page(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.UPLOAD)),
):
    uploads = db.execute(select(models.Upload).order_by(models.Upload.id.desc()).limit(15)).scalars().all()
    return templates.TemplateResponse(
        request, "upload.html", _context(request, db, user, uploads=uploads, results=None)
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload_submit(
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.UPLOAD)),
):
    results = []
    for file in files:
        data = await file.read()
        if not data:
            continue
        upload = store_upload(db, file, data, user)
        results.append(intake.process_upload(db, upload, data))
    recent = db.execute(select(models.Upload).order_by(models.Upload.id.desc()).limit(15)).scalars().all()
    return templates.TemplateResponse(
        request, "upload.html", _context(request, db, user, uploads=recent, results=results)
    )


@router.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
):
    """The queue of auto-created records still waiting on a clinician."""
    cases = services.case_query(db, status=None, verification=models.VerificationStatus.unverified)
    return templates.TemplateResponse(request, "review.html", _context(request, db, user, cases=cases))


@router.get("/cases/{case_id}", response_class=HTMLResponse)
def case_page(
    request: Request,
    case_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VIEW)),
):
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")
    observations = list(reversed(case.observations))  # oldest first, for the trend
    return templates.TemplateResponse(
        request,
        "case.html",
        _context(request, db, user, case=case, trend=charts.vitals_trend(observations)),
    )


@router.post("/cases/{case_id}/verify")
def verify_from_ui(
    case_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.VERIFY_CASE)),
):
    case = services.get_case(db, case_id)
    if case is None:
        raise HTTPException(404, "Case record not found.")
    services.verify_case(db, case, user)
    return RedirectResponse(f"/cases/{case_id}", status_code=303)


@router.post("/cases/{case_id}/observations")
def add_observation_from_ui(
    case_id: int,
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
    user: models.User = Depends(auth.require(auth.RECORD_OBSERVATIONS)),
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
        recorded_by=user.full_name,
        recorded_by_id=user.id,
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
    next_url: str = Form(default="/"),
    db: Session = Depends(get_db),
    user: models.User = Depends(auth.require(auth.ACKNOWLEDGE_ALERT)),
):
    alert = db.get(models.Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alert not found.")
    services.acknowledge(db, alert, user)
    return RedirectResponse(_safe_next(next_url), status_code=303)
