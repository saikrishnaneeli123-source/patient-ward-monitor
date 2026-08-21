"""Read the text off a photographed or scanned prescription.

Tesseract does the reading. Because a phone photo of a paper prescription is
usually poorly lit and slightly skewed, the image is upscaled, greyscaled and
contrast-stretched first -- that alone lifts recognition of handwritten-style
print noticeably.

If Tesseract is not installed the app does not break: ``ocr_available()`` returns
False and the UI falls back to typing the prescription in, which drives exactly
the same parser and scheduler.
"""

from __future__ import annotations

import io
import shutil
from typing import Any

try:  # Pillow and pytesseract are optional at import time.
    from PIL import Image, ImageEnhance, ImageOps, ImageFilter
    import pytesseract
    _IMAGING = True
except ImportError:  # pragma: no cover - only hit on a bare install
    _IMAGING = False

MAX_DIMENSION = 2200
MIN_DIMENSION = 1000


def ocr_available() -> bool:
    """True when a real OCR engine is installed on this machine."""
    if not _IMAGING:
        return False
    try:
        return shutil.which("tesseract") is not None or bool(pytesseract.get_tesseract_version())
    except Exception:
        return False


def preprocess(image: "Image.Image") -> "Image.Image":
    """Make a phone photo legible to Tesseract."""
    image = ImageOps.exif_transpose(image)
    if image.mode != "L":
        image = image.convert("L")

    width, height = image.size
    longest = max(width, height)
    if longest > MAX_DIMENSION:
        scale = MAX_DIMENSION / longest
    elif longest < MIN_DIMENSION:
        scale = MIN_DIMENSION / longest        # small photos OCR badly; upscale
    else:
        scale = 1.0
    if scale != 1.0:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                             Image.LANCZOS)

    image = ImageOps.autocontrast(image, cutoff=2)
    image = ImageEnhance.Sharpness(image).enhance(1.8)
    image = image.filter(ImageFilter.MedianFilter(size=3))
    return image


def image_to_text(data: bytes) -> dict[str, Any]:
    """Extract text from image bytes. Never raises for a bad upload."""
    if not _IMAGING:
        return {"text": "", "ok": False,
                "error": "Image reading is not installed on the server. Please type the prescription instead."}
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        return {"text": "", "ok": False,
                "error": "That file could not be opened as an image. Please upload a JPG or PNG photo."}

    if not ocr_available():
        return {"text": "", "ok": False,
                "error": "No OCR engine is installed on the server (install 'tesseract-ocr'). "
                         "Please type the prescription instead."}

    prepared = preprocess(image)
    try:
        # PSM 6: treat the page as a single uniform block -- prescriptions are
        # one column of lines, and this keeps each medicine on its own line.
        text = pytesseract.image_to_string(prepared, config="--oem 3 --psm 6")
    except Exception as exc:  # pragma: no cover - engine level failure
        return {"text": "", "ok": False, "error": f"Could not read the image: {exc}"}

    text = text.strip()
    if not text:
        return {"text": "", "ok": False,
                "error": "No text could be read from that photo. Try again in better light, "
                         "holding the phone straight above the paper."}
    return {"text": text, "ok": True, "error": ""}
