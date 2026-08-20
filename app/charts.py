"""Server-rendered inline SVG for the vitals trend.

Rendered on the server, with no client-side charting library: a ward PC should
show the trend on a slow or offline network, and a hospital CSP should not need
to allow a CDN.

Design decisions worth stating, because they are legibility decisions:

* **Risk lives in the labelled background bands, not in four marker hues.** The
  four NEWS2 risk colours cannot all be told apart as small marks on a white
  card — ``warning`` and ``serious`` measure ΔE 13.6 apart and both sit under
  3:1 contrast. So the bands carry risk (each labelled in words), the line is a
  single ink stroke, and only genuinely escalating points take an accent colour
  plus a printed score. Colour never carries meaning on its own.
* **One y-axis per chart.** The vitals are separate small multiples rather than
  extra lines on the NEWS2 axis; overlaying measures with different units on one
  scale invents correlations that are not in the data.
* **x is real elapsed time**, so a six-hour gap looks like a six-hour gap.
* Every value plotted is also in the observation table below the chart, so no
  number is reachable only by hovering.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime

from markupsafe import Markup

# Ink and chrome, matching the app surface (#ffffff cards).
INK = "#16202c"
SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#ffffff"
ACCENT = "#2a78d6"   # single hue for the vitals small multiples
CRITICAL = "#d03b3b"  # reserved: only for points that are escalating

# Background band tints, kept faint so the data line stays dominant.
BAND_LOW = "#f2faf2"
BAND_MEDIUM = "#fdf3ec"
BAND_HIGH = "#fdf0f0"


def _fmt(value: float) -> str:
    """Trim trailing zeros so 37.0 renders as 37 and 37.5 stays 37.5."""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text or "0"


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


@dataclass
class Sparkline:
    label: str
    unit: str
    latest: str
    svg: Markup
    lowest: str = ""
    highest: str = ""


@dataclass
class Trend:
    """Everything the case page needs to draw the trend section."""

    news2: Markup | None = None
    sparklines: list[Sparkline] = field(default_factory=list)
    point_count: int = 0
    note: str | None = None

    @property
    def has_chart(self) -> bool:
        return self.news2 is not None


def _x_positions(times: list[datetime], left: float, right: float) -> list[float]:
    """Scale timestamps across the plot. Falls back to even spacing."""
    span = (times[-1] - times[0]).total_seconds()
    if len(times) == 1:
        return [right]
    if span <= 0:
        step = (right - left) / (len(times) - 1)
        return [left + i * step for i in range(len(times))]
    return [left + (right - left) * ((t - times[0]).total_seconds() / span) for t in times]


def _time_labels(times: list[datetime], xs: list[float], max_labels: int = 5) -> list[tuple[float, str]]:
    """Label at most ``max_labels`` ticks so they cannot collide."""
    count = len(times)
    if count <= max_labels:
        indices = range(count)
    else:
        step = (count - 1) / (max_labels - 1)
        indices = sorted({round(i * step) for i in range(max_labels)})
    return [(xs[i], times[i].strftime("%d %b\n%H:%M")) for i in indices]


def news2_chart(observations: list) -> Markup | None:
    """Line chart of NEWS2 over time against labelled risk bands."""
    points = [o for o in observations if o.news2_score is not None]
    if not points:
        return None

    width, height = 720.0, 250.0
    left, right = 44.0, 690.0
    top, bottom = 18.0, 196.0  # below `bottom` is the x-axis band

    times = [o.recorded_at for o in points]
    scores = [o.news2_score for o in points]
    y_max = max(8, max(scores) + 1)
    xs = _x_positions(times, left, right)

    def y_of(score: float) -> float:
        return bottom - (bottom - top) * (score / y_max)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" class="trend-svg" '
        f'aria-labelledby="news2-title news2-desc">',
        '<title id="news2-title">NEWS2 score over time</title>',
        f'<desc id="news2-desc">{len(points)} observations. Latest score '
        f'{scores[-1]}, {_esc(points[-1].risk_level or "unscored")} risk. Every value is '
        f'listed in the observation table below.</desc>',
    ]

    # Risk bands, painted first so everything else sits above them.
    # Bands are half-open on the y-axis: scores 5 and 6 occupy the range 5 to 7,
    # so the thresholds meet edge to edge with no unpainted gap between them.
    for lo, hi, fill, label in (
        (0, 5, BAND_LOW, "low 0–4"),
        (5, 7, BAND_MEDIUM, "medium 5–6"),
        (7, y_max, BAND_HIGH, "high 7+"),
    ):
        if lo > y_max:
            continue
        band_top, band_bottom = y_of(min(hi, y_max)), y_of(lo)
        parts.append(
            f'<rect x="{left}" y="{band_top:.1f}" width="{right - left:.1f}" '
            f'height="{max(band_bottom - band_top, 0):.1f}" fill="{fill}"/>'
        )
        if band_bottom - band_top >= 14:
            parts.append(
                f'<text x="{right - 6}" y="{band_top + 12:.1f}" text-anchor="end" '
                f'font-size="10" fill="{MUTED}">{label}</text>'
            )

    # Solid hairline gridlines and y ticks — never dashed.
    for score in range(0, y_max + 1, 2):
        y = y_of(score)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="{MUTED}" style="font-variant-numeric: tabular-nums">{score}</text>'
        )

    parts.append(
        f'<line x1="{left}" y1="{bottom:.1f}" x2="{right}" y2="{bottom:.1f}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )

    # The data line.
    ys = [y_of(s) for s in scores]
    if len(points) > 1:
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        parts.append(
            f'<polyline points="{polyline}" fill="none" stroke="{INK}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )

    # Markers. Escalating observations take the accent and print their score;
    # everything else stays quiet so the exceptions are what the eye lands on.
    peak_index = scores.index(max(scores))
    for i, (obs, x, y) in enumerate(zip(points, xs, ys)):
        escalating = (obs.risk_level in ("medium", "high")) or scores[i] >= 5
        fill = CRITICAL if escalating else SURFACE
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{5 if escalating else 4}" fill="{fill}" '
            f'stroke="{SURFACE if escalating else INK}" stroke-width="2"/>'
        )
        # Direct-label only the latest and the peak; the axis and table carry the rest.
        if i in (len(points) - 1, peak_index):
            parts.append(
                f'<text x="{x:.1f}" y="{y - 12:.1f}" text-anchor="middle" font-size="12" '
                f'font-weight="700" fill="{INK}" '
                f'style="font-variant-numeric: tabular-nums">{scores[i]}</text>'
            )
        # A generous invisible hit target for the native hover tooltip.
        stamp = obs.recorded_at.strftime("%d %b %H:%M")
        risk = obs.risk_level or "unscored"
        by = f" · {obs.recorded_by}" if obs.recorded_by else ""
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12" fill="transparent">'
            f"<title>{_esc(stamp)} — NEWS2 {scores[i]} ({_esc(risk)} risk){_esc(by)}</title></circle>"
        )

    for x, label in _time_labels(times, xs):
        first, second = label.split("\n")
        parts.append(
            f'<text x="{x:.1f}" y="{bottom + 18:.1f}" text-anchor="middle" font-size="11" '
            f'fill="{MUTED}">{first}</text>'
            f'<text x="{x:.1f}" y="{bottom + 32:.1f}" text-anchor="middle" font-size="11" '
            f'fill="{MUTED}" style="font-variant-numeric: tabular-nums">{second}</text>'
        )

    parts.append("</svg>")
    return Markup("".join(parts))


def sparkline(observations: list, attr: str, label: str, unit: str) -> Sparkline | None:
    """One small multiple: a single measure over the same time span."""
    pairs = [(o.recorded_at, getattr(o, attr)) for o in observations if getattr(o, attr) is not None]
    if not pairs:
        return None

    width, height = 240.0, 64.0
    left, right, top, bottom = 4.0, 232.0, 10.0, 52.0
    times = [t for t, _ in pairs]
    values = [float(v) for _, v in pairs]
    lo, hi = min(values), max(values)
    # A flat series should sit on the middle of the band, not on an edge.
    pad = (hi - lo) * 0.15 or max(abs(hi) * 0.05, 1.0)
    lo, hi = lo - pad, hi + pad

    xs = _x_positions(times, left, right)
    ys = [bottom - (bottom - top) * ((v - lo) / (hi - lo)) for v in values]

    parts = [
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" class="spark-svg">',
        f"<title>{_esc(label)} over time, latest {_fmt(values[-1])} {_esc(unit)}</title>",
    ]
    if len(pairs) > 1:
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        parts.append(
            f'<polyline points="{polyline}" fill="none" stroke="{ACCENT}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
    parts.append(
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="4" fill="{ACCENT}" '
        f'stroke="{SURFACE}" stroke-width="2"/>'
    )
    parts.append("</svg>")

    return Sparkline(
        label=label,
        unit=unit,
        latest=_fmt(values[-1]),
        svg=Markup("".join(parts)),
        lowest=_fmt(min(values)),
        highest=_fmt(max(values)),
    )


SPARK_SERIES = [
    ("respiratory_rate", "Respiratory rate", "/min"),
    ("spo2", "SpO₂", "%"),
    ("pulse", "Pulse", "bpm"),
    ("systolic_bp", "Systolic BP", "mmHg"),
    ("temperature_c", "Temperature", "°C"),
]


def vitals_trend(observations: list) -> Trend:
    """Build the whole trend section from observations in chronological order."""
    scored = [o for o in observations if o.news2_score is not None]
    if not scored:
        return Trend(note="No scored observations yet — record a set to start the trend.")

    trend = Trend(
        news2=news2_chart(observations),
        sparklines=[
            s for s in (sparkline(observations, attr, label, unit) for attr, label, unit in SPARK_SERIES)
            if s is not None
        ],
        point_count=len(scored),
    )
    if len(scored) == 1:
        trend.note = "One observation so far — a second is needed before a trend means anything."
    return trend
