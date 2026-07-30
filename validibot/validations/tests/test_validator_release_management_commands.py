"""Regression tests for validator release management-command interfaces.

Django's ``BaseCommand`` reserves global options such as ``--version``.
Release lifecycle commands must therefore use a distinct option name for the
backend release identity, or their parsers fail before any operator action can
run. These tests construct the real parsers so option collisions cannot hide
behind service-level test coverage.
"""

from validibot.validations.management.commands import activate_validator_backend_release
from validibot.validations.management.commands import retire_validator_backend_release


def test_activation_parser_accepts_release_version_without_shadowing_django():
    """Activation must keep Django's flag while parsing release identity."""
    parser = activate_validator_backend_release.Command().create_parser(
        "manage.py",
        "activate_validator_backend_release",
    )

    options = parser.parse_args(
        [
            "--backend",
            "energyplus",
            "--release-version",
            "0.15.4",
        ]
    )

    assert options.release_version == "0.15.4"


def test_retirement_parser_accepts_release_version_without_shadowing_django():
    """Retirement must remain callable when its exact backend version is supplied."""
    parser = retire_validator_backend_release.Command().create_parser(
        "manage.py",
        "retire_validator_backend_release",
    )

    options = parser.parse_args(
        [
            "--backend",
            "energyplus",
            "--release-version",
            "0.15.4",
            "--reason",
            "Verified drain completed.",
        ]
    )

    assert options.release_version == "0.15.4"
