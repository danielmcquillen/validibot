"""
Validator infrastructure: base classes, data structures, and registry.

This package provides the foundation that all validators build on:

- **BaseValidator**: Abstract base class for all validators.
- **SimpleValidator**: Template method base for synchronous validators
  (Basic, JSON Schema, XML Schema, THERM, AI).
- **AdvancedValidator**: Template method base for container-based validators
  (EnergyPlus, FMU) that dispatch work to validator jobs.
- **ValidationIssue, ValidationResult, AssertionStats**: Data classes shared
  by all validators.
- **register_validator, get**: Registry for mapping ValidationType to
  validator classes.

External callers should import from this package rather than reaching into
individual modules::

    from validibot.validations.validators.base import (
        BaseValidator,
        SimpleValidator,
        AdvancedValidator,
        ValidationIssue,
        ValidationResult,
        register_validator,
    )
"""

from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.base import AssertionEvaluationResult
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.base.config import CatalogEntrySpec
from validibot.validations.validators.base.config import ValidatorConfig
from validibot.validations.validators.base.config import discover_configs
from validibot.validations.validators.base.config import get_all_configs
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.base.config import populate_registry
from validibot.validations.validators.base.registry import get
from validibot.validations.validators.base.registry import register_validator
from validibot.validations.validators.base.simple import SimpleValidator

__all__ = [
    "AdvancedValidator",
    "AssertionEvaluationResult",
    "AssertionStats",
    "BaseValidator",
    "CatalogEntrySpec",
    "SimpleValidator",
    "ValidationIssue",
    "ValidationResult",
    "ValidatorConfig",
    "discover_configs",
    "get",
    "get_all_configs",
    "get_config",
    "populate_registry",
    "register_validator",
]
