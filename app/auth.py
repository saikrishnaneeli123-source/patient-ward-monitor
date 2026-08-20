"""Authentication, password hashing and role-based access control.

Passwords are hashed with scrypt from the standard library — memory-hard, no
third-party dependency to keep patched. Sessions are signed cookies; devices and
integrations may instead present ``Authorization: Bearer <token>``, where only a
SHA-256 of the token is stored.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timezone

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Role, User

# scrypt at the RFC 7914 interactive-login parameters: n=2**14 costs ~16 MB and
# ~200 ms per attempt, which is slow enough to make offline cracking expensive
# and fast enough for a nurse logging in on ward hardware.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
# OpenSSL defaults to a 32 MB ceiling, which n=2**15 exceeds; raise it explicitly.
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class NotAuthenticated(Exception):
    """No valid session or token. HTML routes redirect; API routes get a 401."""


class Forbidden(Exception):
    """Authenticated, but the role lacks the permission."""

    def __init__(self, permission: str, role: Role):
        self.permission = permission
        self.role = role
        super().__init__(f"Role '{role.value}' may not {permission}.")


# --------------------------------------------------------------------------
# Passwords and tokens
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = os.urandom(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Returns False on any malformed hash."""
    try:
        scheme, n, r, p, salt_hex, expected_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(bytes.fromhex(expected_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, bytes.fromhex(expected_hex))


def generate_api_token() -> tuple[str, str]:
    """Return (token_to_show_once, hash_to_store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_api_token(token)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Brute-force throttling
# --------------------------------------------------------------------------
# In-process only: adequate for a single ward instance, and it fails closed on
# restart. A multi-instance deployment should move this to shared storage.

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
_failures: dict[str, list[float]] = {}


def is_locked_out(username: str) -> bool:
    recent = [t for t in _failures.get(username, []) if time.monotonic() - t < LOCKOUT_SECONDS]
    _failures[username] = recent
    return len(recent) >= MAX_ATTEMPTS


def record_failure(username: str) -> None:
    _failures.setdefault(username, []).append(time.monotonic())


def clear_failures(username: str) -> None:
    _failures.pop(username, None)


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------

VIEW = "view records"
UPLOAD = "upload case sheets"
RECORD_OBSERVATIONS = "record observations"
VERIFY_CASE = "verify a case record"
EDIT_CASE = "edit a case record"
DISCHARGE = "discharge a patient"
ACKNOWLEDGE_ALERT = "acknowledge alerts"
WRITE_NOTE = "write clinical notes"
RECEIVE_HANDOVER = "receive a handover"
VIEW_AUDIT = "read the audit log"
MANAGE_USERS = "manage users"

CLINICAL = {
    VIEW, UPLOAD, RECORD_OBSERVATIONS, VERIFY_CASE, ACKNOWLEDGE_ALERT,
    WRITE_NOTE, RECEIVE_HANDOVER,
}

ROLE_PERMISSIONS: dict[Role, set[str]] = {
    # Doctors own the clinical record: they may correct and discharge.
    Role.doctor: CLINICAL | {EDIT_CASE, DISCHARGE},
    # Nurses run the observations and may confirm a transcription against the sheet.
    Role.nurse: set(CLINICAL),
    # Ward clerks scan paperwork and read the board; no clinical authority.
    Role.clerk: {VIEW, UPLOAD},
    Role.readonly: {VIEW},
    Role.admin: CLINICAL | {EDIT_CASE, DISCHARGE, MANAGE_USERS, VIEW_AUDIT},
}


# Name -> value for every permission, so templates and tests read from the same
# place the role matrix does. A hand-kept copy silently drifts, and a permission
# missing from a template resolves to undefined — hiding the control with no error.
ALL_PERMISSIONS: dict[str, str] = {
    "VIEW": VIEW,
    "UPLOAD": UPLOAD,
    "RECORD_OBSERVATIONS": RECORD_OBSERVATIONS,
    "VERIFY_CASE": VERIFY_CASE,
    "EDIT_CASE": EDIT_CASE,
    "DISCHARGE": DISCHARGE,
    "ACKNOWLEDGE_ALERT": ACKNOWLEDGE_ALERT,
    "WRITE_NOTE": WRITE_NOTE,
    "RECEIVE_HANDOVER": RECEIVE_HANDOVER,
    "VIEW_AUDIT": VIEW_AUDIT,
    "MANAGE_USERS": MANAGE_USERS,
}


def permissions_for(role: Role) -> set[str]:
    return ROLE_PERMISSIONS.get(role, set())


def can(user: User | None, permission: str) -> bool:
    if user is None or not user.is_active:
        return False
    return permission in permissions_for(user.role)


# --------------------------------------------------------------------------
# Lookup and dependencies
# --------------------------------------------------------------------------

def authenticate(db: Session, username: str, password: str) -> User | None:
    """Verify credentials. Returns None for unknown user, bad password or lockout."""
    username = (username or "").strip().lower()
    if not username or is_locked_out(username):
        return None

    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None or not user.is_active:
        # Hash anyway so a missing user is not distinguishable by response time.
        verify_password(password, hash_password("timing-equalisation-placeholder"))
        record_failure(username)
        _audit_login(db, user, "unknown or deactivated account", username=username)
        db.commit()
        return None

    if not verify_password(password, user.password_hash):
        record_failure(username)
        _audit_login(db, user, "wrong password", username=username)
        db.commit()
        return None

    clear_failures(username)
    user.last_login_at = datetime.now(timezone.utc)
    _audit_login(db, user, LOGIN_OK)
    db.commit()
    return user


LOGIN_OK = "ok"


def _audit_login(db: Session, user: User | None, outcome: str, username: str | None = None) -> None:
    """Log the attempt. Imported lazily to keep auth free of an import cycle."""
    from app import audit

    succeeded = outcome == LOGIN_OK
    audit.record(
        db,
        actor=user if succeeded else None,
        action=audit.LOGIN_SUCCEEDED if succeeded else audit.LOGIN_FAILED,
        entity_type="user",
        entity_id=user.id if user else None,
        summary=(
            f"{user.full_name} signed in."
            if succeeded
            else f"Failed sign-in for '{username}' — {outcome}."
        ),
        details={"username": username or (user.username if user else None), "outcome": outcome},
    )


def user_from_token(db: Session, token: str) -> User | None:
    digest = hash_api_token(token)
    user = db.execute(select(User).where(User.api_token_hash == digest)).scalar_one_or_none()
    return user if user is not None and user.is_active else None


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Resolve the caller from a bearer token or the session cookie. May be None."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return user_from_token(db, header[7:].strip())

    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if not user_id:
        return None
    user = db.get(User, user_id)
    return user if user is not None and user.is_active else None


def require_login(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require(permission: str):
    """Dependency factory: ``user = Depends(require(auth.VERIFY_CASE))``."""

    def dependency(user: User | None = Depends(current_user)) -> User:
        if user is None:
            raise NotAuthenticated()
        if permission not in permissions_for(user.role):
            raise Forbidden(permission, user.role)
        return user

    return dependency


def create_user(
    db: Session, *, username: str, full_name: str, password: str, role: Role, with_token: bool = False
) -> tuple[User, str | None]:
    """Create a staff account. Returns (user, api_token shown once or None)."""
    username = username.strip().lower()
    if not username:
        raise ValueError("Username is required.")
    if db.execute(select(User).where(User.username == username)).scalar_one_or_none():
        raise ValueError(f"User '{username}' already exists.")

    token, token_hash = generate_api_token() if with_token else (None, None)
    user = User(
        username=username,
        full_name=full_name.strip() or username,
        password_hash=hash_password(password),
        role=role,
        api_token_hash=token_hash,
    )
    db.add(user)
    db.commit()
    return user, token
