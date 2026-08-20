"""Validity gate — sensor fault detection (PRD Section 6, architecture doc Section 11).

Fault-tier detection: fast, deterministic, no model needed. Runs before any
trend/decision-tree logic, per PRD Section 10 build-plan step 4.

Bounds are engineering placeholders (no published spec for "physically
plausible sensor range" at this price point). MAX_MOISTURE_JUMP_PCT is
grounded in Dominguez-Nino et al. (2020, Sensors 20(18):5100, DOI
10.3390/s20185100), which measured capacitive soil moisture sensor
coefficient-of-variation at 10-16% under constant conditions — see
docs/soil-health-node-citations.md item 5. The threshold is set clearly
above that measured noise ceiling so the gate doesn't fire on normal
sensor noise; refine against real trial data (execution plan, 12-16 Aug)
once available.
"""

from dataclasses import dataclass

MOISTURE_BOUNDS = (0.0, 100.0)          # percent, physical range of a capacitive sensor
GAS_INDEX_BOUNDS = (0.0, 500.0)         # BSEC IAQ index typical output range
TEMP_BOUNDS = (-10.0, 60.0)             # deg C, realistic indoor/greenhouse range

MAX_MOISTURE_JUMP_PCT = 25.0            # comfortably above the ~16% CV noise ceiling (citation above)


@dataclass
class ValidityResult:
    is_valid: bool
    validity_score: float
    reason: str | None


def _bounds_check(value: float, bounds: tuple[float, float], name: str) -> str | None:
    low, high = bounds
    if value < low or value > high:
        return f"{name} out of bounds: {value} not in [{low}, {high}]"
    return None


def check_validity(
    moisture_pct: float,
    gas_index_value: float,
    temp_c: float,
    previous_moisture_pct: float | None = None,
) -> ValidityResult:
    """Bounds + rate-of-change check.

    previous_moisture_pct is optional — a cold-start reading with no prior
    value skips the rate-of-change check rather than failing it.
    """
    for value, bounds, name in (
        (moisture_pct, MOISTURE_BOUNDS, "moisture_pct"),
        (gas_index_value, GAS_INDEX_BOUNDS, "gas_index"),
        (temp_c, TEMP_BOUNDS, "temp_c"),
    ):
        reason = _bounds_check(value, bounds, name)
        if reason is not None:
            low, high = bounds
            span = high - low
            overshoot = max(value - high, low - value, 0.0)
            score = min(1.0, 0.5 + overshoot / span) if span else 1.0
            return ValidityResult(is_valid=False, validity_score=score, reason=reason)

    if previous_moisture_pct is not None:
        jump = abs(moisture_pct - previous_moisture_pct)
        if jump > MAX_MOISTURE_JUMP_PCT:
            excess = jump - MAX_MOISTURE_JUMP_PCT
            score = min(1.0, 0.5 + excess / MAX_MOISTURE_JUMP_PCT)
            return ValidityResult(
                is_valid=False,
                validity_score=score,
                reason=f"moisture jumped {jump:.1f}pct in one reading "
                f"(max plausible {MAX_MOISTURE_JUMP_PCT}pct)",
            )

    return ValidityResult(is_valid=True, validity_score=1.0, reason=None)
