"""Placeholder decision tree — literature-seeded thresholds (PRD Section 6).

Not a trained model. This is the "starting point" tree the PRD (Section 5,
step 1) describes: literature-seeded thresholds standing in for a real
classifier until the controlled two-pot trial (execution plan, 16-17 Aug)
produces labeled data to train and refine it against. Every threshold lives
in THRESHOLDS below so retraining/recalibration touches one place.

Threshold provenance — see docs/soil-health-node-citations.md for full
verification detail (each entry checked against the published paper):
- rsmi_low = 0.20: CITED. Global critical soil moisture threshold for plant
  water stress, Fu et al. 2024, Nature Communications 15:4826
  (theta_crit ~0.19 m3/m3, range 0.12-0.26). Citations doc item 1 flags a
  unit caveat: theta_crit is absolute volumetric water content, RSMI is a
  normalized index — treated as an analogous reference point, not a literal
  unit match.
- rsmi_high = 0.90: NOT independently cited (PRD Section 5, no paper found
  for a specific sustained-waterlogging RSMI cutoff) — engineering
  placeholder pending trial data.
- light_delta_bright, gas_index_trend slope scale: NOT cited. Citations doc
  item 6 documents the literature search performed and why no numeric
  threshold exists for either (BSEC's IAQ index is unitless/proprietary;
  no paper gives a lux-rate-of-change cutoff convertible to this metric).
  Both remain engineering placeholders, calibrate against trial data
  (execution plan, 12-16 Aug).
"""

from dataclasses import dataclass

from .validity import ValidityResult

THRESHOLDS = {
    "rsmi_low": 0.20,
    "rsmi_high": 0.90,
    "light_delta_bright": 50.0,  # placeholder, not literature-sourced — see module docstring
}


@dataclass
class Flag:
    flag: str
    confidence: float
    contributing_trend: str


def _threshold_confidence(distance: float, scale: float) -> float:
    """Map a threshold-crossing distance to a confidence in [0.5, 1.0].

    Placeholder heuristic per PRD Section 6 note ("derived from how far the
    reading sits from the threshold") — the "how many consecutive readings
    agree" half of that note is future rolling-baseline work (architecture
    doc Section 11), not implemented here.
    """
    if scale <= 0:
        return 0.5
    return max(0.5, min(1.0, 0.5 + 0.5 * (distance / scale)))


def classify(
    validity: ValidityResult,
    rsmi: float,
    light_delta: float,
    gas_index_trend: float,
    rsmi_trend: float,
) -> Flag:
    """Walk the PRD Section 6 tree and return a single flag + confidence."""
    if not validity.is_valid:
        return Flag("sensor_fault", validity.validity_score, validity.reason or "")

    if rsmi < THRESHOLDS["rsmi_low"]:
        confidence = _threshold_confidence(
            THRESHOLDS["rsmi_low"] - rsmi, THRESHOLDS["rsmi_low"]
        )
        if light_delta > THRESHOLDS["light_delta_bright"]:
            return Flag("drought_onset", confidence, "moisture_down+light_up")
        return Flag("moisture_low", confidence, "moisture_down")

    if rsmi > THRESHOLDS["rsmi_high"]:
        confidence = _threshold_confidence(
            rsmi - THRESHOLDS["rsmi_high"], 1.0 - THRESHOLDS["rsmi_high"]
        )
        if rsmi_trend > 0:
            trend = "moisture_up"
        elif rsmi_trend < 0:
            trend = "moisture_down"
        else:
            trend = "moisture_flat"
        return Flag("waterlogged_risk", confidence, trend)

    if gas_index_trend < 0 and rsmi_trend < 0:
        confidence = _threshold_confidence(
            abs(gas_index_trend) + abs(rsmi_trend), 1.0
        )
        return Flag("soil_health_degrading", confidence, "gas_down+moisture_down")

    return Flag("stable", 0.5, "none")
