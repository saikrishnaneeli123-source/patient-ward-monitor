"""NEWS2 (National Early Warning Score 2) calculation.

Implements the Royal College of Physicians NEWS2 chart. This is decision
*support* only: it flags deterioration for a human to act on, it does not make
clinical decisions. See DISCLAIMER in the README.

Any parameter that was not recorded is skipped and reported in ``missing`` —
a partial score is always an underestimate, so the UI shows it as partial
rather than pretending the total is complete.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LOW = "low"
LOW_MEDIUM = "low-medium"
MEDIUM = "medium"
HIGH = "high"

RESPONSE = {
    LOW: "Routine observations, minimum 12-hourly.",
    LOW_MEDIUM: "Urgent review by a competent clinician; minimum hourly observations.",
    MEDIUM: "Urgent review by a ward-based doctor; minimum hourly observations.",
    HIGH: "Emergency assessment by a critical care capable team; continuous monitoring.",
}


def _band(value: float, bands: list[tuple[float | None, float | None, int]]) -> int:
    """Return the score for the first band whose (low, high) range contains value."""
    for low, high, score in bands:
        if (low is None or value >= low) and (high is None or value <= high):
            return score
    return 0


RESP_RATE_BANDS = [(None, 8, 3), (9, 11, 1), (12, 20, 0), (21, 24, 2), (25, None, 3)]
SPO2_SCALE1_BANDS = [(None, 91, 3), (92, 93, 2), (94, 95, 1), (96, None, 0)]
SYSTOLIC_BANDS = [(None, 90, 3), (91, 100, 2), (101, 110, 1), (111, 219, 0), (220, None, 3)]
PULSE_BANDS = [(None, 40, 3), (41, 50, 1), (51, 90, 0), (91, 110, 1), (111, 130, 2), (131, None, 3)]
TEMP_BANDS = [(None, 35.0, 3), (35.1, 36.0, 1), (36.1, 38.0, 0), (38.1, 39.0, 1), (39.1, None, 2)]


def _spo2_scale2(spo2: int, on_oxygen: bool) -> int:
    """SpO2 Scale 2 — for patients with hypercapnic respiratory failure.

    On scale 2 a *high* saturation on oxygen also scores, because it risks
    suppressing hypoxic respiratory drive.
    """
    if spo2 <= 83:
        return 3
    if spo2 <= 85:
        return 2
    if spo2 <= 87:
        return 1
    if not on_oxygen:
        return 0  # 88% or above breathing air is the target range
    if spo2 <= 92:
        return 0
    if spo2 <= 94:
        return 1
    if spo2 <= 96:
        return 2
    return 3


@dataclass
class News2Result:
    score: int
    risk: str
    response: str
    breakdown: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    triggered_by_single_parameter: bool = False

    @property
    def is_partial(self) -> bool:
        return bool(self.missing)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "risk": self.risk,
            "response": self.response,
            "breakdown": self.breakdown,
            "missing": self.missing,
            "partial": self.is_partial,
            "single_parameter_3": self.triggered_by_single_parameter,
        }


def calculate_news2(
    *,
    respiratory_rate: int | None = None,
    spo2: int | None = None,
    on_oxygen: bool = False,
    spo2_scale: int = 1,
    systolic_bp: int | None = None,
    pulse: int | None = None,
    temperature_c: float | None = None,
    consciousness: str | None = "alert",
) -> News2Result:
    breakdown: dict[str, int] = {}
    missing: list[str] = []

    if respiratory_rate is None:
        missing.append("respiratory_rate")
    else:
        breakdown["respiratory_rate"] = _band(respiratory_rate, RESP_RATE_BANDS)

    if spo2 is None:
        missing.append("spo2")
    elif spo2_scale == 2:
        breakdown["spo2"] = _spo2_scale2(spo2, on_oxygen)
    else:
        breakdown["spo2"] = _band(spo2, SPO2_SCALE1_BANDS)

    # Supplemental oxygen is itself a scoring parameter (2 points).
    breakdown["oxygen"] = 2 if on_oxygen else 0

    if systolic_bp is None:
        missing.append("systolic_bp")
    else:
        breakdown["systolic_bp"] = _band(systolic_bp, SYSTOLIC_BANDS)

    if pulse is None:
        missing.append("pulse")
    else:
        breakdown["pulse"] = _band(pulse, PULSE_BANDS)

    if temperature_c is None:
        missing.append("temperature_c")
    else:
        breakdown["temperature_c"] = _band(temperature_c, TEMP_BANDS)

    # ACVPU: anything other than Alert scores 3.
    if consciousness is None:
        missing.append("consciousness")
    else:
        breakdown["consciousness"] = 0 if str(consciousness).lower() == "alert" else 3

    total = sum(breakdown.values())
    single_param_3 = any(v == 3 for v in breakdown.values())

    if total >= 7:
        risk = HIGH
    elif total >= 5:
        risk = MEDIUM
    elif single_param_3:
        risk = LOW_MEDIUM
    else:
        risk = LOW

    return News2Result(
        score=total,
        risk=risk,
        response=RESPONSE[risk],
        breakdown=breakdown,
        missing=missing,
        triggered_by_single_parameter=single_param_3,
    )


def alerts_for(result: News2Result) -> list[tuple[str, str]]:
    """Map a NEWS2 result onto (severity, message) pairs for the alert feed."""
    alerts: list[tuple[str, str]] = []
    if result.risk == HIGH:
        alerts.append(("critical", f"NEWS2 {result.score} — high risk. {RESPONSE[HIGH]}"))
    elif result.risk == MEDIUM:
        alerts.append(("warning", f"NEWS2 {result.score} — medium risk. {RESPONSE[MEDIUM]}"))
    elif result.risk == LOW_MEDIUM:
        params = [k for k, v in result.breakdown.items() if v == 3]
        alerts.append(
            ("warning", f"Single parameter scoring 3 ({', '.join(params)}). {RESPONSE[LOW_MEDIUM]}")
        )
    if result.is_partial and result.risk != LOW:
        alerts.append(
            ("info", f"Score is partial — not recorded: {', '.join(result.missing)}. True score may be higher.")
        )
    return alerts
