"""
Tests for ``source_kind`` and ``is_path_editable`` on ``StepIODefinition``.

These fields make a step I/O definition's data source explicit in the UI.
``source_kind`` declares whether the value comes from a payload path or the
validator's own internal extraction mechanism. ``is_path_editable`` controls
whether the workflow author can change the source path in the step binding.

This test suite covers default values, explicit assignment, and the
interaction between these fields and existing step I/O definition behaviours.
"""

from django.test import TestCase

from validibot.validations.constants import StepIOSourceKind
from validibot.validations.tests.factories import StepIODefinitionFactory


class TestStepIODefinitionSourceKindDefaults(TestCase):
    """Verify that new StepIODefinition rows get sensible defaults.

    Most step I/O definitions should default to PAYLOAD_PATH + editable, which is the
    safe default for user-created and FMU step inputs. Only system
    validators explicitly set INTERNAL + non-editable via their configs.
    """

    def test_default_source_kind_is_payload_path(self):
        """A new definition defaults to PAYLOAD_PATH.

        Most step inputs get their value from author-configured submission data.
        """
        io_definition = StepIODefinitionFactory()
        self.assertEqual(io_definition.source_kind, StepIOSourceKind.PAYLOAD_PATH)

    def test_default_is_path_editable_is_true(self):
        """A new step I/O definition should default to path-editable so workflow authors
        can wire it to the appropriate payload path for their data format.
        """
        io_definition = StepIODefinitionFactory()
        self.assertTrue(io_definition.is_path_editable)


class TestStepIODefinitionSourceKindExplicit(TestCase):
    """Verify that source_kind and is_path_editable can be set explicitly.

    System validators (EnergyPlus, THERM) set these to INTERNAL + False
    in their configs. This tests that the fields accept those values.
    """

    def test_internal_source_kind(self):
        """A step I/O definition can be marked as INTERNAL when the validator controls
        how the value is extracted (e.g., EnergyPlus simulation metrics).
        """
        io_definition = StepIODefinitionFactory(
            source_kind=StepIOSourceKind.INTERNAL,
        )
        io_definition.refresh_from_db()
        self.assertEqual(io_definition.source_kind, StepIOSourceKind.INTERNAL)

    def test_not_path_editable(self):
        """A definition can be non-editable when its validator controls extraction."""
        io_definition = StepIODefinitionFactory(is_path_editable=False)
        io_definition.refresh_from_db()
        self.assertFalse(io_definition.is_path_editable)

    def test_internal_and_not_editable_together(self):
        """INTERNAL step I/O definitions are typically non-editable — verify the
        combination works correctly (the common case for EnergyPlus).
        """
        io_definition = StepIODefinitionFactory(
            source_kind=StepIOSourceKind.INTERNAL,
            is_path_editable=False,
        )
        io_definition.refresh_from_db()
        self.assertEqual(io_definition.source_kind, StepIOSourceKind.INTERNAL)
        self.assertFalse(io_definition.is_path_editable)

    def test_get_source_kind_display(self):
        """The human-readable display for source_kind should work for both
        values, since the step-I/O edit modal shows it as a badge.
        """
        payload_definition = StepIODefinitionFactory(
            source_kind=StepIOSourceKind.PAYLOAD_PATH,
        )
        internal_definition = StepIODefinitionFactory(
            source_kind=StepIOSourceKind.INTERNAL,
        )
        self.assertEqual(payload_definition.get_source_kind_display(), "Payload Path")
        self.assertEqual(internal_definition.get_source_kind_display(), "Internal")
