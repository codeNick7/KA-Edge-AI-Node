from edge_node.decision_tree import classify
from edge_node.validity import ValidityResult

VALID = ValidityResult(is_valid=True, validity_score=1.0, reason=None)


def test_sensor_fault_leaf():
    invalid = ValidityResult(is_valid=False, validity_score=0.8, reason="moisture_pct out of bounds")
    result = classify(invalid, rsmi=0.5, light_delta=0.0, gas_index_trend=0.0, rsmi_trend=0.0)
    assert result.flag == "sensor_fault"
    assert result.confidence == 0.8


def test_drought_onset_leaf():
    result = classify(VALID, rsmi=0.10, light_delta=60.0, gas_index_trend=0.0, rsmi_trend=0.0)
    assert result.flag == "drought_onset"
    assert result.contributing_trend == "moisture_down+light_up"


def test_moisture_low_leaf():
    result = classify(VALID, rsmi=0.10, light_delta=10.0, gas_index_trend=0.0, rsmi_trend=0.0)
    assert result.flag == "moisture_low"
    assert result.contributing_trend == "moisture_down"


def test_waterlogged_risk_leaf():
    result = classify(VALID, rsmi=0.95, light_delta=0.0, gas_index_trend=1.0, rsmi_trend=1.0)
    assert result.flag == "waterlogged_risk"


def test_soil_health_degrading_leaf():
    result = classify(VALID, rsmi=0.50, light_delta=0.0, gas_index_trend=-2.0, rsmi_trend=-0.1)
    assert result.flag == "soil_health_degrading"


def test_stable_leaf():
    result = classify(VALID, rsmi=0.50, light_delta=0.0, gas_index_trend=1.0, rsmi_trend=-0.1)
    assert result.flag == "stable"
    assert result.confidence == 0.5


def test_rsmi_low_boundary_is_not_drought():
    # rsmi == threshold should NOT trigger the "< threshold" branch
    result = classify(VALID, rsmi=0.20, light_delta=0.0, gas_index_trend=0.0, rsmi_trend=0.0)
    assert result.flag not in ("drought_onset", "moisture_low")


def test_rsmi_high_boundary_is_not_waterlogged():
    result = classify(VALID, rsmi=0.90, light_delta=0.0, gas_index_trend=1.0, rsmi_trend=1.0)
    assert result.flag != "waterlogged_risk"


def test_confidence_is_within_valid_range():
    result = classify(VALID, rsmi=0.01, light_delta=100.0, gas_index_trend=0.0, rsmi_trend=0.0)
    assert 0.5 <= result.confidence <= 1.0
