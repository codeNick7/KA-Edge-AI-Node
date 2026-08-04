"""Synthetic Pot A / Pot B data, standing in for real sensors + evidence store.

Not random noise dressed up as data: moisture follows a repeating dry-down
sawtooth between field capacity and wilting point (texture_table.py) with a
simulated watering reset, light/temp follow a diurnal cycle, all keyed off
wall-clock time so the dashboard visibly moves as it's polled, but is
reproducible within any given second (deterministic per time-bucket seed,
not re-rolled on every request). Inference rows run the real
edge_node.validity + edge_node.features + edge_node.decision_tree pipeline
against these synthetic readings, so what the dashboard shows is exactly
what the placeholder classifier would produce on real hardware, not a
hand-faked label.

MODEL_VERSION here names the untrained, literature-seeded placeholder tree
(decision_tree.classify) per architecture doc Section 8's requirement that
every inference record its model version -- once a real trained model is
exported (edge_node/export.py), this becomes that export's model_version.
"""

import math
import random
from datetime import datetime, timedelta, timezone

from edge_node.decision_tree import classify
from edge_node.features import compute_light_delta, compute_rsmi, compute_trend_slope
from edge_node.validity import check_validity

MODEL_VERSION = "placeholder-literature-seeded-v0"

BUCKET_SECONDS = 60  # one synthetic "reading" per minute of wall-clock time

_POT_PROFILE = {
    "A": {"seed": 1, "dry_down_hours": 6.0, "phase_offset_s": 0.0, "texture": "loam"},
    "B": {"seed": 2, "dry_down_hours": 9.0, "phase_offset_s": 1800.0, "texture": "loam"},
}


def _bucket_rng(pot: str, bucket_index: int) -> random.Random:
    profile = _POT_PROFILE[pot]
    return random.Random(f"{profile['seed']}:{bucket_index}")


def _simulate_reading(pot: str, bucket_index: int) -> dict:
    profile = _POT_PROFILE[pot]
    t = bucket_index * BUCKET_SECONDS + profile["phase_offset_s"]
    rng = _bucket_rng(pot, bucket_index)

    from edge_node.texture_table import get_texture_bounds

    bounds = get_texture_bounds(profile["texture"])
    wp, fc = bounds["wilting_point"], bounds["field_capacity"]

    cycle_s = profile["dry_down_hours"] * 3600.0
    phase = (t % cycle_s) / cycle_s  # 0.0 (just watered) -> 1.0 (about to be watered)
    moisture_pct = fc - phase * (fc - wp) * 1.1 + rng.uniform(-0.6, 0.6)
    moisture_pct = max(0.0, min(100.0, moisture_pct))

    day_phase = (t % 86400) / 86400
    light_lux = max(0.0, 800.0 * math.sin(day_phase * math.pi) ** 2 + rng.uniform(-20, 20))
    temp_c = 22.0 + 5.0 * math.sin(day_phase * 2 * math.pi - math.pi / 2) + rng.uniform(-0.3, 0.3)
    gas_index = 150.0 + 40.0 * math.sin(t / 3600.0 + profile["seed"]) + rng.uniform(-5, 5)
    gas_index = max(0.0, min(500.0, gas_index))

    ts = datetime.now(timezone.utc) - timedelta(
        seconds=(_current_bucket(pot) - bucket_index) * BUCKET_SECONDS
    )
    return {
        "timestamp": ts.isoformat(),
        "moisture_pct": round(moisture_pct, 2),
        "gas_index": round(gas_index, 2),
        "temp_c": round(temp_c, 2),
        "light_lux": round(light_lux, 2),
    }


def _current_bucket(pot: str) -> int:
    profile = _POT_PROFILE[pot]
    now_s = datetime.now(timezone.utc).timestamp() - profile["phase_offset_s"]
    return int(now_s // BUCKET_SECONDS)


class MockDataSource:
    def get_raw(self, pot: str, n: int) -> list[dict]:
        if pot not in _POT_PROFILE:
            raise ValueError(f"unknown pot {pot!r}")
        current = _current_bucket(pot)
        return [_simulate_reading(pot, b) for b in range(current - n + 1, current + 1)]

    def get_inference(self, pot: str, n: int) -> list[dict]:
        if pot not in _POT_PROFILE:
            raise ValueError(f"unknown pot {pot!r}")
        texture = _POT_PROFILE[pot]["texture"]
        current = _current_bucket(pot)

        window = 7  # PRD Section 6 trend window
        raw = [_simulate_reading(pot, b) for b in range(current - n - window + 1, current + 1)]

        results = []
        for i in range(window - 1, len(raw)):
            history = raw[i - window + 1 : i + 1]
            row = raw[i]
            prev_moisture = raw[i - 1]["moisture_pct"] if i > 0 else None

            validity = check_validity(
                row["moisture_pct"], row["gas_index"], row["temp_c"], prev_moisture
            )
            rsmi = compute_rsmi(row["moisture_pct"], texture)
            rsmi_history = [compute_rsmi(r["moisture_pct"], texture) for r in history]
            light_history = [r["light_lux"] for r in history]
            gas_history = [r["gas_index"] for r in history]

            light_delta = compute_light_delta(light_history)
            gas_index_trend = compute_trend_slope(gas_history)
            rsmi_trend = compute_trend_slope(rsmi_history)

            flag = classify(validity, rsmi, light_delta, gas_index_trend, rsmi_trend)
            results.append(
                {
                    "timestamp": row["timestamp"],
                    "rsmi": round(rsmi, 4),
                    "light_delta": round(light_delta, 2),
                    "gas_index_trend": round(gas_index_trend, 4),
                    "rsmi_trend": round(rsmi_trend, 4),
                    "flag": flag.flag,
                    "confidence": round(flag.confidence, 3),
                    "contributing_trend": flag.contributing_trend,
                    "model_version": MODEL_VERSION,
                }
            )
        return results[-n:]
