#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys
from pathlib import Path


def get_default_settings_module(argv: list[str]) -> str:
    """Return the default Django settings module for a manage.py invocation.

    ``manage.py test`` should use the dedicated test settings so test-only
    storage backends and other overrides are active without requiring callers
    to export ``DJANGO_SETTINGS_MODULE`` manually.
    """
    if len(argv) > 1 and argv[1] == "test":
        return "config.settings.test"
    return "config.settings.local"


def main():
    """Run administrative tasks."""
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE",
        get_default_settings_module(sys.argv),
    )

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?",
        ) from exc

    # This allows easy placement of apps within the interior
    # validibot directory.
    current_path = Path(__file__).parent.resolve()
    sys.path.append(str(current_path / "validibot"))

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
