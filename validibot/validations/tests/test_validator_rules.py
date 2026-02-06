from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorRuleType
from validibot.validations.models import RulesetAssertion
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.utils import create_custom_validator


@pytest.mark.django_db
def test_default_assertion_links_signals_and_prevents_signal_delete():
    """Default assertions on default_ruleset protect referenced catalog entries."""
    validator = ValidatorFactory()
    signal = ValidatorCatalogEntryFactory(validator=validator, slug="foo")
    default_ruleset = validator.ensure_default_ruleset()
    RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_catalog_entry=signal,
        rhs={"expr": "foo > 0"},
        severity=Severity.ERROR,
        order=0,
        message_template="Sample",
        cel_cache="foo > 0",
    )

    # Signal is protected by the assertion reference (PROTECT on FK)
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        signal.delete()

    # After deleting all assertions, the signal can be removed.
    default_ruleset.assertions.all().delete()
    signal.delete()


@pytest.mark.django_db
def test_default_assertions_modal_lists_rules(client):
    """The default assertions modal shows assertions from default_ruleset."""
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
    default_ruleset = validator.ensure_default_ruleset()
    RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_field="payload.value >= 0",
        rhs={"expr": "payload.value >= 0"},
        severity=Severity.ERROR,
        order=1,
        message_template="Always positive",
        cel_cache="payload.value >= 0",
    )

    url = reverse(
        "validations:validator_default_assertions",
        kwargs={"slug": validator.slug},
    )
    response = client.get(url, HTTP_HX_REQUEST="true")

    assert response.status_code == HTTPStatus.OK
    html = response.content.decode()
    assert "Default assertions for" in html
    assert "Always positive" in html
    assert "payload.value" in html
    assert "View Validator Assertions" in html


@pytest.mark.django_db
def test_default_assertion_allows_boolean_literal(client):
    """Creating a default assertion with boolean literals works via the API."""
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
    default_ruleset = validator.default_ruleset
    assertion = default_ruleset.assertions.get(message_template="Bool check")
    assert assertion.rhs["expr"] == "bool_in == true"
    assert assertion.target_catalog_entry is not None
    assert assertion.target_catalog_entry.slug == "bool_in"


@pytest.mark.django_db
def test_default_assertion_move_reorders(client):
    """Moving default assertions up/down reorders them on default_ruleset."""
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
    default_ruleset = validator.ensure_default_ruleset()
    RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_field="payload.a == 1",
        rhs={"expr": "payload.a == 1"},
        severity=Severity.ERROR,
        order=10,
        message_template="First",
        cel_cache="payload.a == 1",
    )
    second = RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_field="payload.b == 2",
        rhs={"expr": "payload.b == 2"},
        severity=Severity.ERROR,
        order=20,
        message_template="Second",
        cel_cache="payload.b == 2",
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
    names = list(
        default_ruleset.assertions.order_by("order").values_list(
            "message_template",
            flat=True,
        ),
    )
    assert names[0] == "Second"
    assert names[1] == "First"


@pytest.mark.django_db
def test_author_not_creator_cannot_move(client):
    """Only the creator of a custom validator can move its default assertions."""
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
    default_ruleset = created.ensure_default_ruleset()
    assertion = RulesetAssertion.objects.create(
        ruleset=default_ruleset,
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_field="payload.a == 1",
        rhs={"expr": "payload.a == 1"},
        severity=Severity.ERROR,
        order=10,
        message_template="Only creator",
        cel_cache="payload.a == 1",
    )

    client.force_login(non_creator)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.post(
        reverse(
            "validations:validator_rule_move",
            kwargs={"pk": created.pk, "rule_pk": assertion.pk},
        ),
        data={"direction": "up"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
