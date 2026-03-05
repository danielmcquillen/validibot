"""Unit tests for ``WorkflowStepResource`` — the relational through table
that binds resources (weather files, model templates) to workflow steps.

Background
----------
Previously, resource references lived as UUID strings inside
``WorkflowStep.config["resource_file_ids"]`` — a JSONField with no FK
integrity. ``WorkflowStepResource`` replaces that with a proper relational
model that supports two mutually exclusive modes:

1. **Catalog reference** — FK to a shared ``ValidatorResourceFile`` (PROTECT),
   e.g., a weather EPW file from the validator library.
2. **Step-owned file** — a ``FileField`` on the record itself, e.g., a
   template IDF uploaded specifically for this step.

The DB-level ``ck_step_resource_xor_file`` check constraint ensures exactly
one mode is populated. Steps CASCADE to their resources; deleting a shared
catalog file that is still in use is blocked (PROTECT).

See Also
--------
ADR 2026-03-04: EnergyPlus Parameterized Model Templates — Phase 0
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError

from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

# ── Catalog reference mode ──────────────────────────────────────────
#
# These tests verify the "Mode 1" path: a step resource that points to
# a shared ValidatorResourceFile from the validator library. This is the
# default factory behaviour (WorkflowStepResourceFactory creates a
# catalog reference with role=WEATHER_FILE).


@pytest.mark.django_db
def test_catalog_reference_creation():
    """Creating a catalog-reference resource sets the FK and leaves the
    FileField empty. The ``is_catalog_reference`` property must be True,
    and ``is_step_owned`` must be False.
    """
    resource = WorkflowStepResourceFactory()

    assert resource.pk is not None
    assert resource.is_catalog_reference is True
    assert resource.is_step_owned is False
    assert resource.validator_resource_file is not None
    assert resource.step_resource_file.name == ""


@pytest.mark.django_db
def test_catalog_reference_str():
    """The __str__ for catalog-reference mode includes 'catalog=' and
    the resource role for quick identification in admin/debugging.
    """
    resource = WorkflowStepResourceFactory()
    s = str(resource)
    assert "catalog=" in s
    assert resource.role in s


# ── Step-owned file mode ────────────────────────────────────────────
#
# These tests verify "Mode 2": a step resource that stores its own file
# directly (e.g., a template IDF uploaded for this specific step).
# No ValidatorResourceFile FK is set.


@pytest.mark.django_db
def test_step_owned_resource_creation():
    """Creating a step-owned resource saves the file to the FileField
    and leaves the ValidatorResourceFile FK null. The ``is_step_owned``
    property must be True.
    """
    step = WorkflowStepFactory()
    resource = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        validator_resource_file=None,
        step_resource_file=SimpleUploadedFile("template.idf", b"! IDF template"),
        filename="template.idf",
        resource_type="ENERGYPLUS_IDF",
    )

    assert resource.pk is not None
    assert resource.is_step_owned is True
    assert resource.is_catalog_reference is False
    assert resource.validator_resource_file is None
    assert resource.step_resource_file.name != ""
    assert resource.filename == "template.idf"


@pytest.mark.django_db
def test_step_owned_str():
    """The __str__ for step-owned mode includes 'file=' and the original
    filename for quick identification.
    """
    step = WorkflowStepFactory()
    resource = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        step_resource_file=SimpleUploadedFile("template.idf", b"! IDF template"),
        filename="template.idf",
    )
    s = str(resource)
    assert "file=" in s
    assert "template.idf" in s


# ── XOR constraint ──────────────────────────────────────────────────
#
# The ``ck_step_resource_xor_file`` check constraint enforces that
# exactly one of the two modes is populated. These tests confirm the DB
# rejects invalid combinations at the SQL level — this cannot be
# bypassed even if application-level validation is skipped.


@pytest.mark.django_db
def test_xor_constraint_both_set_raises():
    """The DB rejects a row where both ``validator_resource_file`` AND
    ``step_resource_file`` are populated. This prevents ambiguity about
    which file source is authoritative.
    """
    step = WorkflowStepFactory()
    vrf = ValidatorResourceFileFactory()

    with pytest.raises(IntegrityError, match="ck_step_resource_xor_file"):
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=vrf,
            step_resource_file=SimpleUploadedFile("dup.epw", b"LOCATION,Dup"),
        )


@pytest.mark.django_db
def test_xor_constraint_neither_set_raises():
    """The DB rejects a row where neither ``validator_resource_file``
    nor ``step_resource_file`` is populated. A resource must point to
    *something* — an empty record is meaningless.
    """
    step = WorkflowStepFactory()

    with pytest.raises(IntegrityError, match="ck_step_resource_xor_file"):
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=None,
            step_resource_file="",
        )


# ── FK cascade / protect semantics ─────────────────────────────────
#
# These tests verify the referential integrity contracts:
# - Deleting a WorkflowStep CASCADES to its resources (no orphans).
# - Deleting a ValidatorResourceFile that is still referenced is
#   PROTECTED (no dangling FKs).


@pytest.mark.django_db
def test_cascade_deleting_step_deletes_resources():
    """When a WorkflowStep is deleted, all its WorkflowStepResource rows
    are automatically deleted via CASCADE. This prevents orphaned
    resources and is the expected Django FK behaviour.
    """
    resource = WorkflowStepResourceFactory()
    resource_id = resource.pk
    step = resource.step

    step.delete()

    assert not WorkflowStepResource.objects.filter(pk=resource_id).exists()


@pytest.mark.django_db
def test_protect_deleting_validator_resource_file_blocked():
    """Attempting to delete a ValidatorResourceFile that is still
    referenced by a WorkflowStepResource raises ProtectedError.
    This prevents the "dangling UUID" problem that existed when
    resource references were stored as JSON strings.
    """
    resource = WorkflowStepResourceFactory()
    vrf = resource.validator_resource_file

    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        vrf.delete()


# ── get_storage_uri() dispatch ──────────────────────────────────────
#
# ``get_storage_uri()`` is the single entry point for the envelope
# builder to resolve a resource to a downloadable URI. It delegates
# to the appropriate source depending on the resource mode.


@pytest.mark.django_db
def test_get_storage_uri_catalog_reference():
    """For catalog-reference resources, ``get_storage_uri()`` delegates
    to ``ValidatorResourceFile.get_storage_uri()``. In tests with local
    filesystem storage, this returns a ``file://`` URI.
    """
    resource = WorkflowStepResourceFactory()
    uri = resource.get_storage_uri()

    # The factory creates a file using local filesystem storage,
    # so the URI should start with file:// (ValidatorResourceFile.get_storage_uri)
    assert uri.startswith("file://")
    assert "weather" in uri.lower() or "epw" in uri.lower()


@pytest.mark.django_db
def test_get_storage_uri_step_owned():
    """For step-owned resources, ``get_storage_uri()`` returns the
    Django FileField's ``.url`` property — a relative media path in
    local storage, or a GCS URI in production.
    """
    step = WorkflowStepFactory()
    resource = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        step_resource_file=SimpleUploadedFile("template.idf", b"! IDF data"),
        filename="template.idf",
    )

    uri = resource.get_storage_uri()
    # Local storage returns a path via .url
    assert "step_resources" in uri or "template" in uri


# ── Role constants ──────────────────────────────────────────────────


def test_role_constants_defined():
    """Smoke test: verify role constants are accessible on the model
    class and match expected string values. These constants are used
    throughout the codebase for filtering step resources by purpose.
    """
    assert WorkflowStepResource.WEATHER_FILE == "WEATHER_FILE"
    assert WorkflowStepResource.MODEL_TEMPLATE == "MODEL_TEMPLATE"


# ── Multiple resources per step ─────────────────────────────────────


@pytest.mark.django_db
def test_step_can_have_multiple_resources():
    """A single step can hold resources with different roles — for
    example, both a weather file (catalog reference) and a template
    (step-owned file). This is the anticipated EnergyPlus parameterized
    template workflow where a step needs both a weather EPW and a
    template IDF.
    """
    step = WorkflowStepFactory()
    weather = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=ValidatorResourceFileFactory(),
    )
    template = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        step_resource_file=SimpleUploadedFile("template.idf", b"! IDF"),
        filename="template.idf",
    )

    expected_count = 2
    assert step.step_resources.count() == expected_count
    assert (
        step.step_resources.filter(role=WorkflowStepResource.WEATHER_FILE).first()
        == weather
    )
    assert (
        step.step_resources.filter(role=WorkflowStepResource.MODEL_TEMPLATE).first()
        == template
    )
