import pytest

from django.core.exceptions import ValidationError
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.users.tests.utils import ensure_all_roles_exist
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import ValidatorRuleType
from simplevalidations.validations.models import ValidatorCatalogRuleEntry
from simplevalidations.validations.tests.factories import ValidatorCatalogEntryFactory
from simplevalidations.validations.tests.factories import ValidatorFactory


@pytest.mark.django_db
def test_default_assertion_links_signals_and_prevents_signal_delete():
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


@pytest.mark.django_db
def test_default_assertions_modal_lists_rules(client):
    ensure_all_roles_exist()
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.AUTHOR)
    user.set_current_org(org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()
    validator = ValidatorFactory(
        org=org,
        is_system=False,
        validation_type=ValidationType.BASIC,
        slug="default-assertions-validator",
    )
    rule = validator.rules.create(
        name="Always positive",
        rule_type=ValidatorRuleType.CEL_EXPRESSION,
        expression="payload.value >= 0",
        order=1,
    )

    url = reverse(
        "validations:validator_default_assertions",
        kwargs={"slug": validator.slug},
    )
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    html = response.content.decode()
    assert "Default assertions for" in html
    assert rule.name in html
    assert "payload.value" in html
    assert "View Validator Assertions" in html
