# Patient Ward Monitor

Scan a ward's case sheets, get **one case record per patient**, then monitor those
patients with NEWS2 early-warning scoring.

Upload a photo or a multi-page PDF of the ward folder. Claude reads each sheet,
splits the document by patient, and creates a separate case record for every
person it finds — matching against patients already on the ward so a re-scan
updates their record instead of duplicating them.

![Ward board](docs/board.png)

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # add your ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...

python -m scripts.seed_demo   # optional: a demo ward with 6 patients
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/> for the ward board, or `/docs` for the API.

Without an API key the app still runs — scans are stored and records can be
created by hand — but automatic extraction is disabled and says so.

---

## How intake works

```
scan (PDF / image)
      │
      ▼
  Claude Opus 5 vision  ──►  CaseSheetBatch { sheets: [ per-patient data + page range ] }
      │
      ▼
  identity match  ──►  MRN  ──►  name + DOB  ──►  name + age  ──►  new patient
      │
      ▼
  one CaseRecord per sheet, status = UNVERIFIED
      │
      ▼
  clinician verifies against the original scan
```

**One sheet, one record.** A 40-page PDF holding 20 patients produces 20 case
records, each tagged with the pages it was read from (`source_pages`) so any
field can be traced back to the paper.

**Identity matching, strongest signal first.** Hospital number is authoritative.
Falling back to name + DOB is treated as reliable; a name-only or name + age
match still links the record but raises a warning asking staff to confirm it is
the same person. Same name with a *different* recorded DOB is treated as a
different person — never merged.

**Same patient, same admission → update, not duplicate.** A second sheet for
someone already on the board updates their open episode. A sheet with a
different admission date starts a new case record, so readmissions stay separate.

### The safety rules that shape the code

Automatic transcription of handwriting is useful and it is also how a wrong drug
dose gets into a chart. The pipeline is deliberately conservative:

| Rule | Where |
|---|---|
| Illegible fields are left `null` and named in `unreadable_fields` — never guessed | `extraction.py` (prompt) |
| Every auto-created record starts `unverified` and is listed in the review queue | `intake.apply_sheet` |
| A scan never overwrites clinical data a clinician has already **verified** — conflicts are flagged for manual reconciliation | `intake._apply_clinical` |
| Existing demographics are only gap-filled, never clobbered by a blank field | `intake._fill_patient_blanks` |
| A sheet with no name *and* no MRN is skipped, not filed under a guess | `intake.apply_sheet` |
| Confidence below 50% raises a warning on the record | `intake._sheet_warnings` |
| Medication entries with no readable drug name are dropped rather than invented | `intake._medications` |
| "None known" in the allergy box is shown but not rendered as a red allergy flag | `services.real_allergies` |
| A NEWS2 score computed from incomplete vitals is labelled *partial* — it can only be an underestimate | `scoring.calculate_news2` |

---

## Monitoring

Each set of vitals is scored with **NEWS2** (Royal College of Physicians): RR,
SpO₂ (scales 1 and 2), supplemental oxygen, systolic BP, pulse, temperature and
ACVPU consciousness. The score drives the ward board ordering (sickest first),
the escalation text, and the alert feed.

| Score | Risk | Response |
|---|---|---|
| 0 | Low | Routine observations, minimum 12-hourly |
| 1–4 | Low | Minimum 4–6 hourly |
| Any single parameter = 3 | Low–medium | Urgent review by a competent clinician; hourly obs |
| 5–6 | Medium | Urgent review by a ward doctor; hourly obs |
| ≥ 7 | High | Emergency assessment by a critical care capable team |

Vitals printed on the case sheet are transcribed as the first observation, so a
patient has a score the moment their record is created.

---

## API

