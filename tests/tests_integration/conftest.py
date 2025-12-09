"""
Pytest fixtures for integration tests.

This conftest handles the psycopg3 + live_server threading issue.

The problem: Django's live_server uses a threaded server. psycopg3 connections
are not thread-safe. After a test, the connection is corrupted but Django's
DatabaseWrapper still holds a reference to it. When pytest-django tries to
flush the database during teardown, it fails.

Solution: Monkey-patch the flush command to reset connections before running.
"""

import pytest
from django.db import connections


def _reset_bad_connections():
    """
    Reset database connections that are in a BAD state.

    This forces Django to create fresh connections on the next database access.
    We must also reset Django's atomic block tracking to allow new connections.
    """
    for conn in connections.all():
        # Check if the underlying psycopg connection is BAD
        if hasattr(conn, "connection") and conn.connection is not None:
            try:
                is_bad = False
                # Check if the connection is usable
                if hasattr(conn.connection, "pgconn"):
                    from psycopg.pq import ConnStatus

                    if conn.connection.pgconn.status == ConnStatus.BAD:
                        is_bad = True

                if is_bad:
                    # Force-reset ALL connection state so Django can reconnect
                    conn.connection = None
                    conn.closed_in_transaction = False
                    conn.in_atomic_block = False
                    conn.savepoint_ids = []
                    conn.needs_rollback = False
            except Exception:
                # If we can't check, assume it's bad and reset everything
                conn.connection = None
                conn.closed_in_transaction = False
                conn.in_atomic_block = False
                conn.savepoint_ids = []
                conn.needs_rollback = False


@pytest.fixture(autouse=True)
def reset_connections_before_test():
    """Reset any bad connections before each test starts."""
    _reset_bad_connections()
    yield
    # Also reset after test in case teardown needs a fresh connection
    _reset_bad_connections()


# Monkey-patch Django's flush command to reset connections before running
_original_flush_handle = None


def _patched_flush_handle(self, *args, **options):
    """Patched flush command that resets bad connections first."""
    _reset_bad_connections()
    return _original_flush_handle(self, *args, **options)


def pytest_configure(config):
    """Patch the flush command when pytest starts."""
    global _original_flush_handle
    from django.core.management.commands.flush import Command as FlushCommand

    _original_flush_handle = FlushCommand.handle
    FlushCommand.handle = _patched_flush_handle


def pytest_unconfigure(config):
    """Restore the original flush command when pytest ends."""
    from django.core.management.commands.flush import Command as FlushCommand

    if _original_flush_handle is not None:
        FlushCommand.handle = _original_flush_handle
