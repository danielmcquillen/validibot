"""
Type-safe Pydantic models for WorkflowStep.config.

Each validator/action type stores different keys in WorkflowStep.config (a
JSONField). Previously these were untyped dicts — code that read config keys
used string literals with no schema validation. These Pydantic models provide:

- Type safety: Each config key has a declared type and optionality.
- Validation: Pydantic validates config at parse time, catching typos and
  type mismatches early.
- Documentation: The models serve as living documentation of what each
  validator/action type expects in its config.
- Forward compatibility: ``extra="allow"`` means runtime-injected keys
  (e.g., ``primary_file_uri`` added during container launch) don't break.

Usage::

    from validibot.workflows.step_configs import get_step_config

    # Parse a WorkflowStep's config into a typed model
    typed = get_step_config(step)
    if isinstance(typed, EnergyPlusStepConfig):
        file_ids = typed.resource_file_ids  # list[str], type-checked

See Also:
    - GitHub issue #96: Add type-safe Pydantic models for WorkflowStep.config
    - WorkflowStep.typed_config property (workflows/models.py)
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowStep


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class BaseStepConfig(BaseModel):
    """Base config model for all step types.

    Uses ``extra="allow"`` so runtime-injected keys (like ``primary_file_uri``
    or ``schema_type_label``) don't cause validation errors.
    """

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Validator step configs
# ---------------------------------------------------------------------------


class JsonSchemaStepConfig(BaseStepConfig):
    """Config for JSON Schema validator steps.

    Stores metadata about the schema source and a text preview for display.
    The actual schema content is stored on the Ruleset, not in step config.
    """

    schema_source: str = ""
    """How the schema was provided: "text", "upload", or "keep"."""

    schema_type: str = ""
    """JSON Schema draft version (e.g., "2020-12", "draft-07")."""

    schema_text_preview: str = ""
    """First 1200 characters of the schema for display in the step editor."""

    schema_type_label: str = ""
    """Human-readable label for the schema type (computed in views)."""


class XmlSchemaStepConfig(BaseStepConfig):
    """Config for XML Schema validator steps.

    Same structure as JSON Schema — stores source metadata and text preview.
    """

    schema_source: str = ""
    """How the schema was provided: "text", "upload", or "keep"."""

    schema_type: str = ""
    """Schema type: "XSD", "DTD", or "RELAXNG"."""

    schema_text_preview: str = ""
    """First 1200 characters of the schema for display in the step editor."""

    schema_type_label: str = ""
    """Human-readable label for the schema type (computed in views)."""


class EnergyPlusStepConfig(BaseStepConfig):
    """Config for EnergyPlus validator steps.

    Controls which checks to run, whether to run the simulation, and which
    weather/resource files to include.
    """

    resource_file_ids: list[str] = Field(default_factory=list)
    """UUIDs of ValidatorResourceFile records (e.g., weather files)."""

    idf_checks: list[str] = Field(default_factory=list)
    """Subset of: "duplicate-names", "hvac-sizing", "schedule-coverage"."""

    run_simulation: bool = False
    """Whether to run the EnergyPlus simulation (not just IDF checks)."""

    timestep_per_hour: int = 4
    """EnergyPlus simulation timesteps per hour (default: 4)."""


class FmuStepConfig(BaseStepConfig):
    """Config for FMU validator steps.

    FMU validation has no per-step configuration -- all settings are
    determined by the container and any attached assertions.
    """


class BasicStepConfig(BaseStepConfig):
    """Config for Basic assertion validator steps.

    Basic validation has no per-step configuration — assertions are
    managed on the Ruleset, not in step config.
    """


class AiAssistStepConfig(BaseStepConfig):
    """Config for AI Assist validator steps.

    Controls the AI template, enforcement mode, cost limits, and any
    policy rules or JSONPath selectors.
    """

    template: str = ""
    """AI template type: "ai_critic" or "policy_check"."""

    mode: str = ""
    """Enforcement mode: "ADVISORY" (non-blocking) or "BLOCKING"."""

    cost_cap_cents: int = 10
    """Maximum cost in cents for this AI step (1-500)."""

    selectors: list[str] = Field(default_factory=list)
    """JSONPath selectors to extract parts of the submission (max 20)."""

    policy_rules: list[dict[str, Any]] = Field(default_factory=list)
    """Policy rules as dicts with keys: path, operator, value, value_b, message."""


class CustomValidatorStepConfig(BaseStepConfig):
    """Config for Custom Validator steps.

    Custom validators have no per-step configuration — assertions are
    managed on the Ruleset.
    """


# ---------------------------------------------------------------------------
# Action step configs
# ---------------------------------------------------------------------------


class SlackActionStepConfig(BaseStepConfig):
    """Config for Slack message action steps."""

    message: str = ""
    """Message text to send to the configured Slack channel."""


class CertificateActionStepConfig(BaseStepConfig):
    """Config for signed certificate action steps."""

    certificate_template: str = ""
    """Template filename or display name for the certificate."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Maps ValidationType / action type string → config model class.
# Used by get_step_config() to select the right model.
STEP_CONFIG_MODELS: dict[str, type[BaseStepConfig]] = {
    # Validator types (from ValidationType)
    "JSON_SCHEMA": JsonSchemaStepConfig,
    "XML_SCHEMA": XmlSchemaStepConfig,
    "ENERGYPLUS": EnergyPlusStepConfig,
    "FMU": FmuStepConfig,
    "BASIC": BasicStepConfig,
    "AI_ASSIST": AiAssistStepConfig,
    "CUSTOM_VALIDATOR": CustomValidatorStepConfig,
    # Action types
    "SLACK_MESSAGE": SlackActionStepConfig,
    "SIGNED_CERTIFICATE": CertificateActionStepConfig,
}


def get_step_config(step: WorkflowStep) -> BaseStepConfig:
    """Parse a WorkflowStep's config dict into a typed Pydantic model.

    Resolves the step's validator or action type, looks up the matching
    config model from the registry, and returns a validated instance.
    Falls back to BaseStepConfig if the type is unknown.

    Args:
        step: The WorkflowStep whose config to parse.

    Returns:
        A typed config model instance (e.g., EnergyPlusStepConfig).
    """
    config_data = step.config or {}

    # Determine the step type string
    step_type = None
    if step.validator_id:
        step_type = getattr(step.validator, "validation_type", None)
    elif step.action_id:
        action = getattr(step, "action", None)
        if action:
            definition = getattr(action, "definition", None)
            if definition:
                step_type = getattr(definition, "type", None)

    model_class = STEP_CONFIG_MODELS.get(step_type or "", BaseStepConfig)
    return model_class.model_validate(config_data)
