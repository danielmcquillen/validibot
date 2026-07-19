"""Detect database histories that predate the current-schema reset.

Validibot deliberately replaced several long pre-launch migration tails on
2026-07-16. Those files were not Django squashes and therefore do not declare
``replaces``. A fresh database is safe, as is a database already built from the
new ``*_current_schema`` migrations. A database that recorded one of the
deleted tail migrations would otherwise let ``migrate`` attempt duplicate
schema operations.

This module keeps the cut lines explicit and exposes a pure comparison helper
used by deployment preflight and backup-restore compatibility checks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# App label -> (first reset migration number, current-schema marker).
CURRENT_SCHEMA_CUTOVERS: dict[str, tuple[int, str]] = {
    "actions": (2, "0002_current_schema"),
    "submissions": (3, "0003_current_schema"),
    "users": (4, "0004_current_schema"),
    "validations": (26, "0026_current_schema"),
    "workflows": (7, "0007_current_schema"),
}


def incompatible_reset_migrations(
    *,
    applied_migrations: Iterable[tuple[str, str]],
    known_migrations: Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    """Return deleted pre-reset migration records that make upgrade unsafe.

    Known current migrations at or beyond a cut line are valid. Unknown names
    below the cut line are retained historical prefixes and are also valid.
    An unknown name at or beyond a cut line belongs to the removed migration
    tail. If the app's current-schema marker is already recorded, the database
    has completed the deliberate cutover and old recorder rows are harmless.
    """
    applied = set(applied_migrations)
    known = set(known_migrations)
    incompatible: list[str] = []

    for app_label, (cutover_number, current_marker) in CURRENT_SCHEMA_CUTOVERS.items():
        if (app_label, current_marker) in applied:
            continue
        for applied_app, migration_name in applied:
            if applied_app != app_label or (applied_app, migration_name) in known:
                continue
            number_text, _, _description = migration_name.partition("_")
            if number_text.isdigit() and int(number_text) >= cutover_number:
                incompatible.append(f"{app_label}.{migration_name}")

    return tuple(sorted(incompatible))


__all__ = ["CURRENT_SCHEMA_CUTOVERS", "incompatible_reset_migrations"]
