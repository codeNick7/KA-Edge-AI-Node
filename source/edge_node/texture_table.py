"""Soil texture lookup table for RSMI.

CORRECTED per docs/soil-health-node-citations.md item 4: the PRD's original
sandy/loam/clay ranges (attributed to Cornell NMSP) do not match any
verifiable published source and were withdrawn. These are INTERIM values
from field-measured data (PMC7085626, "Irrigation Scheduling Based on
Wireless Sensors Output") pending a jar/sedimentation test on the actual
pot mix (60% red soil / 20% clay / 20% manure) and a Saxton & Rawls (2006)
pedotransfer calculation from that measured sand/clay/OM split — see the
citations doc for the full status.

    sandy_loam: WP 11% / FC 23%  (PMC7085626, field-measured)
    clay_loam:  WP 17% / FC 35%  (PMC7085626, field-measured)
    loam:       WP 14% / FC 29%  (interpolated midpoint of the two above —
                not itself measured; the interim default until the jar
                test + Saxton-Rawls calculation replace it)
"""

TEXTURE_TABLE = {
    "sandy_loam": {"wilting_point": 11.0, "field_capacity": 23.0},
    "clay_loam": {"wilting_point": 17.0, "field_capacity": 35.0},
    "loam": {"wilting_point": 14.0, "field_capacity": 29.0},
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
