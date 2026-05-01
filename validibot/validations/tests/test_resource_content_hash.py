"""Tests for resource-file content hashing + drift detection.

ADR-2026-04-27 Phase 3 Session C, task 11: catalog files
(``ValidatorResourceFile``) and step-owned files
(``WorkflowStepResource.step_resource_file``) referenced by locked
or used workflows must not silently mutate. The mechanism:

1. Both models store a SHA-256 ``content_hash`` of their file's bytes.
2. The hash is recomputed on every save.
3. If a save would change the hash AND the resource is referenced by
   a locked / used workflow, save() raises ``ValidationError``.

The tests below pin both halves: the *positive* path (hash gets
populated, mutation works on uncommitted resources) and the
*negative* path (locked-workflow ownership rejects mutation).
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase

from validibot.core.filesafety import sha256_field_file
from validibot.core.filesafety import sha256_hexdigest
from validibot.validations.constants import ResourceFileType
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


# ──────────────────────────────────────────────────────────────────────
# sha256_field_file helper
# ──────────────────────────────────────────────────────────────────────
#
# Pure-helper unit tests, separate from the model integration tests
# below. These pin the contract that the helper preserves the file's
# read position so it can be re-read by Django's storage.save() after
# we finish hashing.


class Sha256FieldFileTests(TestCase):
    """The helper hashes content without leaving the file in a bad state."""

    def test_returns_empty_hash_for_empty_field(self):
        """Unset / empty FieldFile -> SHA-256 of empty bytes."""

        class FakeField:
            name = ""

            def read(self, n):  # pragma: no cover - never reached
                raise AssertionError("read should not be called for empty file")

        digest = sha256_field_file(FakeField())
        assert digest == sha256_hexdigest(b"")

    def test_hashes_simple_byte_payload(self):
        """Bytes -> known hex digest."""
        # Use a real Django ContentFile so we exercise the same .read()
        # interface as production.
        cf = ContentFile(b"hello", name="hello.txt")
        digest = sha256_field_file(cf)
        assert digest == sha256_hexdigest(b"hello")

    def test_preserves_file_position(self):
        """Hashing must leave the file at its original read position.

        Critical for Django's ``Model.save()`` flow: we hash before
        ``super().save()``, which then re-reads the file via
        ``storage.save()``. If the helper left the cursor at EOF,
        the file would be persisted as zero bytes.
        """
        cf = ContentFile(b"abc123", name="x.txt")
        # Move position so we can verify the helper restores it,
        # not just naively rewinds to zero. Named to avoid PLR2004
        # ("magic value used in comparison") and to document the
        # contract: "we asked for offset N; offset N is what we get back."
        starting_offset = 2
        cf.seek(starting_offset)
        sha256_field_file(cf)
        # Should be back at the position we left it.
        assert cf.tell() == starting_offset

    def test_does_not_close_file(self):
        """The helper must NOT close the file — uploaded buffers die when closed."""
        cf = ContentFile(b"keep-me-open", name="alive.txt")
        sha256_field_file(cf)
        # If closed, this would raise ValueError("I/O operation on closed file").
        cf.seek(0)
        assert cf.read() == b"keep-me-open"


# ──────────────────────────────────────────────────────────────────────
# ValidatorResourceFile.content_hash + drift gate
# ──────────────────────────────────────────────────────────────────────


class ValidatorResourceFileHashPopulationTests(TestCase):
    """First save populates ``content_hash`` from the file's bytes."""

    def test_first_save_populates_hash(self):
        """A newly-created resource gets a non-empty content_hash."""
        validator = ValidatorFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Test EPW",
            filename="test.epw",
            file=ContentFile(b"weather data v1", name="test.epw"),
        )
        assert resource.content_hash == sha256_hexdigest(b"weather data v1")

    def test_re_save_with_unchanged_bytes_keeps_hash(self):
        """Saving without changing the file is idempotent."""
        validator = ValidatorFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Test",
            filename="test.epw",
            file=ContentFile(b"v1", name="test.epw"),
        )
        first_hash = resource.content_hash
        resource.name = "Renamed"  # cosmetic field, not a content change
        resource.save()
        resource.refresh_from_db()
        assert resource.content_hash == first_hash


