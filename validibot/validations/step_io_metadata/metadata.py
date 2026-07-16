"""
Typed Pydantic accessor models for validator-specific step I/O metadata.

Each validator type stores domain-specific properties in the ``metadata``
and ``provider_binding`` JSONFields on ``StepIODefinition``. These Pydantic
models provide type-safe access to that JSON, validating on both read
(via model properties) and write (via ``.model_dump()``).

**Design principle:** Universal step I/O properties live as database columns
on ``StepIODefinition`` (contract_key, direction, data_type, unit, etc.).
Validator-specific properties that only matter for a particular validator
type live in JSON, accessed through these models.

Two JSON fields serve different purposes:

- ``provider_binding``: Execution-facing config. Used by the runtime to
  locate values in provider-specific formats (e.g., EnergyPlus metric keys).
  Never contains submission-source info — that lives on ``StepInputBinding``.

- ``metadata``: UI and presentation data. Used by the step I/O table, edit
  modal, and submission form (e.g., template variable constraints).

See Also:
    - ``validations/models.py`` — ``StepIODefinition`` model
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import Field

# ---------------------------------------------------------------------------
# FMU step I/O metadata
# ---------------------------------------------------------------------------


class FMUStepIOMetadata(BaseModel):
    """UI/presentation metadata for FMU step I/O definitions.

    Stored in ``StepIODefinition.metadata`` for FMU-derived step I/O definitions
    introspection (both library and step-level paths). Contains FMI
    specification properties used for display and debugging.
    """

    variability: str = ""
    """FMI variability: 'continuous', 'discrete', 'fixed', etc."""

    value_reference: int = 0
    """FMI value reference number from modelDescription.xml."""

    value_type: str = "Real"
    """FMI data type: 'Real', 'Integer', 'Boolean', 'String'."""


class FMUProviderBinding(BaseModel):
    """Provider-native extraction config for FMU step I/O definitions.

    Stored in ``StepIODefinition.provider_binding``. Describes how the
    FMU runtime should treat this variable.

    Note: FMU step I/O definitions primarily use ``native_name`` on ``StepIODefinition``
    for ``fmpy.simulate_fmu(start_values=...)`` mapping. This binding is
    for additional FMI-specific runtime hints.
    """

    causality: str = "input"
    """FMI causality: 'input', 'output', 'parameter'.

    Lowercase to match the FMI XML spec (modelDescription.xml uses
    ``causality="input"``) and fmpy's expected format. This is FMU-specific
    metadata, not a Validibot concept — Validibot's own input/output
    distinction is the ``direction`` column on StepIODefinition.
    """


# ---------------------------------------------------------------------------
# EnergyPlus step I/O metadata
# ---------------------------------------------------------------------------


class EnergyPlusProviderBinding(BaseModel):
    """Provider-native extraction config for EnergyPlus step outputs.

    Stored in ``StepIODefinition.provider_binding`` for library EnergyPlus
    validator step outputs (defined in ``energyplus/config.py``). Describes
    how to find this step output in EnergyPlus simulation output — not where
    submission data comes from (that is ``source_scope`` on
    ``StepInputBinding``).
    """

    metric_key: str = ""
    """EnergyPlus metric key used by the output extractor.

    Maps to a field on ``EnergyPlusSimulationMetrics`` in
    ``validibot-shared``. Examples: 'site_eui_kwh_m2',
    'simulated_conditioned_area_m2', 'site_electricity_kwh'.
    """


# ---------------------------------------------------------------------------
# Template step I/O metadata
# ---------------------------------------------------------------------------


class TemplateStepIOMetadata(BaseModel):
    """UI/presentation metadata for EnergyPlus template step input definitions.

    Stored in ``StepIODefinition.metadata`` for template step I/O definitions
    created from template variable scanning (``$VARIABLE_NAME`` placeholders in IDF
    files). Controls the submission form rendering for each variable.
    """

    variable_type: Literal["text", "number", "choice"] = "text"
    """Input type constraint for the submission form.

    - ``'text'``: Accepts any non-empty string.
    - ``'number'``: Enables min/max validation.
    - ``'choice'``: Restricts values to the ``choices`` list.
    """

    min_value: float | None = None
    """Minimum allowed value (number type only)."""

    min_exclusive: bool = False
    """Whether min_value is exclusive (strict >)."""

    max_value: float | None = None
    """Maximum allowed value (number type only)."""

    max_exclusive: bool = False
    """Whether max_value is exclusive (strict <)."""

    choices: list[str] = Field(default_factory=list)
    """Allowed values (choice type only)."""
