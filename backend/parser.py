"""Turn the free text of a prescription into structured, schedulable medicines.

This is the heart of the app: a caregiver photographs a prescription, OCR turns
it into text, and this module turns that text into medicines with dose slots so
the scheduler can raise alarms at the right moment.

It understands the shorthand doctors actually write -- ``1-0-1``, ``BD``,
``TDS``, ``HS``, ``q8h``, ``x 5 days``, ``after food`` -- and it reports a
confidence score per line so the UI can ask a human to confirm anything it is
unsure about. Nothing here ever silently guesses a dose: when the frequency is
missing the medicine is flagged rather than assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

# Slots are named times of day. The scheduler maps them onto real clock times
# using the patient's own routine (breakfast at 8, dinner at 20, and so on).
MORNING = "morning"
AFTERNOON = "afternoon"
EVENING = "evening"
NIGHT = "night"
BEDTIME = "bedtime"

SLOT_ORDER = [MORNING, AFTERNOON, EVENING, NIGHT, BEDTIME]

FOOD_ANY = "any"
FOOD_BEFORE = "before_food"
FOOD_AFTER = "after_food"
FOOD_WITH = "with_food"
FOOD_EMPTY = "empty_stomach"

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

FORM_WORDS: list[tuple[str, str, str]] = [
    # (regex, canonical form, default dose unit)
    (r"tabs?|tablets?|tblt", "tablet", "tablet"),
    (r"caps?|capsules?", "capsule", "capsule"),
    (r"syp|syr|syrup|susp|suspension|soln?|solution|elixir|liquid", "syrup", "ml"),
    (r"inj|injection|vial|amp|ampoule", "injection", "unit"),
    (r"drops?|gtt|eye\s*drops?|ear\s*drops?|nasal\s*drops?", "drops", "drop"),
    (r"inh|inhaler|puffs?|rotacaps?|respules?|nebuli[sz]er", "inhaler", "puff"),
    (r"oint|ointment|cream|gel|lotion|spray", "topical", "application"),
    (r"sachets?|powder|granules?", "sachet", "sachet"),
    (r"patch(?:es)?", "patch", "patch"),
    (r"supp|suppository", "suppository", "suppository"),
]

# A leading "T." / "C." / "S." is common Indian shorthand for Tab / Cap / Syrup.
SHORT_FORM_PREFIX = {"t": "tablet", "c": "capsule", "s": "syrup", "inj": "injection"}

STRENGTH_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mg/ml|mcg/ml|mg|mcg|µg|ug|gms?|g|ml|iu|units?|%)\b",
    re.I,
)

_NUM = r"(?:\d{1,2}(?:\.\d)?|\d\s*/\s*\d|½|¼|¾)"
DOSE_PATTERN_RE = re.compile(
    rf"(?<![\w./-])(?P<pattern>{_NUM}(?:\s*[-–—]\s*{_NUM}){{2,3}})(?![\w/-])"
)

# Frequency shorthand -> (slots, human label). ``None`` slots mean "decide later".
FREQUENCY_WORDS: list[tuple[str, list[str], str]] = [
    (r"\bq\.?i\.?d\.?\b|\bq\.?d\.?s\.?\b|\bfour\s+times\b|\b4\s*times\b",
     [MORNING, AFTERNOON, EVENING, NIGHT], "QID (four times a day)"),
    (r"\bt\.?d\.?s\.?\b|\bt\.?i\.?d\.?\b|\bthrice\b|\bthree\s+times\b|\b3\s*times\b",
     [MORNING, AFTERNOON, NIGHT], "TDS (three times a day)"),
    (r"\bb\.?d\.?\b|\bb\.?i\.?d\.?\b|\btwice\b|\btwo\s+times\b|\b2\s*times\b",
     [MORNING, NIGHT], "BD (twice a day)"),
    (r"\bh\.?s\.?\b|\bnocte\b|\bat\s+night\b|\bat\s+bed\s*time\b|\bbed\s*time\b",
     [BEDTIME], "HS (at bedtime)"),
    (r"\bo\.?m\.?\b|\bmane\b|\bin\s+the\s+morning\b|\bevery\s+morning\b",
     [MORNING], "OM (every morning)"),
    (r"\bo\.?d\.?\b|\bq\.?d\.?\b|\bonce\s+(?:a\s+)?daily\b|\bonce\s+a\s+day\b|\bone\s+time\s+a\s+day\b|\bdaily\b",
     [MORNING], "OD (once a day)"),
]

PRN_RE = re.compile(r"\bs\.?o\.?s\.?\b|\bp\.?r\.?n\.?\b|as\s+(?:and\s+when\s+)?(?:needed|required)|if\s+(?:needed|required)|when\s+required", re.I)

INTERVAL_RE = re.compile(
    r"\bq\.?\s*(?P<h1>\d{1,2})\s*h(?:rs?|ours?)?\b|\bevery\s+(?P<h2>\d{1,2})\s*(?:h|hr|hrs|hour|hours)\b|\b(?P<h3>\d{1,2})\s*(?:h|hr|hrs)ly\b|\b(?P<h4>\d{1,2})\s*hourly\b",
    re.I,
)

ALTERNATE_DAY_RE = re.compile(r"\balternate\s+days?\b|\be\.?o\.?d\.?\b|\bevery\s+other\s+day\b", re.I)
WEEKLY_RE = re.compile(r"\bonce\s+(?:a\s+)?week(?:ly)?\b|\bweekly\b|\bevery\s+week\b", re.I)

FOOD_RULES: list[tuple[str, str]] = [
    (r"empty\s+stomach|before\s+breakfast\s+on\s+empty|fasting", FOOD_EMPTY),
    (r"after\s+(?:food|meals?|breakfast|lunch|dinner|eating)|\bp\.?c\.?\b|\ba\s*/\s*f\b|\baf\b", FOOD_AFTER),
    (r"before\s+(?:food|meals?|breakfast|lunch|dinner|eating)|\ba\.?c\.?\b|\bb\s*/\s*f\b|\bbf\b", FOOD_BEFORE),
    (r"with\s+(?:food|meals?|milk|water)|during\s+meals?", FOOD_WITH),
]

DURATION_RULES: list[tuple[str, int]] = [
    (r"(?:x|for|next)\s*(\d{1,3})\s*(?:days?|d)\b", 1),
    (r"(\d{1,3})\s*(?:days?)\b", 1),
    (r"(\d{1,2})\s*/\s*7\b", 1),          # British shorthand: 5/7 == 5 days
    (r"(?:x|for)?\s*(\d{1,2})\s*(?:weeks?|wks?)\b", 7),
    (r"(\d{1,2})\s*/\s*52\b", 7),
    (r"(?:x|for)?\s*(\d{1,2})\s*(?:months?|mons?)\b", 30),
    (r"(\d{1,2})\s*/\s*12\b", 30),
]

ONGOING_RE = re.compile(r"\bcontinue\b|\bcontinuous(?:ly)?\b|\blong\s*term\b|\blife\s*long\b|\bregular(?:ly)?\b|\bdaily\s+for\s+life\b", re.I)

# Lines that are prescription furniture rather than medicines.
NOISE_RE = re.compile(
    r"^\s*(?:rx|r/|prescription|advice|advise|diagnosis|complaints?|investigations?|"
    r"follow[\s-]*up|review|signature|sign|reg\.?\s*no|regd|consultant|"
    r"dr\.?\s|hospital|clinic|nursing\s+home|medical\s+centre|center|"
    r"patient\b|name\s*[:.]|age\s*[:.]|sex\s*[:.]|gender|date\s*[:.]|address|phone|mobile|"
    r"mbbs|md\b|ms\b|dnb|diploma|weight\s*:|bp\s*:|pulse\s*:|temp\s*:)",
    re.I,
)

# Letterhead words can sit anywhere on a line ("City Care Hospital, MG Road").
LETTERHEAD_RE = re.compile(
    r"\b(?:hospital|clinic|nursing\s+home|medical\s+(?:centre|center|college)|"
    r"health\s+(?:centre|center)|polyclinic|pharmacy|laborator(?:y|ies)|"
    r"consultation|appointment|prescribed\s+by|signature)\b"
    r"|\bage\s*[:.]?\s*\d{1,3}\b|\bsex\s*[:.]|\bd\.?o\.?b\.?\b",
    re.I,
)

STOP_WORDS_IN_NAME = re.compile(
    r"^(?:tab|tabs|tablet|tablets|cap|caps|capsule|capsules|syp|syr|syrup|inj|injection|"
    r"od|bd|bid|tds|tid|qid|qds|hs|sos|prn|stat|po|iv|im|sc|oral|orally|"
    r"morning|night|noon|daily|x|for|after|before|with|empty|take|continue|and)$",
    re.I,
)

FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75}


@dataclass
class ParsedMedicine:
    """One medicine lifted off a prescription, ready to become alarms."""

    name: str
    form: str = "tablet"
    strength: str = ""
    dose_qty: float = 1.0
    dose_unit: str = "tablet"
    slots: list[str] = field(default_factory=list)
    slot_qty: dict[str, float] = field(default_factory=dict)
    interval_hours: int | None = None
    day_interval: int = 1          # 2 == alternate days, 7 == weekly
    frequency_label: str = ""
    food: str = FOOD_ANY
    duration_days: int | None = None   # None == ongoing course
    prn: bool = False
    instructions: str = ""
    confidence: float = 0.0
    raw_line: str = ""
    warnings: list[str] = field(default_factory=list)   # a human must check these
    notes: list[str] = field(default_factory=list)      # informational only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _to_number(token: str) -> float:
    """Read ``1``, ``0.5``, ``1/2`` or ``½`` as a number."""
    token = token.strip()
    if token in FRACTIONS:
        return FRACTIONS[token]
    if "/" in token:
        top, _, bottom = token.partition("/")
        try:
            return float(top.strip()) / float(bottom.strip())
        except (ValueError, ZeroDivisionError):
            return 1.0
    try:
        return float(token)
    except ValueError:
        return 1.0


def _fix_ocr_digits(line: str) -> str:
    """OCR reads ``1-0-1`` as ``1-O-l``. Repair letters sitting inside a dose pattern."""

    def repair(match: re.Match[str]) -> str:
        chunk = match.group(0)
        fixed = (chunk.replace("O", "0").replace("o", "0")
                      .replace("l", "1").replace("I", "1").replace("|", "1"))
        return fixed

    return re.sub(r"(?<![\w])[0-9OolI|]\s*[-–—]\s*[0-9OolI|](?:\s*[-–—]\s*[0-9OolI|]){1,2}(?![\w])",
                  repair, line)


def normalise_text(text: str) -> str:
    """Tidy OCR output without destroying the layout that separates medicines."""
    text = text.replace("–", "-").replace("—", "-")
    lines = []
    for line in text.splitlines():
        line = line.replace("\t", " ")
        line = re.sub(r"[•·*]+", " ", line)
        line = re.sub(r"\s{2,}", "  ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _looks_like_noise(line: str) -> bool:
    if NOISE_RE.search(line) or LETTERHEAD_RE.search(line):
        return True
    letters = re.findall(r"[A-Za-z]{3,}", line)
    if not letters:
        return True
    # A line of mostly punctuation or a stray page number is not a medicine.
    if len(line.strip()) < 4:
        return True
    return False


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def _extract_form(line: str) -> tuple[str, str, str]:
    """Return (form, dose_unit, line with the form word removed)."""
    for pattern, form, unit in FORM_WORDS:
        match = re.search(rf"(?<![A-Za-z])(?:{pattern})\.?(?![A-Za-z])", line, re.I)
        if match:
            cleaned = line[: match.start()] + " " + line[match.end():]
            return form, unit, cleaned
    match = re.match(r"^\s*([TCS])\s*[.\-)]\s+", line, re.I)
    if match:
        form = SHORT_FORM_PREFIX[match.group(1).lower()]
        unit = "ml" if form == "syrup" else form
        return form, unit, line[match.end():]
    return "tablet", "tablet", line


def _extract_strength(line: str) -> tuple[str, str]:
    match = STRENGTH_RE.search(line)
    if not match:
        return "", line
    value = match.group("value").rstrip("0").rstrip(".") if "." in match.group("value") else match.group("value")
    unit = match.group("unit").lower()
    unit = {"gms": "g", "gm": "g", "unit": "IU", "units": "IU", "iu": "IU", "µg": "mcg", "ug": "mcg"}.get(unit, unit)
    strength = f"{value} {unit}" if unit != "%" else f"{value}%"
    cleaned = line[: match.start()] + " " + line[match.end():]
    return strength, cleaned


def _extract_dose_pattern(line: str) -> tuple[dict[str, float] | None, str]:
    """Read ``1-0-1`` style patterns into per-slot quantities."""
    for match in DOSE_PATTERN_RE.finditer(line):
        raw = match.group("pattern")
        parts = [p.strip() for p in re.split(r"[-–—]", raw)]
        if not 3 <= len(parts) <= 4:
            continue
        # Guard against dates (12-05-2025) and strengths written with dashes.
        if any(len(p.replace(".", "").replace("/", "")) > 2 for p in parts):
            continue
        values = [_to_number(p) for p in parts]
        if all(v == 0 for v in values) or any(v > 6 for v in values):
            continue
        slot_names = [MORNING, AFTERNOON, NIGHT] if len(parts) == 3 else [MORNING, AFTERNOON, EVENING, NIGHT]
        qty = {slot: value for slot, value in zip(slot_names, values) if value > 0}
        if not qty:
            continue
        cleaned = line[: match.start()] + " " + line[match.end():]
        return qty, cleaned
    return None, line


def _extract_frequency(line: str) -> tuple[list[str], str, str]:
    for pattern, slots, label in FREQUENCY_WORDS:
        match = re.search(pattern, line, re.I)
        if match:
            cleaned = line[: match.start()] + " " + line[match.end():]
            return list(slots), label, cleaned
    return [], "", line


def _extract_interval(line: str) -> tuple[int | None, str]:
    match = INTERVAL_RE.search(line)
    if not match:
        return None, line
    hours = next((int(g) for g in match.groups() if g), None)
    if hours is None or not 1 <= hours <= 24:
        return None, line
    cleaned = line[: match.start()] + " " + line[match.end():]
    return hours, cleaned


def _extract_food(line: str) -> tuple[str, str]:
    for pattern, food in FOOD_RULES:
        match = re.search(pattern, line, re.I)
        if match:
            cleaned = line[: match.start()] + " " + line[match.end():]
            return food, cleaned
    return FOOD_ANY, line


def _extract_duration(line: str) -> tuple[int | None, bool, str]:
    """Return (days, ongoing, cleaned line)."""
    if ONGOING_RE.search(line):
        cleaned = ONGOING_RE.sub(" ", line)
        return None, True, cleaned
    for pattern, multiplier in DURATION_RULES:
        match = re.search(pattern, line, re.I)
        if match:
            count = int(match.group(1))
            if count == 0 or count > 400:
                continue
            cleaned = line[: match.start()] + " " + line[match.end():]
            return count * multiplier, False, cleaned
    return None, False, line


def _extract_name(line: str) -> str:
    """Whatever survives the other extractors, up to the first non-name token."""
    line = re.sub(r"^\s*\d{1,2}\s*[).\-:]\s*", " ", line)   # serial number
    line = re.sub(r"[(){}\[\]]", " ", line)
    tokens = [t for t in re.split(r"\s+", line.strip()) if t]
    picked: list[str] = []
    for token in tokens:
        bare = token.strip(".,;:-")
        if not bare:
            continue
        if STOP_WORDS_IN_NAME.match(bare):
            if picked:
                break
            continue
        if re.fullmatch(r"[\d.]+", bare):
            if picked:
                break
            continue
        if not re.search(r"[A-Za-z]", bare):
            if picked:
                break
            continue
        picked.append(bare)
        if len(picked) >= 4:
            break
    name = " ".join(picked).strip(" -+")
    name = re.sub(r"\s{2,}", " ", name)
    return name


def _titleise(name: str) -> str:
    words = []
    for word in name.split():
        if word.isupper() and len(word) <= 4:
            words.append(word)           # keep acronyms such as ORS, HCQ
        else:
            words.append(word.capitalize())
    return " ".join(words)


# ---------------------------------------------------------------------------
# Line parsing
# ---------------------------------------------------------------------------

def parse_line(line: str) -> ParsedMedicine | None:
    """Parse a single prescription line. Returns ``None`` if it is not a medicine."""
    raw_line = line.strip()
    if not raw_line or _looks_like_noise(raw_line):
        return None

    working = _fix_ocr_digits(raw_line)
    working = re.sub(r"^\s*\(?\d{1,2}\s*[).\-:]\s+", " ", working).lstrip()

    form, dose_unit, working = _extract_form(working)
    strength, working = _extract_strength(working)
    duration_days, ongoing, working = _extract_duration(working)
    slot_qty, working = _extract_dose_pattern(working)
    interval_hours, working = _extract_interval(working)
    slots, freq_label, working = _extract_frequency(working)
    food, working = _extract_food(working)

    prn = bool(PRN_RE.search(working))
    if prn:
        working = PRN_RE.sub(" ", working)

    day_interval = 1
    day_label = ""
    if ALTERNATE_DAY_RE.search(working):
        day_interval = 2
        working = ALTERNATE_DAY_RE.sub(" ", working)
        day_label = "every alternate day"
    elif WEEKLY_RE.search(working):
        day_interval = 7
        working = WEEKLY_RE.sub(" ", working)
        day_label = "once a week"

    name = _extract_name(working)
    if len(name.replace(" ", "")) < 3:
        return None

    med = ParsedMedicine(
        name=_titleise(name),
        form=form,
        strength=strength,
        dose_unit=dose_unit,
        raw_line=raw_line,
        food=food,
        prn=prn,
        day_interval=day_interval,
        duration_days=duration_days,
        interval_hours=interval_hours,
    )

    # A written pattern (1-0-1) is more specific than an abbreviation, so it wins.
    if slot_qty:
        med.slots = [s for s in SLOT_ORDER if s in slot_qty]
        med.slot_qty = slot_qty
        med.dose_qty = max(slot_qty.values())
        med.frequency_label = freq_label or _pattern_label(slot_qty)
    elif interval_hours:
        med.slots = []
        med.frequency_label = f"Every {interval_hours} hours"
    elif slots:
        med.slots = slots
        med.frequency_label = freq_label
    elif prn:
        med.slots = []
        med.frequency_label = "SOS (only when needed)"
    elif day_label:
        # "Once a week" / "alternate day" already says how often; take it in the morning.
        med.slots = [MORNING]
        med.frequency_label = day_label.capitalize()
    else:
        med.slots = [MORNING]
        med.frequency_label = "Once a day (assumed)"
        med.warnings.append(
            "No frequency was written on this line. Assumed once a day - please confirm."
        )

    if day_label and day_label.capitalize() != med.frequency_label:
        med.frequency_label = f"{med.frequency_label}, {day_label}"

    # Liquids and injections carry their dose in the strength (5 ml, 10 IU).
    if not slot_qty and med.form in {"syrup", "injection", "drops"} and strength:
        value_match = re.match(r"([\d.]+)\s*(ml|IU|drop)", strength, re.I)
        if value_match:
            med.dose_qty = float(value_match.group(1))
            med.strength = ""

    if not med.slot_qty and med.slots:
        med.slot_qty = {slot: med.dose_qty for slot in med.slots}

    if ongoing:
        med.duration_days = None
        med.instructions = "Ongoing / long-term medicine"

    med.confidence = _score(med, strength, slot_qty, slots, interval_hours, prn)
    if med.prn:
        med.notes.append("Taken only when needed - no fixed alarm will ring.")
    if not med.strength and med.form in {"tablet", "capsule"}:
        med.warnings.append("Strength was not detected - please check the dose.")
    return med


def _pattern_label(slot_qty: dict[str, float]) -> str:
    order = [MORNING, AFTERNOON, EVENING, NIGHT]
    parts = [_fmt_qty(slot_qty.get(slot, 0)) for slot in order if slot in slot_qty or slot != EVENING]
    return "-".join(parts)


def _fmt_qty(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _score(med: ParsedMedicine, strength: str, slot_qty, slots, interval_hours, prn: bool) -> float:
    score = 0.35
    if len(med.name) >= 4:
        score += 0.20
    if strength:
        score += 0.15
    if med.form != "tablet" or re.search(r"tab|cap|syp|inj", med.raw_line, re.I):
        score += 0.10
    if slot_qty or slots or interval_hours or prn or med.day_interval > 1:
        score += 0.25
    else:
        score -= 0.10
    if med.duration_days:
        score += 0.05
    return round(max(0.05, min(score, 1.0)), 2)


def parse_prescription(text: str) -> dict[str, Any]:
    """Parse a whole prescription. Returns medicines plus any overall warnings."""
    cleaned = normalise_text(text or "")
    medicines: list[ParsedMedicine] = []
    skipped: list[str] = []

    for line in cleaned.splitlines():
        # Some prescriptions put two medicines on one line separated by a big gap.
        segments = [line]
        med_found = False
        for segment in segments:
            parsed = parse_line(segment)
            if parsed:
                medicines.append(parsed)
                med_found = True
        if not med_found and line.strip() and not NOISE_RE.search(line):
            skipped.append(line.strip())

    medicines = _deduplicate(medicines)

    warnings: list[str] = []
    if not medicines:
        warnings.append(
            "No medicines could be read from this prescription. "
            "Please type them in by hand, or upload a clearer photo."
        )
    low_confidence = [m.name for m in medicines if m.confidence < 0.6]
    if low_confidence:
        warnings.append(
            "Please double-check these before saving: " + ", ".join(low_confidence)
        )

    return {
        "medicines": [m.to_dict() for m in medicines],
        "warnings": warnings,
        "skipped_lines": skipped,
        "clean_text": cleaned,
    }


def _deduplicate(medicines: list[ParsedMedicine]) -> list[ParsedMedicine]:
    seen: dict[str, ParsedMedicine] = {}
    ordered: list[ParsedMedicine] = []
    for med in medicines:
        key = f"{med.name.lower()}|{med.strength.lower()}"
        existing = seen.get(key)
        if existing is None:
            seen[key] = med
            ordered.append(med)
        elif med.confidence > existing.confidence:
            ordered[ordered.index(existing)] = med
            seen[key] = med
    return ordered
