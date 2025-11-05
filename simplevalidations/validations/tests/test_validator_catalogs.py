import pytest

from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.tests.factories import (
    CustomValidatorFactory,
)
from simplevalidations.validations.tests.factories import (
    ValidatorCatalogEntryFactory,
)
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.validations.tests.factories import RulesetFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db
def test_validator_catalog_entries_grouped():
    validator = ValidatorFactory(validation_type=ValidationType.CUSTOM_RULES)
    ValidatorCatalogEntryFactory(
        validator=validator,
        entry_type=CatalogEntryType.SIGNAL_INPUT,
        slug="floor-area",
    )
    ValidatorCatalogEntryFactory(
        validator=validator,
        entry_type=CatalogEntryType.SIGNAL_OUTPUT,
        slug="electric-demand",
    )
    grouped = validator.catalog_entries_by_type()
    assert len(grouped[CatalogEntryType.SIGNAL_INPUT]) == 1
    assert grouped[CatalogEntryType.SIGNAL_INPUT][0].slug == "floor-area"
    assert len(grouped[CatalogEntryType.SIGNAL_OUTPUT]) == 1


@pytest.mark.django_db
def test_custom_validator_sets_org_on_validator():
    org = OrganizationFactory()
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_RULES,
        org=None,
        is_system=False,
    )
    CustomValidatorFactory(org=org, validator=validator)
    validator.refresh_from_db()
    assert validator.org == org
    assert validator.is_custom


def test_base_engine_exposes_default_helpers():
    class DummyEngine(BaseValidatorEngine):
        validation_type = ValidationType.JSON_SCHEMA

        def validate(self, validator, submission, ruleset):  # pragma: no cover - unused
            raise NotImplementedError

    helpers = DummyEngine().get_cel_helpers()
    assert "percentile" in helpers
    assert helpers["percentile"].return_type == "number"


@pytest.mark.django_db
def test_ruleset_validator_property_resolves_via_workflow_step():
    ruleset = RulesetFactory()
    validator = ValidatorFactory()
    WorkflowStepFactory(validator=validator, ruleset=ruleset)
    assert ruleset.validator == validator
