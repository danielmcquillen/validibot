import pytest

from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.providers import get_provider_for_validator
from simplevalidations.validations.tests.factories import ValidatorFactory


@pytest.mark.django_db
def test_energyplus_provider_syncs_catalog_entries():
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    provider = get_provider_for_validator(validator)
    assert provider is not None
    created, existing = provider.ensure_catalog_entries()
    assert created >= 1
    # Second call should not duplicate
    created_again, existing_again = provider.ensure_catalog_entries()
    assert created_again == 0
    assert existing_again >= 1
    slugs = set(validator.catalog_entries.values_list("slug", flat=True))
    assert "facility_electric_demand_w" in slugs
