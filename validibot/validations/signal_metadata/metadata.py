"""
Typed Pydantic accessor models for validator-specific signal metadata.

Each validator type stores domain-specific properties in the ``metadata``
and ``provider_binding`` JSONFields on ``SignalDefinition``. These Pydantic
models provide type-safe access to that JSON, validating on both read
(via model properties) and write (via ``.model_dump()``).

**Design principle:** Universal signal properties live as database columns
on ``SignalDefinition`` (contract_key, direction, data_type, unit, etc.).
Validator-specific properties that only matter for a particular validator
type live in JSON, accessed through these models.

Two JSON fields serve different purposes:

- ``provider_binding``: Execution-facing config. Used by the runtime to
  locate values in provider-specific formats (e.g., EnergyPlus metric keys).
  Never contains submission-source info — that lives on ``StepSignalBinding``.

- ``metadata``: UI and presentation data. Used by the signals table, edit
  modal, and submission form (e.g., template variable constraints).

See Also:
    - ``validations/models.py`` — ``SignalDefinition`` model
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from pydantic import Field

# ---------------------------------------------------------------------------
# FMU signal metadata
# ---------------------------------------------------------------------------


class FMUSignalMetadata(BaseModel):
    """UI/presentation metadata for FMU signals.

    Stored in ``SignalDefinition.metadata`` for signals created from FMU
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
    """Provider-native extraction config for FMU signals.

    Stored in ``SignalDefinition.provider_binding``. Describes how the
    FMU runtime should treat this variable.

    Note: FMU signals primarily use ``native_name`` on ``SignalDefinition``
    for ``fmpy.simulate_fmu(start_values=...)`` mapping. This binding is
    for additional FMI-specific runtime hints.
    """

    causality: str = "input"
    """FMI causality: 'input', 'output', 'parameter'.

    Lowercase to match the FMI XML spec (modelDescription.xml uses
    ``causality="input"``) and fmpy's expected format. This is FMU-specific
    metadata, not a Validibot concept — Validibot's own input/output
    distinction is the ``direction`` column on SignalDefinition.
    """


# ---------------------------------------------------------------------------
# EnergyPlus signal metadata
# ---------------------------------------------------------------------------


class EnergyPlusProviderBinding(BaseModel):
    """Provider-native extraction config for EnergyPlus output signals.

    Stored in ``SignalDefinition.provider_binding`` for library EnergyPlus
    validator signals (defined in ``energyplus/config.py``). Describes
    how to find this signal in EnergyPlus simulation output — not where
    submission data comes from (that is ``source_scope`` on
    ``StepSignalBinding``).
    """

    metric_key: str = ""
    """EnergyPlus metric key used by the output extractor.

    Maps to a field on ``EnergyPlusSimulationMetrics`` in
    ``validibot-shared``. Examples: 'site_eui_kwh_m2',
    'floor_area_m2', 'site_electricity_kwh'.
    """


# ---------------------------------------------------------------------------
# Template signal metadata
# ---------------------------------------------------------------------------


class TemplateSignalMetadata(BaseModel):
    """UI/presentation metadata for EnergyPlus template signals.

    Stored in ``SignalDefinition.metadata`` for signals created from
    template variable scanning (``$VARIABLE_NAME`` placeholders in IDF
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
