"""The vitals trend SVG.

The charts are rendered server-side, so they are testable as strings: the tests
below check the data actually reaches the geometry, and that the legibility
rules the chart was designed around hold.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import charts
from app.models import Consciousness, Observation

BASE = datetime(2026, 3, 11, 8, 0, tzinfo=timezone.utc)


def observation(minutes: int, score: int, risk: str = "low", **vitals) -> Observation:
    return Observation(
        case_record_id=1,
        recorded_at=BASE + timedelta(minutes=minutes),
        news2_score=score,
        risk_level=risk,
        consciousness=Consciousness.alert,
        recorded_by="Test Nurse",
        **vitals,
    )


# --------------------------------------------------------------------------
# Empty and degenerate cases
# --------------------------------------------------------------------------

def test_no_observations_gives_a_note_not_a_chart():
    trend = charts.vitals_trend([])
    assert not trend.has_chart
    assert "No scored observations" in trend.note


def test_unscored_observations_do_not_make_a_trend():
    trend = charts.vitals_trend([Observation(case_record_id=1, recorded_at=BASE, news2_score=None)])
    assert not trend.has_chart


def test_a_single_observation_renders_but_says_it_is_not_yet_a_trend():
    trend = charts.vitals_trend([observation(0, 3, pulse=88)])
    assert trend.has_chart
    assert trend.point_count == 1
    assert "second is needed" in trend.note
    # One point means a marker, never a line.
    assert "<polyline" not in trend.news2


def test_two_observations_draw_a_line():
    trend = charts.vitals_trend([observation(0, 2), observation(60, 5, risk="medium")])
    assert "<polyline" in trend.news2
    assert trend.note is None


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def test_x_scales_by_real_elapsed_time_not_observation_index():
    """A six-hour gap must look like a six-hour gap."""
    times = [BASE, BASE + timedelta(hours=1), BASE + timedelta(hours=7)]
    xs = charts._x_positions(times, 0.0, 100.0)
    assert xs[0] == pytest.approx(0.0)
    assert xs[2] == pytest.approx(100.0)
    # 1 hour of a 7-hour span is about a seventh across, not halfway.
    assert xs[1] == pytest.approx(100 / 7, abs=0.5)


def test_identical_timestamps_fall_back_to_even_spacing():
    xs = charts._x_positions([BASE, BASE, BASE], 0.0, 100.0)
    assert xs == pytest.approx([0.0, 50.0, 100.0])


def test_time_axis_labels_are_capped_so_they_cannot_collide():
    times = [BASE + timedelta(hours=i) for i in range(40)]
    xs = charts._x_positions(times, 0.0, 700.0)
    assert len(charts._time_labels(times, xs)) <= 5


def test_higher_scores_plot_higher_on_the_chart():
    svg = charts.news2_chart([observation(0, 1), observation(60, 9, risk="high")])
    ys = [float(pair.split(",")[1]) for pair in _polyline_points(svg)]
    assert ys[1] < ys[0], "a worse score must sit higher up (smaller y)"


def _polyline_points(svg: str) -> list[str]:
    marker = 'points="'
    start = svg.index(marker) + len(marker)
    return svg[start : svg.index('"', start)].split()


def test_the_y_axis_always_covers_the_escalation_thresholds():
    """Even an all-low patient shows where 5 and 7 sit, so the trend has context."""
    svg = charts.news2_chart([observation(0, 0), observation(30, 1)])
    assert "high 7+" in svg
    assert "medium 5–6" in svg
    assert "low 0–4" in svg


def test_the_scale_grows_to_fit_an_extreme_score():
    svg = charts.news2_chart([observation(0, 17, risk="high")])
    assert ">16<" in svg or ">18<" in svg, "axis ticks should extend past the worst score"


# --------------------------------------------------------------------------
# Legibility rules the design depends on
# --------------------------------------------------------------------------

def test_risk_bands_are_labelled_in_words_not_colour_alone():
    svg = charts.news2_chart([observation(0, 3)])
    for label in ("low 0–4", "medium 5–6", "high 7+"):
        assert label in svg


def test_only_the_latest_and_peak_points_are_directly_labelled():
    """A number on every point is unreadable; the table below carries the rest."""
    obs = [observation(i * 30, score) for i, score in enumerate([1, 2, 3, 4, 2])]
    svg = charts.news2_chart(obs)
    labels = svg.count('font-weight="700"')
    assert labels == 2, "expected the peak and the latest point only"


def test_a_single_point_is_labelled_once_when_it_is_both_peak_and_latest():
    svg = charts.news2_chart([observation(0, 4)])
    assert svg.count('font-weight="700"') == 1


def test_escalating_points_take_the_accent_colour_and_quiet_ones_do_not():
    calm = charts.news2_chart([observation(0, 1), observation(30, 2)])
    assert charts.CRITICAL not in calm

    escalating = charts.news2_chart([observation(0, 1), observation(30, 8, risk="high")])
    assert charts.CRITICAL in escalating


def test_gridlines_are_solid_hairlines():
    svg = charts.news2_chart([observation(0, 3), observation(30, 4)])
    assert "stroke-dasharray" not in svg, "dashed gridlines read as thresholds, not grid"


def test_every_point_has_a_hover_tooltip_with_its_own_reading():
    obs = [observation(0, 2), observation(45, 6, risk="medium")]
    svg = charts.news2_chart(obs)
    assert "11 Mar 08:00 — NEWS2 2 (low risk) · Test Nurse" in svg
    assert "11 Mar 08:45 — NEWS2 6 (medium risk) · Test Nurse" in svg


def test_the_chart_carries_a_text_description_for_screen_readers():
    svg = charts.news2_chart([observation(0, 6, risk="medium")])
    assert 'role="img"' in svg
    assert "<desc" in svg
    assert "listed in the observation table below" in svg


def test_recorded_by_is_escaped_so_a_name_cannot_inject_markup():
    obs = observation(0, 3)
    obs.recorded_by = '<script>alert("x")</script>'
    svg = charts.news2_chart([obs])
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# --------------------------------------------------------------------------
# Sparklines
# --------------------------------------------------------------------------

def test_a_sparkline_is_built_per_measure_that_has_data():
    obs = [
        observation(0, 2, respiratory_rate=18, spo2=97, pulse=80, systolic_bp=120, temperature_c=36.8),
        observation(60, 4, respiratory_rate=22, spo2=94, pulse=96, systolic_bp=112, temperature_c=38.1),
    ]
    trend = charts.vitals_trend(obs)
    labels = [s.label for s in trend.sparklines]
    assert labels == ["Respiratory rate", "SpO₂", "Pulse", "Systolic BP", "Temperature"]


def test_measures_with_no_readings_are_omitted_entirely():
    trend = charts.vitals_trend([observation(0, 2, pulse=80), observation(30, 3, pulse=90)])
    assert [s.label for s in trend.sparklines] == ["Pulse"]


def test_a_sparkline_reports_its_latest_value_and_range():
    obs = [observation(0, 1, pulse=72), observation(30, 2, pulse=110), observation(60, 2, pulse=88)]
    spark = charts.vitals_trend(obs).sparklines[0]
    assert spark.latest == "88"
    assert (spark.lowest, spark.highest) == ("72", "110")


def test_decimal_values_keep_their_precision_without_trailing_zeros():
    obs = [observation(0, 1, temperature_c=37.0), observation(30, 2, temperature_c=38.5)]
    spark = charts.vitals_trend(obs).sparklines[0]
    assert spark.latest == "38.5"
    assert spark.lowest == "37"


def test_a_flat_series_does_not_divide_by_zero():
    obs = [observation(0, 1, pulse=80), observation(30, 1, pulse=80)]
    spark = charts.vitals_trend(obs).sparklines[0]
    assert "<polyline" in spark.svg
    assert "nan" not in spark.svg.lower()


def test_sparklines_use_one_hue_rather_than_a_colour_per_measure():
    """They are separate facets, not a categorical set — one hue keeps them comparable."""
    obs = [
        observation(0, 2, respiratory_rate=18, spo2=97, pulse=80),
        observation(60, 3, respiratory_rate=20, spo2=95, pulse=90),
    ]
    for spark in charts.vitals_trend(obs).sparklines:
        assert charts.ACCENT in spark.svg


def test_risk_bands_tile_the_axis_without_gaps():
    """Scores 5 and 6 occupy y 5–7, so no score sits in an unpainted gap."""
    import re

    svg = charts.news2_chart([observation(0, 3), observation(30, 9, risk="high")])
    bands = re.findall(r'<rect x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', svg)
    spans = sorted((float(y), float(y) + float(h)) for y, h in bands)
    assert len(spans) == 3
    for (_, lower_end), (upper_start, _) in zip(spans, spans[1:]):
        assert lower_end == pytest.approx(upper_start, abs=0.2), "bands must meet edge to edge"


def test_all_three_risk_bands_are_labelled_even_when_narrow():
    svg = charts.news2_chart([observation(0, 2)])
    assert svg.count("medium 5–6") == 1
    assert svg.count("low 0–4") == 1
    assert svg.count("high 7+") == 1
