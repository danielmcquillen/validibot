"""Tests for the WorkflowSignalMapping model.

Signal mappings let workflow authors declare named signals (e.g. "emissivity")
that resolve a data path in the submission payload before any validation step
runs.  They are the bridge between the author's domain vocabulary and the
raw JSON structure of submissions, and are surfaced in CEL expressions as
``s.<name>``.

These tests verify the model's database constraints, field behaviour, ordering,
and cascade semantics.  Getting any of these wrong would silently break signal
resolution at validation time or allow duplicate signal names that produce
ambiguous CEL bindings, so each invariant is tested explicitly.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db

EXPECTED_WORKFLOW_SIGNAL_COUNT = 2


# ── Basic creation ──────────────────────────────────────────────────────
# Confirm that a mapping round-trips through the ORM with all fields intact.


class TestCreateBasicMapping:
    def test_create_basic_mapping(self):
        """A signal mapping must persist all core fields faithfully.

        If any field is silently dropped or transformed on save, signal
        resolution will use stale or default values, producing wrong
        validation results with no visible error.
        """
        workflow = WorkflowFactory()
        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
        )
        mapping.refresh_from_db()

        assert mapping.name == "emissivity"
        assert mapping.source_path == "materials[0].emissivity"
        assert mapping.on_missing == "error"
        assert mapping.workflow == workflow


# ── Unique constraint ───────────────────────────────────────────────────
# A workflow must not have two signals with the same name, because CEL
# bindings are keyed by name — duplicates would make ``s.<name>``
# ambiguous.


class TestUniqueConstraint:
    def test_unique_constraint_enforced(self):
        """Two mappings with the same workflow + name must raise ValidationError.

        The model's ``save()`` calls ``full_clean()`` which triggers the
        application-level cross-table uniqueness check in ``clean()``.
        This fires before the database constraint, providing a clear
        error message instead of an opaque IntegrityError.

        Without this check, the resolver could silently pick one of
        the duplicates, leading to non-deterministic validation outcomes.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
        )
        with pytest.raises(ValidationError, match="already defined"):
            WorkflowSignalMapping.objects.create(
                workflow=workflow,
                name="emissivity",
                source_path="materials[1].emissivity",
                on_missing="error",
            )

    def test_different_workflows_same_name_allowed(self):
        """Different workflows may each define a signal named "emissivity".

        The unique constraint is scoped to (workflow, name), not name
        alone.  A global uniqueness constraint would make common domain
        terms unusable across independent workflows.
        """
        wf_a = WorkflowFactory()
        wf_b = WorkflowFactory()

        WorkflowSignalMapping.objects.create(
            workflow=wf_a,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
        )
        mapping_b = WorkflowSignalMapping.objects.create(
            workflow=wf_b,
            name="emissivity",
            source_path="surfaces[0].emissivity",
            on_missing="error",
        )
        # If we get here without IntegrityError the constraint is correct.
        assert mapping_b.pk is not None


# ── on_missing choices ──────────────────────────────────────────────────
# The two modes ("error" and "null") control whether a missing signal
# aborts the run or injects null.  Both must be accepted by the ORM.


class TestOnMissingChoices:
    def test_on_missing_choices(self):
        """Both "error" and "null" are valid on_missing values.

        These are the only two strategies the resolver understands.  If
        the model rejects either value, workflow authors lose the ability
        to choose graceful degradation vs. fail-fast for optional signals.
        """
        workflow = WorkflowFactory()
        for value in ("error", "null"):
            mapping = WorkflowSignalMapping.objects.create(
                workflow=workflow,
                name=f"signal_{value}",
                source_path=f"path.{value}",
                on_missing=value,
            )
            mapping.refresh_from_db()
            assert mapping.on_missing == value


# ── default_value JSON field ────────────────────────────────────────────
# default_value is a JSONField that must faithfully store any JSON-legal
# value, because authors may need to supply fallback scalars, objects,
# arrays, or explicit null.


class TestDefaultValueJsonField:
    @pytest.mark.parametrize(
        "value",
        [
            {"key": "val"},
            [1, 2, 3],
            42,
            "hello",
            True,
            None,
        ],
        ids=["dict", "list", "number", "string", "bool", "null"],
    )
    def test_default_value_json_field(self, value):
        """default_value must round-trip any JSON-legal type.

        Workflow authors supply fallback values of arbitrary shape — a
        numeric default for a measurement signal, a dict for a compound
        default, or null to explicitly indicate absence.  Losing type
        fidelity would inject the wrong value at validation time.
        """
        workflow = WorkflowFactory()
        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name=f"sig_{id(value)}",
            source_path="some.path",
            default_value=value,
        )
        mapping.refresh_from_db()
        assert mapping.default_value == value


# ── Ordering ────────────────────────────────────────────────────────────
# Mappings are ordered by ``position`` so the UI and resolver process
# signals in the author's intended sequence.


class TestOrderingByPosition:
    def test_ordering_by_position(self):
        """Querysets must return mappings ordered by the position field.

        The UI displays signals in author-defined order, and some
        downstream tooling may depend on deterministic iteration.  If
        ordering is lost, the signal editor becomes confusing and any
        position-dependent logic breaks silently.
        """
        workflow = WorkflowFactory()
        m3 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="third",
            source_path="c",
            position=30,
        )
        m1 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="first",
            source_path="a",
            position=10,
        )
        m2 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="second",
            source_path="b",
            position=20,
        )

        ordered = list(
            WorkflowSignalMapping.objects.filter(workflow=workflow),
        )
        assert ordered == [m1, m2, m3]


# ── String representation ──────────────────────────────────────────────
# The __str__ output appears in admin, debug logs, and error messages.
# It must clearly identify which signal maps to which path.


class TestStrRepresentation:
    def test_str_representation(self):
        """str(mapping) must return 's.emissivity -> materials[0].emissivity'.

        This format mirrors the CEL accessor syntax (s.<name>) so that
        log messages and admin listings are immediately recognisable to
        workflow authors debugging signal resolution issues.
        """
        workflow = WorkflowFactory()
        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
        )
        assert str(mapping) == "s.emissivity \u2192 materials[0].emissivity"


# ── Cascade delete ──────────────────────────────────────────────────────
# Signal mappings are owned by their workflow.  Deleting the workflow
# must cascade to its mappings so no orphaned rows remain.


class TestCascadeDeleteWithWorkflow:
    def test_cascade_delete_with_workflow(self):
        """Deleting a workflow must cascade-delete its signal mappings.

        Orphaned mappings would accumulate over time and could collide
        with future workflows if uniqueness constraints are ever relaxed.
        The FK uses on_delete=CASCADE, and this test proves it works
        end-to-end through the ORM.
        """
        workflow = WorkflowFactory()
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
        )
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="conductivity",
            source_path="materials[0].conductivity",
        )
        assert (
            WorkflowSignalMapping.objects.filter(workflow=workflow).count()
            == EXPECTED_WORKFLOW_SIGNAL_COUNT
        )

        workflow.delete()

        assert WorkflowSignalMapping.objects.count() == 0
