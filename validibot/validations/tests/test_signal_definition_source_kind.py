"""
Tests for ``source_kind`` and ``is_path_editable`` on ``SignalDefinition``.

These fields were added to make the signal's data source explicit in the UI.
``source_kind`` declares whether the value comes from a payload path or the
validator's own internal extraction mechanism. ``is_path_editable`` controls
whether the workflow author can change the source path in the step binding.

This test suite covers default values, explicit assignment, and the
interaction between these fields and existing signal behaviours.
"""

from django.test import TestCase

from validibot.validations.constants import SignalSourceKind
from validibot.validations.tests.factories import SignalDefinitionFactory


class TestSignalDefinitionSourceKindDefaults(TestCase):
    """Verify that new SignalDefinition rows get sensible defaults.

    Most signals should default to PAYLOAD_PATH + editable, which is the
    safe default for user-created and FMU input signals. Only system
    validators explicitly set INTERNAL + non-editable via their configs.
    """

    def test_default_source_kind_is_payload_path(self):
        """A new signal should default to PAYLOAD_PATH because most signals
        get their value from submission data that the author configures.
        """
        sig = SignalDefinitionFactory()
        self.assertEqual(sig.source_kind, SignalSourceKind.PAYLOAD_PATH)

    def test_default_is_path_editable_is_true(self):
        """A new signal should default to path-editable so workflow authors
        can wire it to the appropriate payload path for their data format.
        """
        sig = SignalDefinitionFactory()
        self.assertTrue(sig.is_path_editable)


class TestSignalDefinitionSourceKindExplicit(TestCase):
    """Verify that source_kind and is_path_editable can be set explicitly.

    System validators (EnergyPlus, THERM) set these to INTERNAL + False
    in their configs. This tests that the fields accept those values.
    """

    def test_internal_source_kind(self):
        """A signal can be marked as INTERNAL when the validator controls
        how the value is extracted (e.g., EnergyPlus simulation metrics).
        """
        sig = SignalDefinitionFactory(
            source_kind=SignalSourceKind.INTERNAL,
        )
        sig.refresh_from_db()
        self.assertEqual(sig.source_kind, SignalSourceKind.INTERNAL)

    def test_not_path_editable(self):
        """A signal can be marked as non-editable when the validator controls
        the extraction path and the author should not change it.
        """
        sig = SignalDefinitionFactory(is_path_editable=False)
        sig.refresh_from_db()
        self.assertFalse(sig.is_path_editable)

    def test_internal_and_not_editable_together(self):
        """INTERNAL signals are typically non-editable — verify the
        combination works correctly (the common case for EnergyPlus).
        """
        sig = SignalDefinitionFactory(
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        )
        sig.refresh_from_db()
        self.assertEqual(sig.source_kind, SignalSourceKind.INTERNAL)
        self.assertFalse(sig.is_path_editable)

    def test_get_source_kind_display(self):
        """The human-readable display for source_kind should work for both
        values, since the UI shows this as a badge in the signal edit modal.
        """
        sig_payload = SignalDefinitionFactory(
            source_kind=SignalSourceKind.PAYLOAD_PATH,
        )
        sig_internal = SignalDefinitionFactory(
            source_kind=SignalSourceKind.INTERNAL,
        )
        self.assertEqual(sig_payload.get_source_kind_display(), "Payload Path")
        self.assertEqual(sig_internal.get_source_kind_display(), "Internal")
