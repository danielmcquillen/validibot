"""
Validator infrastructure: base classes, data structures, and registry.

This package provides the foundation that all validators build on:

- **BaseValidator**: Abstract base class for all validators.
- **SimpleValidator**: Template method base for synchronous validators
  (Basic, JSON Schema, XML Schema, THERM).
- **AdvancedValidator**: Template method base for validators that require
  dedicated compute — either container-based (EnergyPlus, FMU) or
  compute-intensive services (AI via external APIs).
- **ValidationIssue, ValidationResult, AssertionStats**: Data classes shared
  by all validators.
- **ValidatorConfig**: Single source of truth for validator metadata,
  class binding, and step editor UI extensions.
- **get_validator_class**: Registry lookup for mapping ValidationType to
  validator classes.

External callers should import from this package rather than reaching into
individual modules::

    from validibot.validations.validators.base import (
        BaseValidator,
        SimpleValidator,
        AdvancedValidator,
        ValidationIssue,
        ValidationResult,
    )
"""

from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.base import AssertionEvaluationResult
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import StepEditorCardSpec
from validibot.validations.validators.base.config import ValidatorConfig
from validibot.validations.validators.base.config import discover_configs
from validibot.validations.validators.base.config import get_all_configs
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.base.config import get_output_envelope_class
from validibot.validations.validators.base.config import get_validator_class
from validibot.validations.validators.base.config import register_validator_config
from validibot.validations.validators.base.simple import SimpleValidator

__all__ = [
    "AdvancedValidator",
    "AssertionEvaluationResult",
    "AssertionStats",
    "BaseValidator",
    "CatalogEntrySpec",
    "SimpleValidator",
    "StepEditorCardSpec",
    "ValidationIssue",
    "ValidationResult",
    "ValidatorConfig",
    "discover_configs",
    "get_all_configs",
    "get_config",
    "get_output_envelope_class",
    "get_validator_class",
    "register_validator_config",
]
