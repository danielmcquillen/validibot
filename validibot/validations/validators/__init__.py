"""
Validator implementations package.

Each validator is a class that subclasses BaseValidator and implements the
validate() method. Validators are the core units that perform actual validation
work within a workflow step.

Validators are organized by complexity:

- **Single-file validators** (basic.py, json_schema.py, xml_schema.py):
  Simple, self-contained validators that are unlikely to need multiple modules.

- **Package validators** (therm/, energyplus/, fmu/, ai/, custom/):
  Complex validators with parsers, models, domain checks, or external
  integrations. Each package has a ``validator.py`` entry point.

- **Base classes** (base/):
  Infrastructure shared by all validators: BaseValidator, SimpleValidator,
  AdvancedValidator, data classes, and the registry.

To add a new validator, see the ``therm/`` package as the reference
implementation for SimpleValidator, or ``energyplus/`` for AdvancedValidator.
"""

# Import all validators so they register with the registry on module load.
from validibot.validations.validators import ai  # noqa: F401
from validibot.validations.validators import basic  # noqa: F401
from validibot.validations.validators import custom  # noqa: F401
from validibot.validations.validators import energyplus  # noqa: F401
from validibot.validations.validators import fmu  # noqa: F401
from validibot.validations.validators import json_schema  # noqa: F401
from validibot.validations.validators import therm  # noqa: F401
from validibot.validations.validators import xml_schema  # noqa: F401
