"""Authentication, the role matrix, and the boundaries between them."""
import pytest

from app import auth
from app.models import Role
from tests.conftest import PASSWORD


# --------------------------------------------------------------------------
# Password hashing
# --------------------------------------------------------------------------

def test_password_round_trip():
    stored = auth.hash_password("a-good-long-password")
    assert auth.verify_password("a-good-long-password", stored)
    assert not auth.verify_password("a-good-long-passwore", stored)


def test_hash_is_salted_so_equal_passwords_differ():
    assert auth.hash_password("same-password") != auth.hash_password("same-password")


def test_plaintext_never_appears_in_the_hash():
    assert "hunter2hunter2" not in auth.hash_password("hunter2hunter2")


def test_short_passwords_are_rejected():
    with pytest.raises(ValueError):
        auth.hash_password("short")


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$bad", "md5$1$2$3$4$5", "scrypt$a$b$c$d$e"])
def test_malformed_hashes_fail_closed(stored):
    assert auth.verify_password("anything", stored) is False


def test_production_cost_parameters():
    """The shipped parameters, asserted directly since tests reuse one hash."""
    assert auth._SCRYPT_N == 2**14, "scrypt cost must stay at the RFC interactive setting"
    assert auth.hash_password("check-the-prefix").startswith("scrypt$16384$8$1$")


def test_api_tokens_are_stored_only_as_a_hash():
    token, digest = auth.generate_api_token()
    assert token != digest
    assert auth.hash_api_token(token) == digest
    assert len(token) > 30


# --------------------------------------------------------------------------
# The role matrix
# --------------------------------------------------------------------------

CLINICAL = {auth.VIEW, auth.UPLOAD, auth.RECORD_OBSERVATIONS, auth.VERIFY_CASE,
            auth.ACKNOWLEDGE_ALERT, auth.WRITE_NOTE, auth.RECEIVE_HANDOVER}

EXPECTED = {
    Role.admin: CLINICAL | {auth.EDIT_CASE, auth.DISCHARGE, auth.MANAGE_USERS, auth.VIEW_AUDIT},
    Role.doctor: CLINICAL | {auth.EDIT_CASE, auth.DISCHARGE},
    Role.nurse: set(CLINICAL),
    Role.clerk: {auth.VIEW, auth.UPLOAD},
    Role.readonly: {auth.VIEW},
}


@pytest.mark.parametrize("role,expected", EXPECTED.items(), ids=lambda v: getattr(v, "value", ""))
def test_role_permissions_are_exactly_as_documented(role, expected):
    assert auth.permissions_for(role) == expected


def test_only_admin_manages_users():
    assert [r for r in Role if auth.MANAGE_USERS in auth.permissions_for(r)] == [Role.admin]


def test_clerks_and_readonly_have_no_clinical_authority():
    for role in (Role.clerk, Role.readonly):
        perms = auth.permissions_for(role)
        assert auth.VERIFY_CASE not in perms
        assert auth.RECORD_OBSERVATIONS not in perms
        assert auth.EDIT_CASE not in perms
        assert auth.WRITE_NOTE not in perms


def test_only_admin_reads_the_audit_log():
    assert [r for r in Role if auth.VIEW_AUDIT in auth.permissions_for(r)] == [Role.admin]


def test_nurses_may_verify_but_not_edit_or_discharge():
    perms = auth.permissions_for(Role.nurse)
    assert auth.VERIFY_CASE in perms
    assert auth.EDIT_CASE not in perms
    assert auth.DISCHARGE not in perms


def test_can_rejects_none_and_deactivated_users(db):
    from app.models import User

    assert auth.can(None, auth.VIEW) is False
    user = db.query(User).filter_by(username="doctor").one()
    assert auth.can(user, auth.VIEW) is True
    user.is_active = False
    assert auth.can(user, auth.VIEW) is False


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

def test_authenticate_accepts_correct_credentials(db):
    user = auth.authenticate(db, "nurse", PASSWORD)
    assert user is not None and user.role == Role.nurse
    assert user.last_login_at is not None


def test_authenticate_is_case_insensitive_on_username(db):
    assert auth.authenticate(db, "NURSE", PASSWORD) is not None


@pytest.mark.parametrize("username,password", [("nurse", "wrong"), ("ghost", PASSWORD), ("", "")])
def test_authenticate_rejects_bad_credentials(db, username, password):
    assert auth.authenticate(db, username, password) is None


def test_deactivated_users_cannot_authenticate(db):
    from app.models import User

    db.query(User).filter_by(username="nurse").one().is_active = False
    db.commit()
    assert auth.authenticate(db, "nurse", PASSWORD) is None


def test_repeated_failures_lock_the_account(db):
    for _ in range(auth.MAX_ATTEMPTS):
        assert auth.authenticate(db, "nurse", "wrong") is None
    assert auth.is_locked_out("nurse")
    # Even the correct password is refused while locked out.
    assert auth.authenticate(db, "nurse", PASSWORD) is None