class ValidatorResourceFileDriftGateTests(TestCase):
    """Locked-workflow + content change -> ValidationError."""

    def _make_locked_resource(self) -> ValidatorResourceFile:
        validator = ValidatorFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Locked Resource",
            filename="locked.epw",
            file=ContentFile(b"original bytes", name="locked.epw"),
        )
        # Wire it to a locked workflow.
        workflow = WorkflowFactory(is_locked=True)
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=resource,
        )
        return resource

    def test_drift_raises_when_used_by_locked_workflow(self):
        """Replacing the file's bytes on a locked-workflow resource raises."""
        resource = self._make_locked_resource()
        resource.file = ContentFile(b"tampered bytes", name="locked.epw")
        with pytest.raises(ValidationError) as exc:
            resource.save()
        assert "file" in exc.value.message_dict

    def test_no_drift_when_not_used_by_locked_workflow(self):
        """Same content change on an unused resource -> save succeeds."""
        validator = ValidatorFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Unused",
            filename="unused.epw",
            file=ContentFile(b"v1", name="unused.epw"),
        )
        # No workflow attached -> not "in use".
        resource.file = ContentFile(b"v2", name="unused.epw")
        resource.save()
        resource.refresh_from_db()
        assert resource.content_hash == sha256_hexdigest(b"v2")

    def test_is_used_by_locked_workflow_false_for_orphan(self):
        """No step references -> not in use."""
        validator = ValidatorFactory()
        resource = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Orphan",
            filename="orphan.epw",
            file=ContentFile(b"x", name="orphan.epw"),
        )
        assert resource.is_used_by_locked_workflow() is False


# ──────────────────────────────────────────────────────────────────────
# WorkflowStepResource.content_hash (step-owned mode)
# ──────────────────────────────────────────────────────────────────────


class WorkflowStepResourceHashPopulationTests(TestCase):
    """Step-owned files populate ``content_hash``; catalog refs leave it blank."""

    def test_step_owned_save_populates_hash(self):
        """A step-owned file's first save sets content_hash."""
        workflow = WorkflowFactory()
        step = WorkflowStepFactory(workflow=workflow)
        resource = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(b"template v1", name="t.idf"),
            filename="t.idf",
            resource_type="MODEL_TEMPLATE",
        )
        assert resource.content_hash == sha256_hexdigest(b"template v1")

    def test_catalog_reference_leaves_hash_blank(self):
        """Catalog refs delegate hashing to ValidatorResourceFile."""
        validator = ValidatorFactory()
        catalog = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Catalog",
            filename="c.epw",
            file=ContentFile(b"catalog bytes", name="c.epw"),
        )
        workflow = WorkflowFactory()
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        resource = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=catalog,
        )
        # Catalog mode: hash is empty, the source row owns it.
        assert resource.content_hash == ""
        # And the catalog row's hash IS populated.
        assert catalog.content_hash == sha256_hexdigest(b"catalog bytes")


class WorkflowStepResourceDriftGateTests(TestCase):
    """Step-owned mutation on locked workflows -> ValidationError."""

    def test_step_owned_drift_raises_when_workflow_locked(self):
        """Replacing the file on a step-owned resource of a locked workflow raises."""
        workflow = WorkflowFactory(is_locked=True)
        step = WorkflowStepFactory(workflow=workflow)
        resource = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(b"v1", name="t.idf"),
            filename="t.idf",
            resource_type="MODEL_TEMPLATE",
        )
        resource.step_resource_file = ContentFile(b"tampered", name="t.idf")
        with pytest.raises(ValidationError) as exc:
            resource.save()
        assert "step_resource_file" in exc.value.message_dict

    def test_step_owned_change_allowed_on_unlocked_workflow(self):
        """Same change on an unlocked workflow -> save succeeds."""
        workflow = WorkflowFactory(is_locked=False)
        step = WorkflowStepFactory(workflow=workflow)
        resource = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(b"v1", name="t.idf"),
            filename="t.idf",
            resource_type="MODEL_TEMPLATE",
        )
        resource.step_resource_file = ContentFile(b"v2", name="t.idf")
        resource.save()
        resource.refresh_from_db()
        assert resource.content_hash == sha256_hexdigest(b"v2")
