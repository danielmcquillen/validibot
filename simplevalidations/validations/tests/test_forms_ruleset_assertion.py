from __future__ import annotations

from django.test import TestCase

from simplevalidations.validations.constants import (
    AssertionType,
    CatalogRunStage,
    Severity,
    ValidationType,
)
from simplevalidations.validations.forms import RulesetAssertionForm
from simplevalidations.validations.tests.factories import (
    RulesetFactory,
    ValidatorCatalogEntryFactory,
    ValidatorFactory,
)
from simplevalidations.validations.utils import update_custom_validator


class RulesetAssertionFormTests(TestCase):
    def _form(self, *, validator, catalog_entries, data: dict):
        return RulesetAssertionForm(
            data=data,
            catalog_entries=catalog_entries,
            validator=validator,
        )

    def test_cel_disallows_unknown_identifiers_when_custom_targets_disabled(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=False,
        )
        validator.refresh_from_db()
        self.assertFalse(validator.allow_custom_assertion_targets)
        entry = ValidatorCatalogEntryFactory(validator=validator, slug="price")
        ruleset = RulesetFactory()
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_field": entry.slug,
                "severity": Severity.ERROR,
                "cel_expression": "price > 0 && rating > 10",
                "when_expression": "",
            },
        )
        self.assertFalse(form._validator_allows_custom_targets())
        self.assertFalse(form.is_valid())
        self.assertIn("Unknown signal(s) referenced", str(form.errors))

    def test_cel_allows_unknown_identifiers_when_custom_targets_enabled(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        validator.__class__.objects.filter(pk=validator.pk).update(
            allow_custom_assertion_targets=True,
        )
        validator.refresh_from_db()
        self.assertTrue(validator.allow_custom_assertion_targets)
        entry = ValidatorCatalogEntryFactory(validator=validator, slug="price")
        form = self._form(
            validator=validator,
            catalog_entries=[entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_field": entry.slug,
                "severity": Severity.ERROR,
                "cel_expression": "price > 0 && rating > 10",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())

    def test_update_custom_validator_persists_validator_fields(self):
        from simplevalidations.validations.tests.factories import CustomValidatorFactory

        custom = CustomValidatorFactory()
        updated = update_custom_validator(
            custom,
            name="New Name",
            description="New Desc",
            notes="New Notes",
            version="9.9",
            allow_custom_assertion_targets=True,
            supported_data_formats=["json"],
        )
        updated.validator.refresh_from_db()
        self.assertEqual(updated.validator.name, "New Name")
        self.assertEqual(updated.validator.description, "New Desc")
        self.assertEqual(updated.validator.version, "9.9")
        self.assertTrue(updated.validator.allow_custom_assertion_targets)
        self.assertEqual(updated.validator.supported_data_formats, ["json"])
        self.assertEqual(updated.notes, "New Notes")

    def test_target_resolution_prefers_input_without_prefix(self):
        validator = ValidatorFactory(validation_type=ValidationType.BASIC, is_system=False)
        input_entry = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="temperature",
            run_stage=CatalogRunStage.INPUT,
        )
        form = self._form(
            validator=validator,
            catalog_entries=[input_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_field": "temperature",
                "severity": Severity.ERROR,
                "cel_expression": "temperature > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())
        self.assertIsNone(form.cleaned_data["target_catalog_entry"])

    def test_output_requires_prefix_on_collision(self):
        validator = ValidatorFactory(validation_type=ValidationType.BASIC, is_system=False)
        input_entry = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="price",
            run_stage=CatalogRunStage.INPUT,
        )
        output_entry = ValidatorCatalogEntryFactory.build(
            validator=validator,
            slug="price",
            run_stage=CatalogRunStage.OUTPUT,
        )

        form = self._form(
            validator=validator,
            catalog_entries=[input_entry, output_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_field": "price",
                "severity": Severity.ERROR,
                "cel_expression": "price > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form.is_valid())

        form_prefixed = self._form(
            validator=validator,
            catalog_entries=[input_entry, output_entry],
            data={
                "assertion_type": AssertionType.CEL_EXPRESSION.value,
                "target_field": "output.price",
                "severity": Severity.ERROR,
                "cel_expression": "output.price > 0",
                "when_expression": "",
            },
        )
        self.assertTrue(form_prefixed.is_valid())
        self.assertIsNone(form_prefixed.cleaned_data["target_catalog_entry"])
