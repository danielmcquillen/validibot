import json

import factory
from factory.django import DjangoModelFactory

from validibot.core.models import CallbackReceiptStatus
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Artifact
from validibot.validations.models import CallbackReceipt
from validibot.validations.models import CustomValidator
from validibot.validations.models import Ruleset
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.models import default_supported_data_formats_for_validation
from validibot.validations.models import default_supported_file_types_for_validation
from validibot.workflows.tests.factories import WorkflowStepFactory


class RulesetFactory(DjangoModelFactory):
    class Meta:
        model = Ruleset

    org = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Test Ruleset {n}")
    ruleset_type = RulesetType.JSON_SCHEMA
    version = "1"

    @factory.lazy_attribute
    def rules_text(self):
        if self.ruleset_type == RulesetType.JSON_SCHEMA:
            return json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {},
                }
            )
        if self.ruleset_type == RulesetType.XML_SCHEMA:
            return "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"
        return ""

    @factory.lazy_attribute
    def metadata(self):
        if self.ruleset_type == RulesetType.JSON_SCHEMA:
            return {
                "schema_type": JSONSchemaVersion.DRAFT_2020_12.value,
            }
        if self.ruleset_type == RulesetType.XML_SCHEMA:
            return {
                "schema_type": XMLSchemaType.XSD.value,
            }
        return {}


class ValidatorFactory(DjangoModelFactory):
    class Meta:
        model = Validator
        skip_postgeneration_save = True

    slug = factory.Sequence(lambda n: f"test-validator-{n}")
    description = factory.Faker("text", max_nb_chars=200)
    name = factory.Sequence(lambda n: f"Test Validator {n}")
    validation_type = ValidationType.JSON_SCHEMA
    version = "1"
    default_ruleset = None
    org = None
    is_system = True
    allow_custom_assertion_targets = False
    has_processor = factory.LazyAttribute(
        lambda obj: (
            obj.validation_type
            in (
                ValidationType.ENERGYPLUS,
                ValidationType.FMU,
            )
        )
    )
    supported_data_formats = factory.LazyAttribute(
        lambda obj: default_supported_data_formats_for_validation(obj.validation_type),
    )
    supported_file_types = factory.LazyAttribute(
        lambda obj: default_supported_file_types_for_validation(obj.validation_type),
    )

    @factory.post_generation
    def ensure_defaults(self, create, extracted, **kwargs):
        """Align flags with the chosen validation type."""
        desired = self.allow_custom_assertion_targets
        if self.validation_type == ValidationType.BASIC:
            desired = True
        if desired != self.allow_custom_assertion_targets:
            self.allow_custom_assertion_targets = desired
            if create:
                self.save(update_fields=["allow_custom_assertion_targets"])


class ValidatorCatalogEntryFactory(DjangoModelFactory):
    class Meta:
        model = ValidatorCatalogEntry

    validator = factory.SubFactory(ValidatorFactory)
    entry_type = CatalogEntryType.SIGNAL
    run_stage = CatalogRunStage.INPUT
    slug = factory.Sequence(lambda n: f"signal-input-{n}")
    label = factory.LazyAttribute(lambda o: o.slug.replace("-", " ").title())
    data_type = CatalogValueType.NUMBER
    order = 0


class CustomValidatorFactory(DjangoModelFactory):
    class Meta:
        model = CustomValidator

    validator = factory.SubFactory(
        ValidatorFactory,
        validation_type=ValidationType.CUSTOM_VALIDATOR,
        is_system=False,
    )
    org = factory.SubFactory(OrganizationFactory)
    custom_type = CustomValidatorType.MODELICA
    base_validation_type = ValidationType.CUSTOM_VALIDATOR
    notes = "Custom validator for tests."
    created_by = factory.SubFactory(UserFactory)


class ValidationRunFactory(DjangoModelFactory):
    class Meta:
        model = ValidationRun

    submission = factory.SubFactory(SubmissionFactory)
    workflow = factory.LazyAttribute(lambda o: o.submission.workflow)
    org = factory.LazyAttribute(lambda o: o.submission.org)
    project = factory.LazyAttribute(lambda o: o.submission.project)
    user = factory.LazyAttribute(lambda o: o.submission.user)
    status = ValidationRunStatus.PENDING
    source = ValidationRunSource.LAUNCH_PAGE


class ValidationStepRunFactory(DjangoModelFactory):
    class Meta:
        model = ValidationStepRun

    validation_run = factory.SubFactory(ValidationRunFactory)
    workflow_step = factory.SubFactory(
        WorkflowStepFactory,
        workflow=factory.SelfAttribute("..validation_run.workflow"),
    )
    step_order = factory.Sequence(lambda n: n * 10)
    status = StepStatus.PENDING
    output = factory.Dict({})


class ValidationFindingFactory(DjangoModelFactory):
    class Meta:
        model = ValidationFinding

    validation_step_run = factory.SubFactory(ValidationStepRunFactory)
    # Ensure run FK matches the step_run's validation_run
    validation_run = factory.LazyAttribute(
        lambda o: o.validation_step_run.validation_run,
    )
    severity = Severity.ERROR
    code = ""
    message = factory.Faker("sentence")
    path = factory.Faker("file_path", depth=3)
    meta = factory.Dict({})
    ruleset_assertion = None


class ArtifactFactory(DjangoModelFactory):
    class Meta:
        model = Artifact

    validation_run = factory.SubFactory(ValidationRunFactory)
    org = factory.SubFactory(OrganizationFactory)
    label = factory.Sequence(lambda n: f"artifact-{n}.txt")
    content_type = "text/plain"
    size_bytes = 0
    # file is optional; tests can attach a ContentFile if needed


class RulesetAssertionFactory(DjangoModelFactory):
    class Meta:
        model = RulesetAssertion

    ruleset = factory.SubFactory(RulesetFactory)
    order = factory.Sequence(lambda n: n * 10)
    assertion_type = AssertionType.BASIC
    operator = AssertionOperator.LE
    target_field = "facility_electric_demand_w"
    severity = Severity.ERROR
    rhs = factory.Dict({"value": 100})
    options = factory.Dict({})
    message_template = "Peak too high."


class CallbackReceiptFactory(DjangoModelFactory):
    """Factory for CallbackReceipt model used in idempotency tests."""

    class Meta:
        model = CallbackReceipt

    callback_id = factory.Sequence(lambda n: f"cb-uuid-{n}")
    validation_run = factory.SubFactory(ValidationRunFactory)
    status = CallbackReceiptStatus.COMPLETED
    result_uri = factory.LazyAttribute(
        lambda o: f"gs://bucket/runs/{o.validation_run.id}/output.json"
    )
