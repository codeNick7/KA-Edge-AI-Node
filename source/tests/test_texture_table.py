from edge_node.texture_table import get_texture_bounds


def test_known_textures():
    assert get_texture_bounds("sandy_loam") == {"wilting_point": 11.0, "field_capacity": 23.0}
    assert get_texture_bounds("clay_loam") == {"wilting_point": 17.0, "field_capacity": 35.0}
    assert get_texture_bounds("loam") == {"wilting_point": 14.0, "field_capacity": 29.0}


def test_unknown_texture_defaults_to_loam():
    assert get_texture_bounds("unobtainium") == {"wilting_point": 14.0, "field_capacity": 29.0}


def test_none_defaults_to_loam():
    assert get_texture_bounds(None) == {"wilting_point": 14.0, "field_capacity": 29.0}


def test_case_and_whitespace_insensitive():
    assert get_texture_bounds("  Loam  ") == {"wilting_point": 14.0, "field_capacity": 29.0}
    assert get_texture_bounds("SANDY_LOAM") == {"wilting_point": 11.0, "field_capacity": 23.0}
