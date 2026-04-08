"""
Tests for ``SignalBindingEditForm`` behaviour related to ``is_path_editable``.

When a signal has ``is_path_editable=False``, the source data path field
should be disabled in the form — preventing workflow authors from changing
a path that the validator controls internally (e.g., EnergyPlus metrics).

Django's ``field.disabled = True`` is a server-side protection: even if
submitted data includes a value for the field, Django ignores it.
"""

from django.test import TestCase

from validibot.validations.constants import SignalSourceKind
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import StepSignalBindingFactory
from validibot.workflows.forms import SignalBindingEditForm


class TestSourceDataPathEditability(TestCase):
    """Verify that is_path_editable controls the source_data_path field state."""

    def test_source_data_path_disabled_when_not_editable(self):
        """When the signal's is_path_editable is False, the source_data_path
        field should be disabled so the author cannot change the validator's
        internal extraction path.
        """
        sig = SignalDefinitionFactory(
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        )
        binding = StepSignalBindingFactory(
            signal_definition=sig,
            source_data_path="metrics.site_eui",
        )
        form = SignalBindingEditForm(
            signal_definition=sig,
            binding=binding,
        )
        self.assertTrue(form.fields["source_data_path"].disabled)

    def test_source_data_path_enabled_when_editable(self):
        """When the signal's is_path_editable is True (the default), the
        source_data_path field should be enabled so the author can wire
        the signal to the appropriate payload path.
        """
        sig = SignalDefinitionFactory(
            source_kind=SignalSourceKind.PAYLOAD_PATH,
            is_path_editable=True,
        )
        binding = StepSignalBindingFactory(
            signal_definition=sig,
            source_data_path="building.floor_area",
        )
        form = SignalBindingEditForm(
            signal_definition=sig,
            binding=binding,
        )
        self.assertFalse(form.fields["source_data_path"].disabled)

    def test_disabled_field_ignores_submitted_value(self):
        """When source_data_path is disabled, submitting a new value should
        not overwrite the existing binding path — Django's disabled field
        behaviour ensures the form ignores the submitted value.
        """
        sig = SignalDefinitionFactory(
            source_kind=SignalSourceKind.INTERNAL,
            is_path_editable=False,
        )
        binding = StepSignalBindingFactory(
            signal_definition=sig,
            source_data_path="metrics.original_path",
        )
        form = SignalBindingEditForm(
            data={
                "label": sig.label,
                "description": sig.description,
                "unit": sig.unit,
                "source_data_path": "attacker.injected.path",
                "default_value": "",
                "is_required": True,
            },
            signal_definition=sig,
            binding=binding,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        binding.refresh_from_db()
        # The binding path should remain unchanged because the field was disabled.
        self.assertEqual(binding.source_data_path, "metrics.original_path")
