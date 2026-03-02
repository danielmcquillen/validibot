"""
Periodic email handler registry.

Provides a simple registry for downstream packages (e.g., validibot-cloud) to
register email handlers that run on a schedule. The ``send_periodic_emails``
management command calls all registered handlers.

This follows the same pattern as ``validibot.core.features.register_feature()``.
In community-only installs, no handlers are registered and the command is a no-op.

Usage (in a downstream AppConfig.ready):

    from validibot.core.emails import register_periodic_email_handler

    register_periodic_email_handler("trial-lifecycle", send_trial_lifecycle_emails)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import StringIO

logger = logging.getLogger(__name__)

_handlers: dict[str, Callable[[StringIO], None]] = {}


def register_periodic_email_handler(
    name: str, handler: Callable[[StringIO], None]
) -> None:
    """Register a periodic email handler by name."""
    _handlers[name] = handler
    logger.info("Periodic email handler registered: %s", name)


def get_periodic_email_handlers() -> dict[str, Callable[[StringIO], None]]:
    """Return a copy of all registered handlers."""
    return dict(_handlers)


def reset_periodic_email_handlers() -> None:
    """Clear all registered handlers (for testing)."""
    _handlers.clear()
