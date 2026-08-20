"""End-to-end tests through the HTTP layer."""
from app.extraction import CaseSheetBatch, ExtractedCaseSheet, ExtractedVitals


def sheet(**overrides) -> ExtractedCaseSheet:
    base = dict(full_name="Asha Rao", mrn="MRN-8891", age_years=54, sex="F",
                ward_name="Medical A", bed_label="4", confidence=0.9)
    return ExtractedCaseSheet(**{**base, **overrides})


def upload(client, png_bytes, name="sheet.png"):
    return client.post("/api/uploads", files=[("files", (name, png_bytes, "image/png"))])


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_upload_creates_one_case_record_per_patient(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[
        sheet(full_name="Asha Rao", mrn="A-1", bed_label="4", page_range="1"),
        sheet(full_name="Bilal Khan", mrn="B-2", bed_label="5", page_range="2"),
    ]))
    response = upload(client, png_bytes)
    assert response.status_code == 201

    result = response.json()[0]
    assert result["upload"]["status"] == "completed"
    assert result["upload"]["records_created"] == 2
    assert [o["action"] for o in result["outcomes"]] == ["created", "created"]
    assert {o["patient_name"] for o in result["outcomes"]} == {"Asha Rao", "Bilal Khan"}

    cases = client.get("/api/cases").json()
    assert len(cases) == 2
    assert all(c["verification"] == "unverified" for c in cases)


def test_reuploading_the_same_file_does_not_duplicate_the_upload(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    first = upload(client, png_bytes).json()[0]["upload"]["id"]
    second = upload(client, png_bytes).json()[0]["upload"]["id"]
    assert first == second
    assert len(client.get("/api/uploads").json()) == 1
    assert len(client.get("/api/cases").json()) == 1


def test_unsupported_file_type_is_rejected(client, fake_extractor):
    fake_extractor(CaseSheetBatch())
    response = client.post("/api/uploads", files=[("files", ("notes.txt", b"hello", "text/plain"))])
    assert response.status_code == 415


def test_extraction_disabled_stores_the_scan_and_reports_why(client, png_bytes):
    # No extractor installed and no API key -> NullExtractor.
    response = upload(client, png_bytes)
    result = response.json()[0]
    assert result["upload"]["status"] == "failed"
    assert "ANTHROPIC_API_KEY" in result["error"]
    # The scan itself is kept so it can be re-extracted later.
    assert client.get(f"/api/uploads/{result['upload']['id']}").status_code == 200


def test_reprocess_reruns_extraction_on_a_stored_scan(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch())  # first pass finds nothing
    upload_id = upload(client, png_bytes).json()[0]["upload"]["id"]
    assert client.get("/api/cases").json() == []

    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    result = client.post(f"/api/uploads/{upload_id}/reprocess").json()
    assert result["upload"]["records_created"] == 1
    assert len(client.get("/api/cases").json()) == 1


def test_board_is_sorted_sickest_first(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[
        sheet(full_name="Stable Sam", mrn="S-1", bed_label="1",
              vitals=ExtractedVitals(respiratory_rate=16, spo2=98, systolic_bp=120,
                                     pulse=72, temperature_c=36.7, consciousness="alert")),
        sheet(full_name="Sick Sara", mrn="S-2", bed_label="2",
              vitals=ExtractedVitals(respiratory_rate=28, spo2=88, on_oxygen=True, systolic_bp=86,
                                     pulse=134, temperature_c=39.5, consciousness="voice")),
    ]))
    upload(client, png_bytes)

    board = client.get("/api/board").json()
    assert [row["patient_name"] for row in board] == ["Sick Sara", "Stable Sam"]
    assert board[0]["risk_level"] == "high"
    assert board[0]["open_alerts"] >= 1
    assert board[1]["news2_score"] == 0


