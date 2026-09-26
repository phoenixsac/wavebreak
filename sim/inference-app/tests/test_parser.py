"""Tiny flat-YAML parser: ints, floats, bools, bare/quoted strings, comments."""

from conftest import load_bundle_module

app = load_bundle_module("v1.0")


def test_parses_scalar_types():
    text = """
# a full-line comment
int_key: 42
float_key: 3.14
bool_true: true
bool_false: False
bare_string: full
quoted: "hello world"
single_quoted: 'x:y#z'
inline_comment: 5  # trailing comment
"""
    cfg = app.parse_flat_yaml(text)
    assert cfg["int_key"] == 42
    assert isinstance(cfg["int_key"], int)
    assert cfg["float_key"] == 3.14
    assert cfg["bool_true"] is True
    assert cfg["bool_false"] is False
    assert cfg["bare_string"] == "full"
    assert cfg["quoted"] == "hello world"
    assert cfg["single_quoted"] == "x:y#z"
    assert cfg["inline_comment"] == 5


def test_ignores_blank_lines_and_comment_only_lines():
    cfg = app.parse_flat_yaml("\n# just a comment\n\nkey: value\n")
    assert cfg == {"key": "value"}


def test_empty_value_becomes_empty_string():
    cfg = app.parse_flat_yaml("empty:\n")
    assert cfg["empty"] == ""


def test_load_config_file_reads_real_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("a: 1\nb: two\n")
    cfg = app.load_config_file(path)
    assert cfg == {"a": 1, "b": "two"}


def test_load_config_file_missing_file_returns_empty(tmp_path):
    assert app.load_config_file(tmp_path / "does-not-exist.yaml") == {}


def test_bundle_config_yaml_files_parse_for_every_release():
    from conftest import ALL_VERSIONS, BUNDLES_DIR

    for version in ALL_VERSIONS:
        module = load_bundle_module(version)
        cfg = module.load_config_file(BUNDLES_DIR / version / "config.yaml")
        assert "frame_interval_s" in cfg
        assert "cache_max_items" in cfg
