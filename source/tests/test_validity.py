from edge_node.validity import MAX_MOISTURE_JUMP_PCT, check_validity


def test_valid_reading_no_previous():
    result = check_validity(moisture_pct=25.0, gas_index_value=100.0, temp_c=22.0)
    assert result.is_valid is True
    assert result.validity_score == 1.0
    assert result.reason is None


def test_valid_reading_with_plausible_previous():
    result = check_validity(
        moisture_pct=25.0, gas_index_value=100.0, temp_c=22.0, previous_moisture_pct=20.0
    )
    assert result.is_valid is True


def test_moisture_out_of_bounds_low():
    result = check_validity(moisture_pct=-5.0, gas_index_value=100.0, temp_c=22.0)
    assert result.is_valid is False
    assert "moisture_pct" in result.reason


def test_moisture_out_of_bounds_high():
    result = check_validity(moisture_pct=150.0, gas_index_value=100.0, temp_c=22.0)
    assert result.is_valid is False
    assert "moisture_pct" in result.reason


def test_gas_index_out_of_bounds():
    result = check_validity(moisture_pct=25.0, gas_index_value=999.0, temp_c=22.0)
    assert result.is_valid is False
    assert "gas_index" in result.reason


def test_temp_out_of_bounds():
    result = check_validity(moisture_pct=25.0, gas_index_value=100.0, temp_c=200.0)
    assert result.is_valid is False
    assert "temp_c" in result.reason


def test_boundary_values_are_valid():
    result = check_validity(moisture_pct=0.0, gas_index_value=0.0, temp_c=-10.0)
    assert result.is_valid is True
    result = check_validity(moisture_pct=100.0, gas_index_value=500.0, temp_c=60.0)
    assert result.is_valid is True


def test_implausible_moisture_jump_flagged():
    result = check_validity(
        moisture_pct=60.0,
        gas_index_value=100.0,
        temp_c=22.0,
        previous_moisture_pct=20.0,
    )
    assert result.is_valid is False
    assert "jumped" in result.reason


def test_moisture_jump_exactly_at_threshold_is_valid():
    result = check_validity(
        moisture_pct=20.0 + MAX_MOISTURE_JUMP_PCT,
        gas_index_value=100.0,
        temp_c=22.0,
        previous_moisture_pct=20.0,
    )
    assert result.is_valid is True


def test_moisture_jump_just_over_threshold_is_invalid():
    result = check_validity(
        moisture_pct=20.0 + MAX_MOISTURE_JUMP_PCT + 0.1,
        gas_index_value=100.0,
        temp_c=22.0,
        previous_moisture_pct=20.0,
    )
    assert result.is_valid is False


def test_validity_score_increases_with_severity():
    mild = check_validity(moisture_pct=101.0, gas_index_value=100.0, temp_c=22.0)
    severe = check_validity(moisture_pct=500.0, gas_index_value=100.0, temp_c=22.0)
    assert severe.validity_score > mild.validity_score
