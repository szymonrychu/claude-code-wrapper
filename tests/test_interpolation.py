import pytest

from claude_code_wrapper.interpolation import InterpolationError, interpolate


def test_interpolation_env_and_default():
    env = {"A": "x", "E": ""}
    assert interpolate("${A}-${B:-d}-${E:-e}", env) == "x-d-e"


def test_unset_var_error_names_path():
    with pytest.raises(InterpolationError) as exc:
        interpolate({"a": [{"b": "${NOPE}"}]}, {})
    assert "a.0.b" in str(exc.value)
    assert "NOPE" in str(exc.value)


def test_dollar_escape():
    out = interpolate({"${K}": "$$HOME ${X:-$$}"}, {})
    assert out == {"${K}": "$HOME $"}


def test_non_strings_untouched():
    obj = {"a": 1, "b": True, "c": None, "d": 1.5}
    assert interpolate(obj, {}) == obj


def test_empty_set_var_substituted_without_default():
    assert interpolate("[${E}]", {"E": ""}) == "[]"
