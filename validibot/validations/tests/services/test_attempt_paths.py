"""Tests for canonical execution-attempt storage path derivation.

The path helper is shared by local Docker and Cloud Run dispatch.  These tests
pin the common layout and ensure caller-controlled identifiers cannot reshape
the storage prefix.
"""

from __future__ import annotations

import pytest

from validibot.validations.services.attempt_paths import attempt_bundle_relpath


def test_attempt_bundle_relpath_includes_the_attempt_boundary():
    """Every runtime bundle must live below its own attempt identifier."""
    path = attempt_bundle_relpath(
        org_id="org-1",
        run_id="run-1",
        attempt_id="attempt-1",
    )

    assert path.as_posix() == "runs/org-1/run-1/attempts/attempt-1"


def test_attempt_bundle_relpath_changes_for_a_retry():
    """A retry must receive a different prefix even when its run is unchanged."""
    first = attempt_bundle_relpath(
        org_id="org-1",
        run_id="run-1",
        attempt_id="attempt-1",
    )
    retry = attempt_bundle_relpath(
        org_id="org-1",
        run_id="run-1",
        attempt_id="attempt-2",
    )

    assert first != retry


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("org_id", "../other-org"),
        ("run_id", "nested/run"),
        ("attempt_id", ".."),
        ("attempt_id", "nested\\attempt"),
        ("attempt_id", ""),
        ("attempt_id", "bad\x00attempt"),
    ],
)
def test_attempt_bundle_relpath_rejects_unsafe_components(field_name, bad_value):
    """No identifier may add directories or escape the server-owned prefix."""
    values = {
        "org_id": "org-1",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
    }
    values[field_name] = bad_value

    with pytest.raises(ValueError, match=f"Unsafe {field_name}"):
        attempt_bundle_relpath(**values)
