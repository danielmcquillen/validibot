"""Regression tests for manage.py defaults."""

import manage


def test_manage_test_defaults_to_test_settings():
    """The test subcommand should load test settings by default."""
    assert (
        manage.get_default_settings_module(["manage.py", "test"])
        == "config.settings.test"
    )


def test_manage_non_test_command_defaults_to_local_settings():
    """Non-test management commands should keep using local settings."""
    assert (
        manage.get_default_settings_module(["manage.py", "runserver"])
        == "config.settings.local"
    )


def test_manage_without_subcommand_defaults_to_local_settings():
    """A bare manage.py invocation should still use local settings."""
    assert manage.get_default_settings_module(["manage.py"]) == "config.settings.local"
