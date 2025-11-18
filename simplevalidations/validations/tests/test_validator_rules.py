import pytest

from django.core.exceptions import ValidationError

from simplevalidations.validations.constants import ValidatorRuleType
from simplevalidations.validations.models import ValidatorCatalogRuleEntry
from simplevalidations.validations.tests.factories import ValidatorCatalogEntryFactory
from simplevalidations.validations.tests.factories import ValidatorFactory


@pytest.mark.django_db
def test_rule_links_signals_and_prevents_signal_delete():
    validator = ValidatorFactory()
    signal = ValidatorCatalogEntryFactory(validator=validator, slug="foo")
    rule = validator.rules.create(
        name="Sample",
        rule_type=ValidatorRuleType.CEL_EXPRESSION,
        expression="foo > 0",
        order=0,
    )
    ValidatorCatalogRuleEntry.objects.create(rule=rule, catalog_entry=signal)

    with pytest.raises(ValidationError):
        signal.delete()

    rule.delete()
    # After rule deletion the signal can be removed.
    signal.delete()
