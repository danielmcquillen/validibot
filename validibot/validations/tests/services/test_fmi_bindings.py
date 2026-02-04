"""
Tests for FMI binding resolution helpers.
"""

from validibot.validations.services.fmi_bindings import resolve_input_value


def test_resolve_input_value_dotted_path():
    """Dotted binding paths traverse nested dictionaries."""
    payload = {"outer": {"inner": {"value": 42}}}
    value = resolve_input_value(payload, binding_path="outer.inner.value", slug="value")
    assert value == 42  # noqa: PLR2004


def test_resolve_input_value_slug_default():
    """When no binding path, slug lookup is used."""
    payload = {"temperature": 21}
    value = resolve_input_value(payload, binding_path="", slug="temperature")
    assert value == 21  # noqa: PLR2004


def test_resolve_input_value_missing_returns_none():
    """Missing path returns None."""
    payload = {"temperature": 21}
    value = resolve_input_value(payload, binding_path="outer.inner", slug="temperature")
    assert value is None