def test_a_successful_login_clears_the_failure_count(db):
    auth.authenticate(db, "nurse", "wrong")
    auth.authenticate(db, "nurse", PASSWORD)
    assert not auth.is_locked_out("nurse")


def test_duplicate_usernames_are_rejected(db):
    with pytest.raises(ValueError, match="already exists"):
        auth.create_user(db, username="nurse", full_name="Impostor",
                         password="another-password", role=Role.nurse)


# --------------------------------------------------------------------------
# HTTP boundaries
# --------------------------------------------------------------------------

PROTECTED_API = ["/api/board", "/api/cases", "/api/patients", "/api/alerts", "/api/uploads", "/api/me"]


@pytest.mark.parametrize("path", PROTECTED_API)
def test_api_requires_authentication(anon_client, path):
    assert anon_client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/", "/upload", "/review", "/users"])
def test_html_pages_redirect_to_login(anon_client, path):
    response = anon_client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_page_is_reachable_without_a_session(anon_client):
    assert anon_client.get("/login").status_code == 200


def test_login_failure_message_does_not_reveal_whether_the_user_exists(anon_client):
    real = anon_client.post("/login", data={"username": "nurse", "password": "wrong"},
                            follow_redirects=False)
    ghost = anon_client.post("/login", data={"username": "ghost", "password": "wrong"},
                             follow_redirects=False)
    assert real.headers["location"] == ghost.headers["location"]


def test_login_then_logout_ends_the_session(client):
    assert client.get("/api/me").status_code == 200
    client.post("/logout", follow_redirects=False)
    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize("target", ["https://evil.example.com/steal", "//evil.example.com", "javascript:alert(1)"])
