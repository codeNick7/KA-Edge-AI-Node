"""Feature engineering layer (PRD Section 4 / architecture doc Section 8).

Converts raw sensor values into soil-type-agnostic relative indices before
anything touches the validity gate or decision tree, per PRD Section 4.
"""

from .texture_table import get_texture_bounds


def compute_rsmi(moisture_pct: float, texture: str | None = None) -> float:
    """Relative Soil Moisture Index = (moisture - WP) / (FC - WP), clipped [0,1].

    PRD Section 4. Texture defaults to loam via texture_table.get_texture_bounds
    when unset/unknown.
    """
    bounds = get_texture_bounds(texture)
    wilting_point = bounds["wilting_point"]
    field_capacity = bounds["field_capacity"]
    span = field_capacity - wilting_point
    if span == 0:
        raise ValueError("field_capacity must differ from wilting_point")
    rsmi = (moisture_pct - wilting_point) / span
    return max(0.0, min(1.0, rsmi))


def gas_index(bsec_output: float) -> float:
    """Passthrough for BME688's on-chip BSEC gas/IAQ index.

    PRD Section 4: BSEC's pretrained output is the right level of
    abstraction here — do not re-derive it from raw resistance values.
    """
    return float(bsec_output)


def compute_light_delta(readings: list[float]) -> float:
    """Rate of change of light readings (confound-corrector, not absolute lux).

    Returns the difference between the two most recent readings. With fewer
    than 2 readings there is no rate to compute, so this returns 0.0 rather
    than raising — a cold-start pot has no light trend yet, not a fault.
    """
    if len(readings) < 2:
        return 0.0
    return readings[-1] - readings[-2]


def compute_trend_slope(readings: list[float]) -> float:
    """Simple linear-regression slope over a window of readings.

    Used for the decision tree's 7-reading gas_index_trend / RSMI_trend
    checks (PRD Section 6). Index position is the x-axis; fewer than 2
    readings yields a slope of 0.0 (no trend yet, not a fault).
    """
    n = len(readings)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(readings) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, readings))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return numerator / denominator
