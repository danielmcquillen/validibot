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
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError

from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
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


# ── Workflow cloning ───────────────────────────────────────────────
#
# When a workflow is cloned to a new version, all step resources must
# be copied to the new steps. Without this, cloned workflows silently
# lose their weather files and model templates.


@pytest.mark.django_db
def test_clone_preserves_catalog_reference_resources():
    """Cloning a workflow must copy catalog-reference resources to the
    new step. The new resource should point to the same shared
    ValidatorResourceFile (it's a shared library item, not a copy).
    """
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow)
    vrf = ValidatorResourceFileFactory()
    WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=vrf,
    )

    cloned = workflow.clone_to_new_version(user=workflow.user)

    cloned_step = cloned.steps.first()
    assert cloned_step is not None
    assert cloned_step.step_resources.count() == 1
    cloned_res = cloned_step.step_resources.first()
    assert cloned_res.role == WorkflowStepResource.WEATHER_FILE
    assert cloned_res.is_catalog_reference is True
    assert cloned_res.validator_resource_file == vrf


@pytest.mark.django_db
def test_clone_preserves_step_owned_file_resources():
    """Cloning a workflow must copy step-owned file resources with a
    fresh copy of the file content. The original and clone should have
    independent file records so deleting one doesn't affect the other.
    """
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow)
    original_content = b"! IDF template content"
    WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        step_resource_file=SimpleUploadedFile("template.idf", original_content),
        filename="template.idf",
        resource_type="ENERGYPLUS_IDF",
    )

    cloned = workflow.clone_to_new_version(user=workflow.user)

    cloned_step = cloned.steps.first()
    assert cloned_step is not None
    assert cloned_step.step_resources.count() == 1
    cloned_res = cloned_step.step_resources.first()
    assert cloned_res.role == WorkflowStepResource.MODEL_TEMPLATE
    assert cloned_res.is_step_owned is True
    assert cloned_res.filename == "template.idf"
    assert cloned_res.resource_type == "ENERGYPLUS_IDF"
    # File content is an independent copy
    cloned_res.step_resource_file.open("rb")
    assert cloned_res.step_resource_file.read() == original_content
    cloned_res.step_resource_file.close()


@pytest.mark.django_db
def test_clone_preserves_multiple_resources_per_step():
    """A step with both a weather file (catalog) and template (step-owned)
    should have both resources copied to the cloned step. This is the
    expected configuration for EnergyPlus template-mode workflows.
    """
    workflow = WorkflowFactory()
    step = WorkflowStepFactory(workflow=workflow)
    vrf = ValidatorResourceFileFactory()
    WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=vrf,
    )
    WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        step_resource_file=SimpleUploadedFile("template.idf", b"! IDF"),
        filename="template.idf",
    )

    cloned = workflow.clone_to_new_version(user=workflow.user)

    cloned_step = cloned.steps.first()
    expected_count = 2
    assert cloned_step.step_resources.count() == expected_count
    assert cloned_step.step_resources.filter(
        role=WorkflowStepResource.WEATHER_FILE,
    ).exists()
    assert cloned_step.step_resources.filter(
        role=WorkflowStepResource.MODEL_TEMPLATE,
    ).exists()


# ── Storage cleanup on deletion ──────────────────────────────────────
#
# Django's FileField does NOT auto-delete files from storage when a
# model instance is deleted.  A ``post_delete`` signal on
# ``WorkflowStepResource`` handles cleanup for step-owned files.
# Without this, deleting step resources (via CASCADE, template
# replacement, or explicit delete) would leave orphaned files in
# storage.


@pytest.mark.django_db
def test_step_owned_file_deleted_from_storage_on_instance_delete():
    """When a step-owned ``WorkflowStepResource`` is deleted, the
    physical file should be removed from the storage backend.

    This verifies that the ``post_delete`` signal correctly calls
    ``FileField.delete(save=False)`` for step-owned files.
    """
    step = WorkflowStepFactory()
    resource = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        step_resource_file=SimpleUploadedFile("cleanup_test.idf", b"! IDF content"),
        filename="cleanup_test.idf",
    )
    # Capture the file path before deletion
    file_name = resource.step_resource_file.name
    storage = resource.step_resource_file.storage

    assert storage.exists(file_name), "File should exist before deletion"

    resource.delete()

    assert not storage.exists(file_name), (
        "File should be removed from storage after WorkflowStepResource deletion"
    )


@pytest.mark.django_db
def test_catalog_reference_deletion_does_not_crash():
    """Deleting a catalog-reference resource (no step-owned file)
    should not crash the ``post_delete`` signal handler.

    Catalog references have ``step_resource_file=""`` — the signal
    handler must handle this gracefully (no file to delete).
    """
    resource = WorkflowStepResourceFactory()
    assert resource.is_catalog_reference

    # Should not raise
    resource.delete()
    assert not WorkflowStepResource.objects.filter(pk=resource.pk).exists()


@pytest.mark.django_db
def test_cascade_delete_cleans_up_step_owned_files():
    """When a WorkflowStep is deleted (CASCADE), its step-owned
    resource files should also be cleaned from storage.

    This is the most common deletion path — steps are deleted when
    workflows are deleted or when steps are removed by the author.
    """
    step = WorkflowStepFactory()
    resource = WorkflowStepResource.objects.create(
        step=step,
        role=WorkflowStepResource.MODEL_TEMPLATE,
        step_resource_file=SimpleUploadedFile("cascade_test.idf", b"! IDF"),
        filename="cascade_test.idf",
    )
    file_name = resource.step_resource_file.name
    storage = resource.step_resource_file.storage

    assert storage.exists(file_name)

    step.delete()  # CASCADE deletes the resource

    assert not storage.exists(file_name), (
        "Step-owned file should be cleaned up when the parent step is deleted"
    )
