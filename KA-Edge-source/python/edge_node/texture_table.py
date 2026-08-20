"""Soil texture lookup table for RSMI.

CORRECTED per docs/soil-health-node-citations.md item 4: the PRD's original
sandy/loam/clay ranges (attributed to Cornell NMSP) do not match any
verifiable published source and were withdrawn. Base values are INTERIM,
from field-measured data (PMC7085626, "Irrigation Scheduling Based on
Wireless Sensors Output") pending a jar/sedimentation test on the actual
pot mix (60% red soil / 20% clay / 20% manure) and a Saxton & Rawls (2006)
pedotransfer calculation from that measured sand/clay/OM split — see the
citations doc for the full status.

UNIT FIX (2026-08-14): PMC7085626's WP/FC are absolute volumetric water
content (VWC), e.g. clay_loam tops out at FC=35 assuming moisture_pct is a
0-~50%-range VWC reading. But KA-Edge-source/sketch/sketch.ino's soilPct()
already normalizes the raw ADC to its own 0-100 scale (0 = dry air,
100 = fully saturated at that sensor's calibrated SOIL_DRY_ADC/SOIL_WET_ADC
endpoints) before it ever reaches this table. Feeding that into
compute_rsmi() against the absolute-VWC bounds double-normalizes: real
readings sit at 50-90%, permanently above every FC value, so RSMI clips to
1.0 forever and drought_onset (RSMI < 0.20) becomes unreachable regardless
of how dry the soil actually gets (see docs/soil-health-node-citations.md
item 1's unit-consistency caveat, previously unresolved).

Interim reconciliation: scale each PMC7085626 bound by 100/50, treating the
sensor's 100% endpoint as roughly typical soil porosity (~50% VWC ceiling)
rather than PMC7085626's own calibration span. This is a provisional
scale-matching, not a real calibration — replace with true WP/FC points
measured directly against soilPct()'s 0-100 output (e.g. observe the % at
visible wilting and at draining-saturation for the actual pot mix) once the
16-17 Aug trial produces that data.

    sandy_loam: WP 22% / FC 46%  (PMC7085626 11/23, x2 rescale)
    clay_loam:  WP 34% / FC 70%  (PMC7085626 17/35, x2 rescale)
    loam:       WP 28% / FC 58%  (interpolated midpoint of the two above —
                not itself measured; the interim default until the jar
                test + Saxton-Rawls calculation replace it)
"""

TEXTURE_TABLE = {
    "sandy_loam": {"wilting_point": 22.0, "field_capacity": 46.0},
    "clay_loam": {"wilting_point": 34.0, "field_capacity": 70.0},
    "loam": {"wilting_point": 28.0, "field_capacity": 58.0},
}

DEFAULT_TEXTURE = "loam"


def get_texture_bounds(texture: str | None = None) -> dict:
    """Look up (wilting_point, field_capacity) for a soil texture.

    Falls back to loam (PRD Section 4) when texture is unknown or unset —
    deployments that haven't recorded a texture at install time still get a
    usable RSMI rather than an error.
    """
    key = (texture or DEFAULT_TEXTURE).strip().lower()
    return TEXTURE_TABLE.get(key, TEXTURE_TABLE[DEFAULT_TEXTURE])
