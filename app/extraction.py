"""Turn a scanned case sheet into structured, per-patient data.

A single upload may contain several patients' sheets (a nurse scans the whole
folder in one pass), so the extractor always returns a *list* of sheets, each
tagged with the pages it was read from. Intake then creates one case record per
returned sheet.

Everything here is deliberately conservative: fields that cannot be read are
returned as ``null`` and named in ``unreadable_fields`` rather than guessed.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from app.config import get_settings

logger = logging.getLogger(__name__)

# Longest edge we send to the API. Claude downsamples above ~1568px anyway, so
# shrinking first cuts tokens and latency without losing legibility.
MAX_IMAGE_EDGE = 1568
SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
PDF_TYPE = "application/pdf"


class ExtractionError(RuntimeError):
    """Raised when a scan could not be turned into structured data."""


class ExtractionUnavailable(ExtractionError):
    """Raised when no extraction backend is configured (no API key)."""


# --------------------------------------------------------------------------
# Output schema
# --------------------------------------------------------------------------
# Dates are strings, not `date`: handwritten sheets use every format under the
# sun, and a strict type would fail the whole extraction over one smudged digit.
# `app.intake` parses them leniently and keeps the raw text on failure.


class ExtractedMedication(BaseModel):
    name: str | None = None
    dose: str | None = None
    route: str | None = None
    frequency: str | None = None


class ExtractedVitals(BaseModel):
    respiratory_rate: int | None = None
    spo2: int | None = None
    on_oxygen: bool | None = None
    systolic_bp: int | None = None
    diastolic_bp: int | None = None
    pulse: int | None = None
    temperature_c: float | None = None
    consciousness: str | None = Field(
        default=None, description="ACVPU: alert, confusion, voice, pain, or unresponsive."
    )


class ExtractedCaseSheet(BaseModel):
    """One patient's case sheet as read off the scan."""

    page_range: str | None = Field(
        default=None, description="1-indexed pages this patient's sheet occupies, e.g. '3' or '3-5'."
    )

    # Identity
    mrn: str | None = Field(default=None, description="Hospital number / MRN / UHID exactly as written.")
    full_name: str | None = None
    age_years: int | None = None
    date_of_birth: str | None = Field(default=None, description="As written on the sheet.")
    sex: str | None = Field(default=None, description="male, female, other, or unknown.")
    phone: str | None = None
    address: str | None = None
    next_of_kin: str | None = None
    next_of_kin_phone: str | None = None

    # Admission
    ward_name: str | None = None
    bed_label: str | None = Field(default=None, description="Bed or cot number.")
    admission_date: str | None = None
    consultant: str | None = None
    department: str | None = Field(default=None, description="Unit or speciality.")

    # Clinical
    chief_complaint: str | None = None
    provisional_diagnosis: str | None = None
    history: str | None = Field(default=None, description="History of presenting illness, verbatim.")
    allergies: list[str] = Field(default_factory=list)
    comorbidities: list[str] = Field(default_factory=list)
    medications: list[ExtractedMedication] = Field(default_factory=list)
    vitals: ExtractedVitals | None = None
    notes: str | None = None

    # Quality signals
    confidence: float = Field(
        default=0.0, description="0.0-1.0 confidence that this sheet was transcribed correctly."
    )
    unreadable_fields: list[str] = Field(
        default_factory=list, description="Field names that were present but illegible."
    )


class CaseSheetBatch(BaseModel):
    sheets: list[ExtractedCaseSheet] = Field(default_factory=list)
    document_notes: str | None = Field(
        default=None, description="Anything about the scan itself: blank pages, poor quality, duplicates."
    )