def test_login_will_not_redirect_off_site(anon_client, target):
    """A `next` parameter must never send a user to another host after login."""
    response = anon_client.post(
        "/login", data={"username": "doctor", "password": PASSWORD, "next": target},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


def test_me_reports_the_callers_permissions(client_as):
    body = client_as(Role.nurse).get("/api/me").json()
    assert body["user"]["role"] == "nurse"
    assert auth.VERIFY_CASE in body["permissions"]
    assert auth.MANAGE_USERS not in body["permissions"]


def test_bearer_token_authenticates_a_device(anon_client, client_as, db):
    created = client_as(Role.admin).post("/api/users", json={
        "username": "obs-tablet", "full_name": "Bay 3 tablet",
        "password": "device-password-1", "role": "nurse", "issue_api_token": True,
    }).json()
    token = created["api_token"]
    assert token

    response = anon_client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "obs-tablet"

    assert anon_client.get("/api/me", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_deactivating_a_user_revokes_their_token_immediately(anon_client, client_as):
    admin = client_as(Role.admin)
    created = admin.post("/api/users", json={
        "username": "old-tablet", "full_name": "Retired tablet",
        "password": "device-password-1", "role": "nurse", "issue_api_token": True,
    }).json()
    token = created["api_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert anon_client.get("/api/me", headers=headers).status_code == 200

    admin.post(f"/api/users/{created['user']['id']}/deactivate")
    assert anon_client.get("/api/me", headers=headers).status_code == 401


def test_admin_cannot_deactivate_themselves(client_as):
    admin = client_as(Role.admin)
    me = admin.get("/api/me").json()["user"]["id"]
    assert admin.post(f"/api/users/{me}/deactivate").status_code == 400


# --------------------------------------------------------------------------
# Role boundaries on real endpoints
# --------------------------------------------------------------------------

@pytest.fixture
def a_case(client, png_bytes, fake_extractor):
    """A case record created by a doctor, for other roles to act on."""
    from app.extraction import CaseSheetBatch, ExtractedCaseSheet

    fake_extractor(CaseSheetBatch(sheets=[
        ExtractedCaseSheet(full_name="Asha Rao", mrn="A-1", ward_name="Medical A",
                           bed_label="4", confidence=0.9)
    ]))
    client.post("/api/uploads", files=[("files", ("s.png", png_bytes, "image/png"))])
    return client.get("/api/cases").json()[0]["id"]


@pytest.mark.parametrize(
    "role,expected",
    [(Role.admin, 201), (Role.doctor, 201), (Role.nurse, 201), (Role.clerk, 201), (Role.readonly, 403)],
    ids=lambda v: str(getattr(v, "value", v)),
)
def test_upload_permission(client_as, png_bytes, fake_extractor, role, expected):
    from app.extraction import CaseSheetBatch

    fake_extractor(CaseSheetBatch())
    response = client_as(role).post(
        "/api/uploads", files=[("files", ("s.png", png_bytes, "image/png"))]
    )
    assert response.status_code == expected


@pytest.mark.parametrize(
    "role,expected",
    [(Role.admin, 200), (Role.doctor, 200), (Role.nurse, 200), (Role.clerk, 403), (Role.readonly, 403)],
    ids=lambda v: str(getattr(v, "value", v)),
)
def test_verify_permission(client_as, a_case, role, expected):
    assert client_as(role).post(f"/api/cases/{a_case}/verify").status_code == expected


@pytest.mark.parametrize(
    "role,expected",
    [(Role.admin, 201), (Role.doctor, 201), (Role.nurse, 201), (Role.clerk, 403), (Role.readonly, 403)],
    ids=lambda v: str(getattr(v, "value", v)),
)
def test_record_observations_permission(client_as, a_case, role, expected):
    response = client_as(role).post(
        f"/api/cases/{a_case}/observations", json={"pulse": 80, "spo2": 97}
    )
    assert response.status_code == expected


@pytest.mark.parametrize(
    "role,expected",
    [(Role.admin, 200), (Role.doctor, 200), (Role.nurse, 403), (Role.clerk, 403), (Role.readonly, 403)],
    ids=lambda v: str(getattr(v, "value", v)),
)
def test_edit_case_permission(client_as, a_case, role, expected):
    assert client_as(role).patch(f"/api/cases/{a_case}", json={"bed_label": "9"}).status_code == expected


@pytest.mark.parametrize(
    "role,expected",
    [(Role.admin, 200), (Role.doctor, 403), (Role.nurse, 403), (Role.clerk, 403), (Role.readonly, 403)],
    ids=lambda v: str(getattr(v, "value", v)),
)
def test_user_management_permission(client_as, role, expected):
    assert client_as(role).get("/api/users").status_code == expected


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
def test_every_role_can_read_the_board(client_as, role):
    assert client_as(role).get("/api/board").status_code == 200


def test_a_403_names_the_role_and_the_action(client_as, a_case):
    body = client_as(Role.readonly).post(f"/api/cases/{a_case}/verify").json()
    assert "readonly" in body["detail"]
    assert "verify" in body["detail"]


def test_the_ui_hides_actions_a_role_cannot_take(client_as, a_case):
    """A readonly user should not be shown a Verify button that would 403."""
    page = client_as(Role.readonly).get(f"/cases/{a_case}").text
    assert "Confirm this record is correct" not in page
    assert "cannot verify records" in page

    nurse_page = client_as(Role.nurse).get(f"/cases/{a_case}").text
    assert "Confirm this record is correct" in nurse_page


def test_verification_is_attributed_to_the_signed_in_nurse(client_as, a_case, client):
    client_as(Role.nurse).post(f"/api/cases/{a_case}/verify")
    case = client.get(f"/api/cases/{a_case}").json()
    assert case["verified_by"] == "Test Nurse"


def test_an_observation_cannot_be_attributed_to_another_member_of_staff(client_as, a_case):
    """`recorded_by` is not accepted from the request — it comes from the session."""
    nurse = client_as(Role.nurse)
    rejected = nurse.post(
        f"/api/cases/{a_case}/observations", json={"pulse": 80, "recorded_by": "Someone Else"}
    )
    assert rejected.status_code == 422

    nurse.post(f"/api/cases/{a_case}/observations", json={"pulse": 80})
    assert nurse.get(f"/api/cases/{a_case}/observations").json()[0]["recorded_by"] == "Test Nurse"


def test_every_permission_is_exposed_to_the_templates():
    """A permission missing from PERMS resolves to undefined and hides its control."""
    from app.routers.ui import templates

    exposed = templates.env.globals["PERMS"]
    declared = {v for perms in auth.ROLE_PERMISSIONS.values() for v in perms}
    assert declared <= set(exposed.values())
    assert set(exposed) == set(auth.ALL_PERMISSIONS)


# --------------------------------------------------------------------------
# Production configuration
# --------------------------------------------------------------------------

def _settings(**env):
    """Build Settings with a clean cache and a controlled environment."""
    import os
    from unittest.mock import patch

    from app.config import Settings, get_settings

    get_settings.cache_clear()
    keys = ("WARD_ENV", "WARD_SECRET_KEY", "WARD_COOKIE_SECURE")
    cleaned = {k: v for k, v in os.environ.items() if k not in keys}
    with patch.dict(os.environ, {**cleaned, **env}, clear=True):
        try:
            return get_settings()
        finally:
            get_settings.cache_clear()


def test_production_refuses_to_start_without_a_secret_key():
    """A random key per restart means everyone is logged out on every deploy."""
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="WARD_SECRET_KEY must be set"):
        _settings(WARD_ENV="production")


def test_development_generates_a_key_and_carries_on():
    assert _settings(WARD_ENV="development").secret_key


def test_production_forces_https_only_cookies():
    assert _settings(WARD_ENV="production", WARD_SECRET_KEY="x" * 40).cookie_secure is True


def test_an_explicit_cookie_setting_still_wins_in_production():
    settings = _settings(
        WARD_ENV="production", WARD_SECRET_KEY="x" * 40, WARD_COOKIE_SECURE="false"
    )
    assert settings.cookie_secure is False
