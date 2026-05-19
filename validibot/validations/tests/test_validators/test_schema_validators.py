"""Runtime tests for schema validators with step-level assertions.

JSON Schema and XML Schema validators perform their schema check first and then
run workflow-authored assertions against the parsed payload. These tests prove
that the authoring UI capability is backed by validator execution, not just a
catalog flag.
"""

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.json_schema.validator import JsonSchemaValidator
from validibot.validations.validators.xml_schema.validator import XmlSchemaValidator


def test_json_schema_validator_runs_step_assertions_after_schema_validation(db):
    """A valid JSON document can still fail an added business-rule assertion."""
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.JSON_SCHEMA,
        rules_text=(
            '{"$schema": "https://json-schema.org/draft/2020-12/schema", '
            '"type": "object", "properties": {"height": {"type": "number"}}, '
            '"required": ["height"]}'
        ),
    )
    assertion = RulesetAssertionFactory(
        ruleset=ruleset,
        target_data_path="height",
        operator=AssertionOperator.GE,
        rhs={"value": 20},
        message_template="Height is below the project minimum.",
    )
    submission = SubmissionFactory(
        content='{"height": 12}',
        file_type=SubmissionFileType.JSON,
    )

    result = JsonSchemaValidator().validate(validator, submission, ruleset)

    assert result.passed is False
    assert result.stats["schema_error_count"] == 0
    assert result.assertion_stats.total == 1
    assert result.assertion_stats.failures == 1
    assert result.issues[0].assertion_id == assertion.pk
    assert result.issues[0].message == "Height is below the project minimum."


def test_xml_schema_validator_runs_step_assertions_after_schema_validation(db):
    """A valid XML document can still fail an added business-rule assertion."""
    validator = ValidatorFactory(
        validation_type=ValidationType.XML_SCHEMA,
        supports_assertions=True,
    )
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.XML_SCHEMA,
        rules_text="""
        <xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="building">
            <xs:complexType>
              <xs:sequence>
                <xs:element name="area" type="xs:int"/>
              </xs:sequence>
            </xs:complexType>
          </xs:element>
        </xs:schema>
        """,
        metadata={"schema_type": XMLSchemaType.XSD.value},
    )
    assertion = RulesetAssertionFactory(
        ruleset=ruleset,
        target_data_path="building.area",
        operator=AssertionOperator.GE,
        rhs={"value": 20},
        options={"coerce_types": True},
        message_template="Area is below the project minimum.",
    )
    submission = SubmissionFactory(
        content="<building><area>12</area></building>",
        file_type=SubmissionFileType.XML,
    )

    result = XmlSchemaValidator().validate(validator, submission, ruleset)

    assert result.passed is False
    assert result.stats["schema_error_count"] == 0
    assert result.assertion_stats.total == 1
    assert result.assertion_stats.failures == 1
    assert result.issues[0].assertion_id == assertion.pk
    assert result.issues[0].message == "Area is below the project minimum."
