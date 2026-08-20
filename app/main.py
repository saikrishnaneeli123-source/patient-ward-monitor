"""Ward monitor application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import Forbidden, NotAuthenticated
from app.config import get_settings
from app.db import init_db
from app.routers import api, ui

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DESCRIPTION = """
Scan a ward's case sheets, get one case record per patient, then track them with
NEWS2 early-warning scoring.

**Clinical decision support only.** Auto-extracted records are created as
*unverified* and must be checked against the original scan by a clinician.

Authenticate with a session cookie (log in at `/login`) or, for devices and
integrations, `Authorization: Bearer <token>`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    get_settings().upload_dir.mkdir(parents=True, exist_ok=True)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Patient Ward Monitor",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )
    settings = get_settings()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.cookie_secure,
    )

    @app.exception_handler(NotAuthenticated)
    def _not_authenticated(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(Forbidden)
    def _forbidden(request: Request, exc: Forbidden):
        detail = f"Your role ({exc.role.value}) may not {exc.permission}."
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=403)
        return RedirectResponse(f"/denied?reason={exc.permission}", status_code=303)

    app.include_router(api.router)
    app.include_router(ui.router)

    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
