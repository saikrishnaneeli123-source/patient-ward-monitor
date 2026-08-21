import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Point the app at a throwaway database *before* backend.db is imported.
_TMP = tempfile.mkdtemp(prefix="pwm-test-")
os.environ["PWM_DB"] = str(Path(_TMP) / "test.db")

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import app


@pytest.fixture()
def client():
    db.reset_connection()
    if Path(os.environ["PWM_DB"]).exists():
        Path(os.environ["PWM_DB"]).unlink()
    db.init_db()
    with TestClient(app) as test_client:
        yield test_client
    db.reset_connection()


@pytest.fixture()
def patient(client):
    response = client.post("/api/patients", json={
        "name": "Kamala Devi",
        "age": 72,
        "caregiver_name": "Ravi",
        "caregiver_phone": "9990001111",
        "routine": {"breakfast": "08:00", "lunch": "13:00", "dinner": "20:00", "bed": "22:00"},
    })
    assert response.status_code == 201, response.text
    return response.json()
