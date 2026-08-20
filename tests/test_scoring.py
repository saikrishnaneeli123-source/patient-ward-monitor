"""NEWS2 scoring, checked against the Royal College of Physicians chart."""
import pytest

from app.scoring import calculate_news2

HEALTHY = dict(
    respiratory_rate=16, spo2=98, on_oxygen=False, systolic_bp=120,
    pulse=70, temperature_c=36.8, consciousness="alert",
)


def test_normal_observations_score_zero():
    result = calculate_news2(**HEALTHY)
    assert result.score == 0
    assert result.risk == "low"
    assert not result.is_partial


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("respiratory_rate", 8, 3), ("respiratory_rate", 10, 1), ("respiratory_rate", 20, 0),
        ("respiratory_rate", 22, 2), ("respiratory_rate", 25, 3),
        ("spo2", 91, 3), ("spo2", 92, 2), ("spo2", 94, 1), ("spo2", 96, 0),
        ("systolic_bp", 90, 3), ("systolic_bp", 100, 2), ("systolic_bp", 110, 1),
        ("systolic_bp", 180, 0), ("systolic_bp", 220, 3),
        ("pulse", 40, 3), ("pulse", 50, 1), ("pulse", 90, 0), ("pulse", 110, 1),
        ("pulse", 130, 2), ("pulse", 131, 3),
        ("temperature_c", 35.0, 3), ("temperature_c", 36.0, 1), ("temperature_c", 37.0, 0),
        ("temperature_c", 38.5, 1), ("temperature_c", 39.5, 2),
    ],
)
def test_individual_parameter_bands(field, value, expected):
    result = calculate_news2(**{**HEALTHY, field: value})
    assert result.breakdown[field] == expected
    assert result.score == expected


def test_supplemental_oxygen_scores_two():
    result = calculate_news2(**{**HEALTHY, "on_oxygen": True})
    assert result.breakdown["oxygen"] == 2
    assert result.score == 2


def test_non_alert_consciousness_scores_three():
    result = calculate_news2(**{**HEALTHY, "consciousness": "voice"})
    assert result.breakdown["consciousness"] == 3
    assert result.risk == "low-medium"  # single parameter scoring 3
    assert result.triggered_by_single_parameter


def test_risk_bands():
    assert calculate_news2(**{**HEALTHY, "pulse": 95}).risk == "low"  # 1
    # 2 (RR) + 1 (SpO2) + 2 (O2) = 5 -> medium
    medium = calculate_news2(**{**HEALTHY, "respiratory_rate": 22, "spo2": 95, "on_oxygen": True})
    assert medium.score == 5 and medium.risk == "medium"
    # 3 (RR) + 3 (SpO2) + 2 (O2) = 8 -> high
    high = calculate_news2(**{**HEALTHY, "respiratory_rate": 26, "spo2": 88, "on_oxygen": True})
    assert high.score == 8 and high.risk == "high"


def test_single_parameter_three_escalates_a_low_total():
    result = calculate_news2(**{**HEALTHY, "pulse": 38})
    assert result.score == 3
    assert result.risk == "low-medium"


def test_missing_parameters_are_reported_not_guessed():
    result = calculate_news2(respiratory_rate=18, pulse=80, consciousness="alert")
    assert set(result.missing) == {"spo2", "systolic_bp", "temperature_c"}
    assert result.is_partial
    assert result.as_dict()["partial"] is True


def test_spo2_scale_two_scores_high_saturation_on_oxygen():
    """On scale 2, over-oxygenation is itself dangerous and must score."""
    on_air = calculate_news2(**{**HEALTHY, "spo2": 90, "spo2_scale": 2})
    assert on_air.breakdown["spo2"] == 0

    over_oxygenated = calculate_news2(**{**HEALTHY, "spo2": 98, "spo2_scale": 2, "on_oxygen": True})
    assert over_oxygenated.breakdown["spo2"] == 3


# --------------------------------------------------------------------------
# Allergy display safety
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entries,expected",
    [
        (["Penicillin (rash)"], ["Penicillin (rash)"]),
        (["None known"], []),
        (["NKDA"], []),
        (["nil known."], []),
        (["N/A"], []),
        ([], []),
        (None, []),
        (["None known", "Sulfa"], ["Sulfa"]),
    ],
)
def test_only_genuine_allergies_are_flagged(entries, expected):
    """'None known' must not render as a red allergy warning on the board."""
    from app.services import real_allergies

    assert real_allergies(entries) == expected
