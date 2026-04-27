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
        checks = typed.idf_checks  # list[str], type-checked

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

    display_signals: list[str] = Field(default_factory=list)
    """Catalog entry slugs for output signals to display to the submitter.

    Controls which output signals are shown in the results view and
    returned by the API.  Empty means show all signals (backward-compatible
    default).  This is cross-validator — any step type can use it."""


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


# ---------------------------------------------------------------------------
# EnergyPlus
# ---------------------------------------------------------------------------


class EnergyPlusStepConfig(BaseStepConfig):
    """Config for EnergyPlus validator steps.

    Stores simulation settings (checks, timestep) and template configuration
    (case sensitivity, output signal selection).  Template variable metadata
    is stored relationally in ``SignalDefinition`` rows rather than here.

    Resource files (weather EPWs, model templates) are stored relationally
    via ``WorkflowStepResource`` rather than in this config. See the
    ``step.step_resources`` reverse relation. The template *file* lives on
    ``WorkflowStepResource`` with ``role=MODEL_TEMPLATE``; the template
    *configuration* (case sensitivity) lives here.
    """

    # ── Simulation settings ──────────────────────────────────────────
    # NOTE: These settings are stored in the step config and validated by
    # Pydantic, but they are NOT yet forwarded to the validator container.
    # ``timestep_per_hour`` reaches the input envelope but the runner
    # ignores it.  ``idf_checks`` and ``run_simulation`` are not included
    # in the envelope schema at all.  Wiring these requires changes to
    # both validibot-shared (envelope schema) and validibot-validator-backends
    # (runner logic).  This is a pre-existing gap, not a regression from
    # the template work.
    # TODO: Forward run settings to the container (requires validibot-shared
    #       and validibot-validator-backends changes).

    idf_checks: list[str] = Field(default_factory=list)
    """Author-selected IDF compliance checks to run before simulation
    (e.g., ``'duplicate-names'``, ``'hvac-sizing'``, ``'schedule-coverage'``).
    Maps to EnergyPlus's ``-x`` flags.

    .. warning:: Not yet forwarded to the container. Stored for future use.
    """

    run_simulation: bool = False
    """Whether to run the full EnergyPlus simulation or just IDF syntax
    checks. When False, only ``idf_checks`` are executed (fast, no weather
    file needed).

    .. warning:: Not yet forwarded to the container. Stored for future use.
    """

    timestep_per_hour: int = 4
    """Number of simulation timesteps per hour (1-60). Higher values
    increase accuracy but slow the simulation. EnergyPlus default is 6;
    we default to 4.

    .. note:: Reaches the input envelope (``inputs.timestep_per_hour``)
       but the validator runner currently ignores it.
    """

    # ── Template settings ──────────────────────────────────────────

    case_sensitive: bool = True
    """Whether template variable matching is case-sensitive.

    When True (default), only ``$UPPERCASE_NAMES`` (matching
    ``[A-Z][A-Z0-9_]*``) are detected as template variables. ``$u_factor``
    or ``$U_Factor`` in the IDF would not be detected — the scanner emits a
    warning so the author can rename or switch modes.

    When False, all variable names are normalized to uppercase during
    scanning and matching."""

    # ── Output display ────────────────────────────────────────────

    show_energyplus_warnings: bool = True
    """Whether to include EnergyPlus simulation warnings in the findings
    shown to submitters.

    EnergyPlus often emits dozens of warnings (e.g., unused objects,
    default assumptions) that are useful for modelers debugging an IDF
    but confusing for submitters who only care about pass/fail results.
    When False, only ERROR-severity messages from the simulation are
    shown as findings; WARNING and INFO messages are suppressed."""


class FMUSimulationConfig(BaseModel):
    """Simulation settings for step-level FMU execution.

    Pre-populated from the FMU's ``DefaultExperiment`` element when
    available.  The workflow author can override any value.  When a
    field is ``None``, the container runner uses its own default.
    """

    start_time: float | None = None
    stop_time: float | None = None
    step_size: float | None = None
    tolerance: float | None = None


class FmuStepConfig(BaseStepConfig):
    """Config for FMU validator steps.

    When the step uses a step-level FMU upload (primary path), the
    ``fmu_simulation`` field stores the discovered simulation defaults.
    FMU variable metadata is stored relationally in ``SignalDefinition``
    rows rather than in the step config.  When the step uses a library
    FMU validator (secondary path), this field is empty and the metadata
    comes from the validator's ``SignalDefinition`` rows and ``FMUModel``.

    See ADR-2026-03-12: Step-Level FMU Upload for Workflow Authors.
    """

    fmu_simulation: FMUSimulationConfig | None = None
    """Simulation settings, pre-populated from DefaultExperiment.
    Only populated for step-level FMU uploads."""


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


class CredentialActionStepConfig(BaseStepConfig):
    """Config for signed credential action steps."""


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
    "SIGNED_CREDENTIAL": CredentialActionStepConfig,
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