def test_verify_flow(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    upload(client, png_bytes)
    case_id = client.get("/api/cases").json()[0]["id"]

    verified = client.post(f"/api/cases/{case_id}/verify").json()
    assert verified["verification"] == "verified"
    # Attributed to the signed-in user, not to a name in the request body.
    assert verified["verified_by"] == "Test Doctor"
    assert verified["verified_at"] is not None

    assert client.get("/api/cases", params={"verification": "unverified"}).json() == []


def test_recording_observations_scores_and_alerts(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    upload(client, png_bytes)
    case_id = client.get("/api/cases").json()[0]["id"]

    observation = client.post(
        f"/api/cases/{case_id}/observations",
        json={"respiratory_rate": 26, "spo2": 89, "on_oxygen": True, "systolic_bp": 88,
              "pulse": 128, "temperature_c": 39.2, "consciousness": "confusion"},
    ).json()

    assert observation["news2_score"] == 18
    assert observation["risk_level"] == "high"
    assert observation["news2_breakdown"]["response"].startswith("Emergency assessment")

    alerts = client.get("/api/alerts").json()
    assert any(a["severity"] == "critical" for a in alerts)

    acked = client.post(f"/api/alerts/{alerts[0]['id']}/acknowledge").json()
    assert acked["acknowledged_by"] == "Test Doctor"
    assert client.get("/api/alerts").json() == [] or all(
        a["id"] != acked["id"] for a in client.get("/api/alerts").json()
    )


def test_observation_validation_rejects_impossible_values(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    upload(client, png_bytes)
    case_id = client.get("/api/cases").json()[0]["id"]
    assert client.post(f"/api/cases/{case_id}/observations", json={"spo2": 140}).status_code == 422


def test_manual_case_creation_is_verified_on_creation(client):
    response = client.post("/api/cases", json={
        "full_name": "Manual Meera", "mrn": "M-9", "age_years": 61,
        "ward_name": "Medical A", "bed_label": "7",
        "provisional_diagnosis": "Illegible scan — entered by hand",
        "allergies": ["Sulfa"],
    })
    assert response.status_code == 201
    case = response.json()
    assert case["verification"] == "verified"
    assert case["extraction_confidence"] is None
    assert case["allergies"] == ["Sulfa"]


def test_patient_search(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[
        sheet(full_name="Asha Rao", mrn="A-1"), sheet(full_name="Bilal Khan", mrn="B-2"),
    ]))
    upload(client, png_bytes)
    assert [p["full_name"] for p in client.get("/api/patients", params={"q": "asha"}).json()] == ["Asha Rao"]
    assert [p["full_name"] for p in client.get("/api/patients", params={"q": "B-2"}).json()] == ["Bilal Khan"]


def test_case_update_and_discharge(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    upload(client, png_bytes)
    case_id = client.get("/api/cases").json()[0]["id"]

    updated = client.patch(f"/api/cases/{case_id}", json={"bed_label": "9", "status": "discharged"}).json()
    assert updated["bed_label"] == "9"
    assert updated["discharge_date"] is not None
    assert client.get("/api/cases").json() == []  # active board is now empty


def test_ui_pages_render(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet(vitals=ExtractedVitals(respiratory_rate=22, spo2=93))]))
    upload(client, png_bytes)
    case_id = client.get("/api/cases").json()[0]["id"]

    for path in ["/", "/upload", "/review", f"/cases/{case_id}"]:
        page = client.get(path)
        assert page.status_code == 200, path
        assert "Ward Monitor" in page.text

    assert "Asha Rao" in client.get("/").text
    assert "unverified" in client.get("/review").text


def test_ui_upload_and_verify_round_trip(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    page = client.post(
        "/upload",
        files=[("files", ("scan.png", png_bytes, "image/png"))],
    
    )
    assert page.status_code == 200
    assert "Asha Rao" in page.text

    case_id = client.get("/api/cases").json()[0]["id"]
    client.post(f"/cases/{case_id}/verify", follow_redirects=True)
    assert client.get(f"/api/cases/{case_id}").json()["verification"] == "verified"


def test_ui_observation_form(client, png_bytes, fake_extractor):
    fake_extractor(CaseSheetBatch(sheets=[sheet()]))
    upload(client, png_bytes)
    case_id = client.get("/api/cases").json()[0]["id"]

    client.post(
        f"/cases/{case_id}/observations",
        data={"respiratory_rate": "22", "spo2": "94", "on_oxygen": "on", "systolic_bp": "105",
              "pulse": "96", "temperature_c": "38.2", "consciousness": "alert"},
        follow_redirects=True,
    )
    observations = client.get(f"/api/cases/{case_id}/observations").json()
    assert len(observations) == 1
    assert observations[0]["news2_score"] == 8
    assert observations[0]["on_oxygen"] is True
