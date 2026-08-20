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

from app import extraction  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.extraction import CaseSheetBatch  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


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


@pytest.fixture
def client():
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
