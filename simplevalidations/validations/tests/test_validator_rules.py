from http import HTTPStatus

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
from simplevalidations.validations.utils import create_custom_validator


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

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Default assertions for" in html
    assert rule.name in html
    assert "payload.value" in html
    assert "View Validator Assertions" in html


@pytest.mark.django_db
def test_default_assertion_allows_boolean_literal(client):
    ensure_all_roles_exist()
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.AUTHOR)
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    validator = ValidatorFactory(org=org, is_system=False)
    ValidatorCatalogEntryFactory(validator=validator, slug="bool_in")

    response = client.post(
        reverse(
            "validations:validator_rule_create",
            kwargs={"pk": validator.pk},
        ),
        data={
            "name": "Bool check",
            "description": "",
            "rule_type": ValidatorRuleType.CEL_EXPRESSION,
            "cel_expression": "bool_in == true",
            "order": 0,
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    rule = validator.rules.get(name="Bool check")
    assert rule.expression == "bool_in == true"
    linked_entries = list(
        ValidatorCatalogRuleEntry.objects.filter(rule=rule).values_list(
            "catalog_entry__slug",
            flat=True,
        ),
    )
    assert linked_entries == ["bool_in"]


@pytest.mark.django_db
def test_default_assertion_move_reorders(client):
    ensure_all_roles_exist()
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.AUTHOR)
    user.set_current_org(org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    validator = create_custom_validator(
        org=org,
        user=user,
        name="Movable",
        description="",
        custom_type="BASIC",
    ).validator
    validator.rules.create(
        name="First",
        rule_type=ValidatorRuleType.CEL_EXPRESSION,
        expression="payload.a == 1",
        order=10,
    )
    second = validator.rules.create(
        name="Second",
        rule_type=ValidatorRuleType.CEL_EXPRESSION,
        expression="payload.b == 2",
        order=20,
    )

    response = client.post(
        reverse(
            "validations:validator_rule_move",
            kwargs={"pk": validator.pk, "rule_pk": second.pk},
        ),
        data={"direction": "up"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    names = list(validator.rules.order_by("order").values_list("name", flat=True))
    assert names[0] == "Second"
    assert names[1] == "First"


@pytest.mark.django_db
def test_author_not_creator_cannot_move(client):
    ensure_all_roles_exist()
    org = OrganizationFactory()
    creator = UserFactory()
    grant_role(creator, org, RoleCode.AUTHOR)
    creator.set_current_org(org)
    non_creator = UserFactory()
    grant_role(non_creator, org, RoleCode.AUTHOR)
    non_creator.set_current_org(org)

    created = create_custom_validator(
        org=org,
        user=creator,
        name="Movable",
        description="",
        custom_type="BASIC",
    ).validator
    rule = created.rules.create(
        name="Only creator",
        rule_type=ValidatorRuleType.CEL_EXPRESSION,
        expression="payload.a == 1",
        order=10,
    )

    client.force_login(non_creator)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.post(
        reverse(
            "validations:validator_rule_move",
            kwargs={"pk": created.pk, "rule_pk": rule.pk},
        ),
        data={"direction": "up"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