SYSTEM_PROMPT = """You transcribe scanned hospital ward case sheets into structured records.

Rules:
1. Return one entry in `sheets` for each DISTINCT PATIENT in the document. A single \
patient's sheet may span several pages (continuation sheets, drug charts, obs charts) — \
merge those into one entry and give the full page range. Never split one patient across \
two entries, and never merge two patients into one.
2. Transcribe what is written. Do NOT infer, expand, normalise or correct clinical content. \
Copy drug names, doses and allergies exactly as they appear.
3. If a field is absent, use null (or an empty list). If a field is present but you cannot \
read it confidently, use null AND name it in `unreadable_fields`. Guessing a dose or an \
allergy is far worse than leaving it blank.
4. `confidence` reflects how sure you are of the transcription overall: 0.9+ for clean printed \
text, ~0.5 for difficult handwriting, below 0.3 if it is largely illegible.
5. Ignore blank pages, duplicated scans and unrelated paperwork; mention them in `document_notes`.
6. Vitals: record only the MOST RECENT set on the sheet. Temperature in Celsius — convert from \
Fahrenheit if the sheet uses it, and say so in `notes`."""

USER_INSTRUCTION = (
    "Extract every patient case sheet in this document. One entry per patient, "
    "with the page range each was read from."
)


class CaseSheetExtractor(Protocol):
    """Pluggable backend so the intake pipeline can be tested without the API."""

    def extract(self, *, data: bytes, content_type: str, filename: str) -> CaseSheetBatch: ...


class NullExtractor:
    """Used when no API key is configured — manual entry still works."""

    available = False

    def extract(self, *, data: bytes, content_type: str, filename: str) -> CaseSheetBatch:
        raise ExtractionUnavailable(
            "Automatic extraction is disabled because ANTHROPIC_API_KEY is not set. "
            "The scan has been stored; create the case record manually, or set the key and re-run intake."
        )


class ClaudeExtractor:
    """Reads case sheets with Claude's vision / document understanding."""

    available = True

    def __init__(self, client=None, model: str | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.extraction_model
        if client is not None:
            self._client = client
        else:
            import anthropic

            self._client = anthropic.Anthropic()

    def extract(self, *, data: bytes, content_type: str, filename: str) -> CaseSheetBatch:
        block = self._build_content_block(data, content_type, filename)
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": [block, {"type": "text", "text": USER_INSTRUCTION}]}],
                output_format=CaseSheetBatch,
            )
        except Exception as exc:  # surfaced to the operator on the upload row
            raise ExtractionError(f"Extraction request failed: {exc}") from exc

        batch = response.parsed_output
        if batch is None:
            raise ExtractionError("The model returned no structured output for this scan.")
        return batch

    def _build_content_block(self, data: bytes, content_type: str, filename: str) -> dict:
        if content_type == PDF_TYPE:
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": PDF_TYPE,
                    "data": base64.standard_b64encode(data).decode("utf-8"),
                },
            }

        media_type, payload = normalise_image(data, content_type)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(payload).decode("utf-8"),
            },
        }


def normalise_image(data: bytes, content_type: str) -> tuple[str, bytes]:
    """Downscale and, if needed, convert an image into a format the API accepts."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return content_type, data

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            needs_convert = content_type not in SUPPORTED_IMAGE_TYPES
            needs_resize = max(img.size) > MAX_IMAGE_EDGE
            if not (needs_convert or needs_resize):
                return content_type, data

            if needs_resize:
                img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            return "image/png", buffer.getvalue()
    except Exception as exc:
        raise ExtractionError(f"Could not read image '{content_type}': {exc}") from exc


def page_count(data: bytes, content_type: str) -> int | None:
    if content_type != PDF_TYPE:
        return 1
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None


_extractor: CaseSheetExtractor | None = None


def get_extractor() -> CaseSheetExtractor:
    """Return the configured extractor, falling back to NullExtractor without a key."""
    global _extractor
    if _extractor is None:
        import os

        if os.environ.get("ANTHROPIC_API_KEY"):
            _extractor = ClaudeExtractor()
        else:
            logger.warning("ANTHROPIC_API_KEY not set — automatic case sheet extraction is disabled.")
            _extractor = NullExtractor()
    return _extractor


def set_extractor(extractor: CaseSheetExtractor | None) -> None:
    """Override the extractor (used by tests and by the demo seed script)."""
    global _extractor
    _extractor = extractor


def detect_content_type(filename: str, declared: str | None) -> str:
    if declared and declared != "application/octet-stream":
        return declared
    suffix = Path(filename).suffix.lower()
    return {
        ".pdf": PDF_TYPE,
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".bmp": "image/bmp",
    }.get(suffix, "application/octet-stream")
