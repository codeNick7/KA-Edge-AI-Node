import pytest

from edge_node.features import (
    compute_light_delta,
    compute_rsmi,
    compute_trend_slope,
    gas_index,
)


def test_rsmi_below_wilting_point_clips_to_zero():
    assert compute_rsmi(5.0, texture="loam") == 0.0  # loam WP=28.0


def test_rsmi_above_field_capacity_clips_to_one():
    assert compute_rsmi(99.0, texture="loam") == 1.0  # loam FC=58.0


def test_rsmi_mid_range():
    # loam WP=28.0, FC=58.0, span=30.0; moisture=43.0 -> midpoint
    assert compute_rsmi(43.0, texture="loam") == pytest.approx(0.5)


def test_rsmi_defaults_to_loam_when_texture_unset():
    assert compute_rsmi(43.0) == pytest.approx(0.5)


def test_rsmi_zero_span_raises(monkeypatch):
    from edge_node import features

    monkeypatch.setattr(
        features,
        "get_texture_bounds",
        lambda texture=None: {"wilting_point": 20.0, "field_capacity": 20.0},
    )
    with pytest.raises(ValueError):
        compute_rsmi(20.0, texture="degenerate")


def test_gas_index_passthrough():
    assert gas_index(123) == 123.0
    assert gas_index(45.6) == 45.6


def test_light_delta_rising():
    assert compute_light_delta([100.0, 150.0]) == 50.0


def test_light_delta_falling():
    assert compute_light_delta([150.0, 100.0]) == -50.0


def test_light_delta_flat():
    assert compute_light_delta([100.0, 100.0]) == 0.0


def test_light_delta_insufficient_readings():
    assert compute_light_delta([]) == 0.0
    assert compute_light_delta([100.0]) == 0.0


def test_light_delta_uses_last_two_of_longer_window():
    assert compute_light_delta([10.0, 20.0, 30.0, 25.0]) == -5.0


def test_trend_slope_rising():
    assert compute_trend_slope([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)


def test_trend_slope_falling():
    assert compute_trend_slope([4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)


def test_trend_slope_flat():
    assert compute_trend_slope([5.0, 5.0, 5.0]) == pytest.approx(0.0)


def test_trend_slope_insufficient_readings():
    assert compute_trend_slope([]) == 0.0
    assert compute_trend_slope([1.0]) == 0.0
