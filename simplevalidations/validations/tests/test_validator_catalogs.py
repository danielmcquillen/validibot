import pytest

from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.tests.factories import (
    CustomValidatorFactory,
)
from simplevalidations.validations.tests.factories import (
    ValidatorCatalogEntryFactory,
)
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.validations.forms import ValidatorCatalogEntryForm
from simplevalidations.validations.tests.factories import RulesetFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db
def test_validator_catalog_entries_grouped():
    validator = ValidatorFactory(validation_type=ValidationType.CUSTOM_VALIDATOR)
    ValidatorCatalogEntryFactory(
        validator=validator,
        entry_type=CatalogEntryType.SIGNAL,
        run_stage=CatalogRunStage.INPUT,
        slug="floor-area",
    )
    ValidatorCatalogEntryFactory(
        validator=validator,
        entry_type=CatalogEntryType.SIGNAL,
        run_stage=CatalogRunStage.OUTPUT,
        slug="electric-demand",
    )
    grouped = validator.catalog_entries_by_type()
    assert len(grouped[CatalogEntryType.SIGNAL]) == 2
    stage_grouped = validator.catalog_entries_by_stage()
    assert len(stage_grouped[CatalogRunStage.INPUT]) == 1
    assert stage_grouped[CatalogRunStage.INPUT][0].slug == "floor-area"
    assert len(stage_grouped[CatalogRunStage.OUTPUT]) == 1


@pytest.mark.django_db
def test_custom_validator_sets_org_on_validator():
    org = OrganizationFactory()
    validator = ValidatorFactory(
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        org=None,
        is_system=False,
    )
    CustomValidatorFactory(org=org, validator=validator)
    validator.refresh_from_db()
    assert validator.org == org
    assert validator.is_custom
    assert SubmissionFileType.YAML in validator.supported_file_types


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


@pytest.mark.django_db
def test_signal_name_must_be_slug_format():
    validator = ValidatorFactory(is_system=False)
    form = ValidatorCatalogEntryForm(
        data={
            "run_stage": CatalogRunStage.INPUT,
            "slug": "My Variable",
            "target_field": "payload.value",
            "label": "",
            "data_type": "number",
        },
        validator=validator,
    )
    assert not form.is_valid()
    assert "slug" in form.errors
    assert "Try: my-variable" in form.errors["slug"][0]


@pytest.mark.django_db
def test_signal_name_unique_across_stages():
    validator = ValidatorFactory(is_system=False)
    ValidatorCatalogEntryFactory(
        validator=validator,
        run_stage=CatalogRunStage.INPUT,
        slug="boo",
    )
    form = ValidatorCatalogEntryForm(
        data={
            "run_stage": CatalogRunStage.OUTPUT,
            "slug": "boo",
            "target_field": "output.boo",
            "label": "",
            "data_type": "number",
        },
        validator=validator,
    )
    assert not form.is_valid()
    assert "unique" in form.errors["slug"][0].lower() or "must be unique" in form.errors["slug"][0].lower()


@pytest.mark.django_db
def test_signal_create_modal_returns_errors_in_htmx():
    org = OrganizationFactory()
    from django.test import Client
    from django.urls import reverse
    from simplevalidations.users.constants import RoleCode
    from simplevalidations.users.tests.factories import UserFactory, grant_role

    user = UserFactory()
    grant_role(user, org, RoleCode.AUTHOR)
    user.set_current_org(org)
    validator = ValidatorFactory(org=org, is_system=False)

    client = Client()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()

    response = client.post(
        reverse("validations:validator_signal_create", kwargs={"pk": validator.pk}),
        data={
            "run_stage": CatalogRunStage.INPUT,
            "slug": "",
            "target_field": "",
            "data_type": "number",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "modal-signal-create" in html
    assert "Signal name is required" in html
