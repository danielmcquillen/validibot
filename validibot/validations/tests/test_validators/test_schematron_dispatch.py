"""Tests for Schematron execution-backend dispatch (ADR-2026-07-01 D4).

Membership in ``ADVANCED_VALIDATION_TYPES`` routes Schematron to a container
processor, but ``GCPExecutionBackend.execute()`` dispatches off an explicit
per-type table — the review flagged that without a ``SCHEMATRON`` entry, a
Schematron step would fall through to "Unsupported validator type" on GCP
despite being correctly advanced-routed. These tests pin that dispatch entry
(and the fall-through refusal for genuinely unknown types) without touching
Cloud Run: the launcher itself is exercised by its own tests and the backend
repo's layer-C suite.
"""

from __future__ import annotations

from types import SimpleNamespace

from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.execution.gcp import GCPExecutionBackend


def _backend_with_project() -> GCPExecutionBackend:
    """A GCP backend that believes it is configured (no real GCP calls)."""
    backend = GCPExecutionBackend()
    backend._project_id = "test-project"
    return backend


def test_execute_dispatches_schematron_to_its_branch(monkeypatch):
    """SCHEMATRON requests reach _execute_schematron, not the fall-through.

    This is the exact gap the ADR's D4 note calls out: the dispatch table is
    explicit, so adding the type to ADVANCED_VALIDATION_TYPES alone would
    leave GCP execution returning "Unsupported validator type".
    """
    backend = _backend_with_project()
    seen: dict[str, object] = {}

    def _fake_execute_schematron(request):
        seen["request"] = request
        return ExecutionResponse(execution_id="exec-1", is_complete=False)

    monkeypatch.setattr(backend, "_execute_schematron", _fake_execute_schematron)

    request = SimpleNamespace(validator_type="SCHEMATRON", run_id="run-1")
    response = backend.execute(request)

    assert seen["request"] is request
    assert response.execution_id == "exec-1"
    assert response.error_message is None


def test_execute_still_refuses_unknown_types():
    """An unregistered type gets the explicit refusal, never a silent pass.

    Guards the dispatch table's fall-through: a future validator type that
    forgets its branch must surface as a clear configuration error.
    """
    backend = _backend_with_project()

    request = SimpleNamespace(validator_type="NOT_A_REAL_TYPE", run_id="run-1")
    response = backend.execute(request)

    assert response.is_complete is True
    assert "Unsupported validator type" in (response.error_message or "")
