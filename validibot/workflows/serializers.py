from __future__ import annotations

from rest_framework import serializers
from rest_framework.reverse import reverse

from validibot.validations.models import RulesetAssertion
from validibot.validations.models import Validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

# ---------------------------------------------------------------------------
# Leaf serializers (no dependencies on other workflow serializers)
# ---------------------------------------------------------------------------


class RulesetAssertionSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a single assertion rule within a ruleset.

    ``target_field`` normalises the two possible target storage styles
    (catalog-entry slug vs free-form data path) into a single string field
    so consumers don't need to know about the internal XOR constraint.
    """

    target_field = serializers.SerializerMethodField(
        help_text=(
            "The assertion target: the catalog entry slug when targeting a known "
            "signal, or the free-form data path otherwise."
        ),
    )

    def get_target_field(self, obj: RulesetAssertion) -> str:
        if obj.target_catalog_entry_id:
            return obj.target_catalog_entry.slug
        return obj.target_data_path or ""

    class Meta:
        model = RulesetAssertion
        fields = [
            "id",
            "order",
            "assertion_type",
            "operator",
            "severity",
            "target_field",
            "when_expression",
            "rhs",
            "message_template",
            "success_message",
        ]
        read_only_fields = fields


class StepRulesetSerializer(serializers.Serializer):
    """
    Read-only summary of a Ruleset attached to a workflow step or validator default.

    Includes the full schema content (for JSON_SCHEMA and XML_SCHEMA rulesets)
    and the complete list of assertions so API consumers can see exactly what
    rules the step will enforce.
    """

    id = serializers.IntegerField(
        read_only=True,
        help_text="Primary key of the ruleset.",
    )
    name = serializers.CharField(
        read_only=True,
        help_text="Human-readable name for the ruleset.",
    )
    ruleset_type = serializers.CharField(
        read_only=True,
        help_text=(
            "Validator type this ruleset belongs to "
            "(e.g. 'BASIC', 'JSON_SCHEMA', 'ENERGYPLUS')."
        ),
    )
    schema = serializers.SerializerMethodField(
        help_text=(
            "Full schema content for JSON_SCHEMA and XML_SCHEMA rulesets, "
            "or null for other ruleset types."
        ),
    )
    assertions = RulesetAssertionSerializer(
        many=True,
        read_only=True,
        help_text="Ordered list of assertion rules in this ruleset.",
    )

    def get_schema(self, obj) -> str | None:
        """Return full schema text for schema-based rulesets, null otherwise."""
        from validibot.validations.constants import RulesetType

        if obj.ruleset_type in {RulesetType.JSON_SCHEMA, RulesetType.XML_SCHEMA}:
            return obj.rules or None
        return None


class ValidatorSummarySerializer(serializers.ModelSerializer):
    """
    Compact read-only representation of a Validator for use inside step detail.

    Exposes fields needed to identify the validator and reproduce its default
    assertion rules. ``default_ruleset`` is always populated (every Validator
    has one) and contains the assertions that apply when a step does not define
    its own step-level ruleset override.

    Effective ruleset resolution for API consumers:
        effective = step.ruleset if step.ruleset else step.validator.default_ruleset
    """

    default_ruleset = StepRulesetSerializer(
        read_only=True,
        help_text=(
            "The validator's default ruleset. Applied when a step does not "
            "define its own step-level ruleset override."
        ),
    )

    class Meta:
        model = Validator
        fields = [
            "slug",
            "name",
            "validation_type",
            "short_description",
            "default_ruleset",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# WorkflowStep serializer
# ---------------------------------------------------------------------------


class WorkflowStepSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a single step within a workflow.

    Each step is either a validator execution or an action (never both).
    - ``validator`` is populated for validation steps; ``action_type`` is null.
    - ``action_type`` is populated for action steps (e.g. 'SLACK_MESSAGE');
      ``validator`` is null.
    - ``config`` is the raw per-step JSON configuration as stored — the shape
      varies by validator/action type (see WorkflowStep.typed_config for the
      Pydantic-validated equivalent).
    - ``ruleset`` is the step-level ruleset overriding the validator's defaults,
      or null if the validator's default ruleset applies.
    """

    step_number = serializers.IntegerField(
        read_only=True,
        help_text="Display position of this step (derived from order field).",
    )

    validator = ValidatorSummarySerializer(
        read_only=True,
        allow_null=True,
        help_text="Validator executed by this step, or null for action steps.",
    )

    action_type = serializers.SerializerMethodField(
        help_text=(
            "Action type identifier (e.g. 'SLACK_MESSAGE', 'SIGNED_CERTIFICATE') "
            "for action steps, or null for validator steps."
        ),
    )

    ruleset = StepRulesetSerializer(
        read_only=True,
        allow_null=True,
        help_text=(
            "Step-level ruleset with assertions, or null if the validator's "
            "default ruleset applies."
        ),
    )

    def get_action_type(self, obj: WorkflowStep) -> str | None:
        if obj.action_id:
            definition = getattr(getattr(obj, "action", None), "definition", None)
            return getattr(definition, "type", None)
        return None

    class Meta:
        model = WorkflowStep
        fields = [
            "id",
            "order",
            "step_number",
            "name",
            "description",
            "validator",
            "action_type",
            "config",
            "ruleset",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Workflow serializers — Slim (list) and Full (detail)
# ---------------------------------------------------------------------------


class WorkflowSlimSerializer(serializers.ModelSerializer):
    """
    Lightweight read-only workflow representation for list endpoints.

    Returns just enough information to identify, navigate to, and launch
    a workflow. Used by GET /api/v1/orgs/{org}/workflows/.

    For the full nested representation including steps and assertions,
    use WorkflowFullSerializer (returned by the detail endpoint).
    """

    org = serializers.SlugRelatedField(
        slug_field="slug",
        read_only=True,
        help_text="Slug of the organization that owns this workflow.",
    )

    url = serializers.SerializerMethodField(
        help_text="Canonical API URL for this workflow.",
    )

    def get_url(self, obj: Workflow) -> str:
        """Generate the canonical org-scoped API URL for this workflow."""
        request = self.context.get("request")
        org_slug = self.context.get("org_slug") or obj.org.slug
        return reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": org_slug, "pk": obj.slug},
            request=request,
        )

    class Meta:
        model = Workflow
        fields = [
            "id",
            "uuid",
            "slug",
            "name",
            "description",
            "version",
            "org",
            "is_active",
            "allowed_file_types",
            "agent_access_enabled",
            "agent_price_cents",
            "url",
        ]
        read_only_fields = fields


class WorkflowFullSerializer(WorkflowSlimSerializer):
    """
    Full read-only workflow representation for the detail endpoint.

    Extends WorkflowSlimSerializer with submission configuration, retention
    policies, and the complete ordered list of steps. Each step includes its
    validator summary, per-step config, and ruleset assertions.

    Used by GET /api/v1/orgs/{org}/workflows/{slug}/.

    Performance note: the view prefetches
    ``steps__validator__default_ruleset__assertions__target_catalog_entry``
    and ``steps__ruleset__assertions__target_catalog_entry``
    so this serializer does not issue additional queries beyond the initial load.
    """

    steps = WorkflowStepSerializer(
        many=True,
        read_only=True,
        help_text="Ordered list of validation steps and actions in this workflow.",
    )

    class Meta(WorkflowSlimSerializer.Meta):
        fields = [
            *WorkflowSlimSerializer.Meta.fields,
            "is_public",
            "allow_submission_name",
            "allow_submission_meta_data",
            "allow_submission_short_description",
            "data_retention",
            "output_retention",
            "success_message",
            "agent_billing_mode",
            "agent_max_launches_per_hour",
            "steps",
        ]
        read_only_fields = fields
