from edge_node.texture_table import get_texture_bounds


def test_known_textures():
    assert get_texture_bounds("sandy_loam") == {"wilting_point": 22.0, "field_capacity": 46.0}
    assert get_texture_bounds("clay_loam") == {"wilting_point": 34.0, "field_capacity": 70.0}
    assert get_texture_bounds("loam") == {"wilting_point": 28.0, "field_capacity": 58.0}


def test_unknown_texture_defaults_to_loam():
    assert get_texture_bounds("unobtainium") == {"wilting_point": 28.0, "field_capacity": 58.0}


def test_none_defaults_to_loam():
    assert get_texture_bounds(None) == {"wilting_point": 28.0, "field_capacity": 58.0}


def test_case_and_whitespace_insensitive():
    assert get_texture_bounds("  Loam  ") == {"wilting_point": 28.0, "field_capacity": 58.0}
    assert get_texture_bounds("SANDY_LOAM") == {"wilting_point": 22.0, "field_capacity": 46.0}
