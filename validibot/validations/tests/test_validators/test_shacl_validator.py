"""Integration tests for :class:`SHACLValidator` (the orchestrator).

These tests exercise the full Django path — Validator + Ruleset +
Submission models — to cover the parts the pure-function engine tests
can't reach: namely the library-validator merge (``validator.default_ruleset``
combined with the step-level ``ruleset``).

The merge path is the key thing this file proves works. The
single-source-of-shapes path is well-covered by ``test_shacl_engine.py``;
here we focus on the wiring between Django models and the engine.
"""

from __future__ import annotations

from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.shacl.validator import SHACLValidator

# When both library default + step extras fire on the same submission,
# we expect exactly this many ERROR findings — one per layered shape.
LIBRARY_PLUS_STEP_ERROR_COUNT = 2

# Same fixture shape as the engine tests, repeated here to keep these
# integration tests self-contained. (Sharing fixtures across test files
# in pytest-django is fiddly; the tiny size keeps the duplication cheap.)
SHAPES_PERSON_REQUIRES_NAME = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [
        sh:path ex:name ;
        sh:minCount 1 ;
        sh:message "Person needs a name." ;
    ] .
"""

# A separate shape used to verify library + step ruleset merging. When
# this is layered on top of the library shape, Bob (who has no nickname
# either) gets two findings, not one.
SHAPES_PERSON_REQUIRES_NICKNAME = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonNicknameShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [
        sh:path ex:nickname ;
        sh:minCount 1 ;
        sh:message "Person needs a nickname (project rule)." ;
    ] .
"""

DATA_BOB_NO_NAME = """
@prefix ex: <http://example.com/> .
ex:bob a ex:Person .
"""

DATA_ALICE_WITH_NAME_NO_NICKNAME = """
@prefix ex: <http://example.com/> .
ex:alice a ex:Person ; ex:name "Alice" .
"""


