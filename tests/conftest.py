"""Test fixtures.

The database URL has to be set before ``app.db`` is imported, because the engine
is built at import time from the settings.
"""
import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="ward-tests-"))
os.environ["WARD_DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["WARD_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["WARD_SECRET_KEY"] = "test-secret-key-not-used-in-production"

from app import audit, auth, extraction  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.extraction import CaseSheetBatch  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Role, User  # noqa: E402

PASSWORD = "ward-test-password"
# scrypt is deliberately slow, so hash the shared test password once here rather
# than once per user per test. Login still exercises the real verify path, and
# test_auth asserts the production cost parameters directly.
_PASSWORD_HASH = auth.hash_password(PASSWORD)


class FakeExtractor:
    """Returns a canned batch so intake can be tested without the API."""

    available = True

    def __init__(self, batch: CaseSheetBatch | None = None):
        self.batch = batch or CaseSheetBatch()
        self.calls: list[dict] = []

    def extract(self, *, data: bytes, content_type: str, filename: str) -> CaseSheetBatch:
        self.calls.append({"data": data, "content_type": content_type, "filename": filename})
        return self.batch


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Tests build the schema directly rather than through init_db, so install the
    # append-only guards here too — otherwise they would go untested.
    audit.install_guards()
    audit.install_db_triggers(engine)
    auth._failures.clear()
    session = SessionLocal()
    try:
        # One account per role, so permission boundaries are testable.
        session.add_all(
            User(
                username=role.value,
                full_name=f"Test {role.value.title()}",
                password_hash=_PASSWORD_HASH,
                role=role,
            )
            for role in Role
        )
        session.commit()
    finally:
        session.close()
    yield
    extraction.set_extractor(None)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def fake_extractor():
    def _install(batch: CaseSheetBatch) -> FakeExtractor:
        stub = FakeExtractor(batch)
        extraction.set_extractor(stub)
        return stub

    return _install


def _login(test_client, role: Role):
    response = test_client.post(
        "/login",
        data={"username": role.value, "password": PASSWORD, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, "login should redirect on success"
    return test_client


@pytest.fixture
def client():
    """Signed in as a doctor — the role with full clinical permissions."""
    from fastapi.testclient import TestClient

    with TestClient(fastapi_app) as test_client:
        yield _login(test_client, Role.doctor)


@pytest.fixture
def client_as():
    """Factory for a client signed in as any role: ``client_as(Role.nurse)``."""
    from fastapi.testclient import TestClient

    clients = []

    def _make(role: Role):
        test_client = TestClient(fastapi_app)
        test_client.__enter__()
        clients.append(test_client)
        return _login(test_client, role)

    yield _make
    for test_client in clients:
        test_client.__exit__(None, None, None)


@pytest.fixture
def anon_client():
    """Not signed in."""
    from fastapi.testclient import TestClient

    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def png_bytes() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def a_case_id(client, png_bytes, fake_extractor):
    """A case record created through the API, for tests that need one."""
    from app.extraction import CaseSheetBatch, ExtractedCaseSheet

    fake_extractor(CaseSheetBatch(sheets=[
        ExtractedCaseSheet(full_name="Asha Rao", mrn="A-1", ward_name="Medical A",
                           bed_label="4", confidence=0.9)
    ]))
    client.post("/api/uploads", files=[("files", ("s.png", png_bytes, "image/png"))])
    return client.get("/api/cases").json()[0]["id"]