Interactive docs at `/docs`. Everything the UI does is available as JSON.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/uploads` | Upload scans; returns per-sheet outcomes |
| `POST` | `/api/uploads/{id}/reprocess` | Re-run extraction on a stored scan |
| `GET` | `/api/uploads` · `/api/uploads/{id}` | Upload history and status |
| `GET` | `/api/board` | Ward board, sickest first |
| `GET` | `/api/cases` | Filter by `status`, `ward`, `verification` |
| `POST` | `/api/cases` | Create a record by hand (illegible scan) |
| `GET`/`PATCH` | `/api/cases/{id}` | Read / correct a record |
| `POST` | `/api/cases/{id}/verify` | Confirm against the original scan |
| `POST`/`GET` | `/api/cases/{id}/observations` | Record and list vitals |
| `GET` | `/api/patients?q=` | Search by name or MRN |
| `GET` | `/api/alerts` · `POST /api/alerts/{id}/acknowledge` | Alert feed |

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/uploads \
  -F "files=@ward-round.pdf" -F "uploaded_by=Sr. Mary Thomas"
```

```json
[{
  "upload": {"status": "completed", "sheets_detected": 3, "records_created": 3},
  "outcomes": [
    {"action": "created", "case_number": "CR-2026-00001", "patient_name": "Asha Rao",
     "page_range": "1", "confidence": 0.94, "warnings": []},
    {"action": "updated", "case_number": "CR-2026-00002", "patient_name": "Bilal Khan",
     "page_range": "2-3", "confidence": 0.71,
     "warnings": ["Illegible on the scan: oxygen flow rate"]}
  ]
}]
```

---

## Configuration

All settings take a `WARD_` prefix and can live in `.env` (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Enables automatic extraction (no prefix) |
| `WARD_DATABASE_URL` | `sqlite:///./ward.db` | Any SQLAlchemy URL |
| `WARD_UPLOAD_DIR` | `./uploads` | Where scans are stored |
| `WARD_EXTRACTION_MODEL` | `claude-opus-5` | Model used to read sheets |
| `WARD_MAX_UPLOAD_MB` | `25` | Per-file upload limit |

Accepted uploads: PDF, PNG, JPEG, WebP, GIF, TIFF, BMP. Images are downscaled to
1568px on the long edge and converted if needed; PDFs are sent whole so Claude
can follow a patient across continuation pages.

---

## Layout

```
app/
  main.py          FastAPI app
  config.py        settings
  db.py            engine and session
  models.py        Patient, CaseRecord, Upload, Observation, Alert, Ward, Bed
  extraction.py    Claude vision/PDF → structured sheets (pluggable backend)
  intake.py        identity matching, record creation, safety rules
  scoring.py       NEWS2
  services.py      queries shared by API and UI
  routers/         api.py (JSON) · ui.py (HTML)
  templates/       board, upload, review, case
scripts/seed_demo.py
tests/             85 tests
```

`extraction.set_extractor()` swaps the backend, which is how the tests run the
whole pipeline without touching the API.

---

## Tests

```bash
pytest -q      # 85 tests
```

Covers the NEWS2 chart parameter by parameter, identity matching and
de-duplication, the no-overwrite rules, alert escalation, and the HTTP layer
end to end.

---

## Before using this with real patients

This is a working application, not a deployed hospital system. It deliberately
does **not** yet include:

- **Authentication or authorisation.** Every endpoint is open. Put it behind an
  identity provider and add role checks before it touches a network.
- **Encryption at rest and PHI handling.** Scans sit unencrypted in
  `WARD_UPLOAD_DIR` and data in SQLite. Real deployment needs encrypted storage,
  retention limits, and a decision about what leaves the building — case sheets
  are sent to the Claude API for extraction, which is a data-processing
  arrangement your organisation must approve.
- **A tamper-evident audit trail.** Verification and acknowledgement are
  recorded, but edits are not versioned.
- **Regulatory clearance.** NEWS2 scoring and automated transcription in a
  clinical workflow may bring this under medical-device software rules in your
  jurisdiction (UKCA/CE under MDR, FDA CDS guidance, CDSCO, etc.).

> **Clinical decision support only.** Auto-extracted records are created
> unverified and must be checked against the original sheet by a clinician.
> NEWS2 scores assist ward staff; they do not replace clinical judgement.