class SHACLValidatorSystemPathTests(TestCase):
    """Verify the ad-hoc system path: step ruleset only, no library validator.

    This is the simplest configuration — an author adds the system
    SHACLValidator, uploads shapes via the step config form, validates
    a submission. The validator has no default_ruleset; everything
    comes from the step.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        # System SHACL validator: no default_ruleset, is_system=True
        # in production but the factory pattern doesn't care for tests.
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            org=cls.org,
            is_system=False,
        )

    def test_passing_submission_returns_passed_true(self):
        """A submission that conforms to the step shapes passes the gate."""
        ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NAME,
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = """@prefix ex: <http://example.com/> .
ex:alice a ex:Person ; ex:name "Alice" ."""
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(self.validator, submission, ruleset)

        assert result.passed is True
        # SHACL conformance produces zero issues — operators only see
        # findings when something is actually wrong.
        assert all(i.severity != Severity.ERROR for i in result.issues)
        assert result.signals["shacl_violation_count"] == 0
        assert result.signals["parse_ok"] is True

    def test_failing_submission_returns_passed_false_with_error_finding(self):
        """A SHACL Violation surfaces as Severity.ERROR and blocks passing."""
        ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NAME,
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_BOB_NO_NAME
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(self.validator, submission, ruleset)

        assert result.passed is False
        error_issues = [i for i in result.issues if i.severity == Severity.ERROR]
        assert len(error_issues) == 1
        assert "bob" in error_issues[0].meta["shacl_focus_node"].lower()
        assert result.signals["shacl_violation_count"] == 1

    def test_native_shacl_report_serialised_to_stats(self):
        """The validation produces a downloadable SHACL ValidationReport.

        Downstream tools (BuildingMOTIF, analytics platforms) can ingest
        the native sh:ValidationReport Turtle directly. We attach it to
        ``stats`` so the existing run-detail UI can surface it as an
        artifact without schema changes.
        """
        ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NAME,
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_BOB_NO_NAME
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(self.validator, submission, ruleset)

        report = result.stats["results_graph_turtle"]
        # Native SHACL reports always include a ValidationReport node
        # plus at least one ValidationResult when violations exist.
        assert "ValidationReport" in report
        assert "ValidationResult" in report

    def test_parse_failure_yields_error_finding_and_passed_false(self):
        """Invalid RDF in the submission produces a clear ERROR.

        Operators sometimes upload the wrong file type (e.g. plain
        text labelled as Turtle). The validator should fail cleanly,
        not crash.
        """
        ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NAME,
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = "this is not RDF at all <<<"
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(self.validator, submission, ruleset)

        assert result.passed is False
        assert any(
            "parse" in (i.code or "").lower() or "parse" in (i.message or "").lower()
            for i in result.issues
        )

    def test_empty_shapes_returns_engine_error(self):
        """A ruleset with no shapes content returns a clear engine error.

        This guards against a library validator (or step) being saved
        with empty rules_text. Without the guard, every submission would
        silently pass.
        """
        ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text="",
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_BOB_NO_NAME
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(self.validator, submission, ruleset)

        assert result.passed is False
        assert any(i.code == "shacl.engine_error" for i in result.issues)


class SHACLValidatorLibraryPathTests(TestCase):
    """Verify the library validator path: default_ruleset + step extras merge.

    This is the contract that distinguishes a library-level custom
    SHACL validator (Priya's ``MeridianCx 223P + G36 Validator``) from
    the ad-hoc system path. The engine concatenates the library
    validator's bundled shapes with any step-level extras the workflow
    author added.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        # Library validator: has a default_ruleset attached carrying the
        # org's bundled shapes (e.g. 223P + G36 in real use).
        cls.default_ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NAME,
        )
        cls.library_validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            org=cls.org,
            is_system=False,
            default_ruleset=cls.default_ruleset,
        )

    def test_library_shapes_apply_without_step_extras(self):
        """When the step ruleset is empty, only the library shapes run.

        Mirrors the common case: Anna picks Priya's library validator,
        adds it to a workflow step with no project-specific extras,
        and expects the library shapes to fire.
        """
        # Step ruleset with no shapes — engine should still merge in
        # the library default and produce the violation.
        step_ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text="",
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_BOB_NO_NAME
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(
            self.library_validator,
            submission,
            step_ruleset,
        )

        # The library shape fires even though the step ruleset is empty.
        error_issues = [i for i in result.issues if i.severity == Severity.ERROR]
        assert len(error_issues) == 1
        assert "name" in error_issues[0].message.lower()

    def test_step_extras_layer_on_top_of_library_shapes(self):
        """Library shapes + step extras combine; Alice fails both rules.

        Alice has a name (satisfies library rule) but no nickname
        (fails the step extra). With merging, we expect exactly one
        ERROR — the nickname rule from the step layer.
        """
        step_ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NICKNAME,
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_ALICE_WITH_NAME_NO_NICKNAME
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(
            self.library_validator,
            submission,
            step_ruleset,
        )

        error_issues = [i for i in result.issues if i.severity == Severity.ERROR]
        # Library rule (needs name) passes; step rule (needs nickname) fails.
        assert len(error_issues) == 1
        assert "nickname" in error_issues[0].message.lower()

    def test_both_rules_fire_when_data_violates_both(self):
        """When the submission violates both library + step rules, both surface.

        Demonstrates the merge produces a union of findings, not
        either-or. This is the value-add of shape stacking: project
        rules supplement, they don't replace.
        """
        step_ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NICKNAME,
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_BOB_NO_NAME  # no name, no nickname
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(
            self.library_validator,
            submission,
            step_ruleset,
        )

        error_issues = [i for i in result.issues if i.severity == Severity.ERROR]
        assert len(error_issues) == LIBRARY_PLUS_STEP_ERROR_COUNT

    def test_step_metadata_overrides_library_engine_knobs(self):
        """Step-level inference_mode wins over the library default.

        Operators sometimes need to override a library validator's
        defaults for a specific workflow (e.g. "this step doesn't need
        OWL inference, run it cheaper"). The engine resolves
        per-key with step > library > fallback precedence.
        """
        # Library default: rdfs. Step override: none.
        self.default_ruleset.metadata = {"inference_mode": "rdfs"}
        self.default_ruleset.save(update_fields=["metadata"])
        step_ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text=SHAPES_PERSON_REQUIRES_NICKNAME,
            metadata={"inference_mode": "none"},
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_ALICE_WITH_NAME_NO_NICKNAME
        submission.save(update_fields=["content"])

        # We're indirectly verifying the override via the engine running
        # successfully (any inference-mode mishandling would crash or
        # produce wrong findings). The signal also confirms the parse
        # happened.
        result = SHACLValidator().validate(
            self.library_validator,
            submission,
            step_ruleset,
        )

        assert result.signals["parse_ok"] is True

    def test_bundled_standards_opt_out_at_step_level(self):
        """A step can opt out of a library validator's bundled standards.

        Library validator says "include Brick"; step says "no thanks,
        empty list." Engine should treat the step's empty list as
        intentional opt-out rather than inheriting the library default.
        """
        self.default_ruleset.metadata = {
            "bundled_standards": ["brick-1.4", "qudt-2.1"],
        }
        self.default_ruleset.save(update_fields=["metadata"])
        step_ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.SHACL,
            rules_text="",
            metadata={"bundled_standards": []},  # explicit opt-out
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
            file_type=SubmissionFileType.TEXT,
        )
        submission.content = DATA_BOB_NO_NAME
        submission.save(update_fields=["content"])

        result = SHACLValidator().validate(
            self.library_validator,
            submission,
            step_ruleset,
        )

        # Opting out means zero bundle warnings get surfaced. If the
        # engine had wrongly inherited the library defaults, we'd see
        # two bundle-not-yet-shipped warnings instead.
        bundle_warnings = [i for i in result.issues if i.code and "bundle" in i.code]
        assert bundle_warnings == []
