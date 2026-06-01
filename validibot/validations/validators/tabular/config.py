"""Validator config for the Tabular Validator.

Declaring this ``config`` is what makes the validator discoverable: at startup
``discover_configs()`` imports every ``<validator>/config.py`` and registers the
``config`` instance. Until this module existed, the ``tabular`` package was
skipped by discovery (no ``config.py``), so its modules were importable for
tests without the validator being surfaced as a choice.
"""

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.config import ValidatorConfig

config = ValidatorConfig(
    slug="tabular-validator",
    name="Tabular Validator",
    short_description=(
        "Validate tabular data (CSV in V1) against a column schema and per-row rules."
    ),
    # NOTE: ValidatorConfig.description is strict-typed `str` (pydantic), so do
    # NOT wrap it in `gettext_lazy` — that would crash on app boot. Other
    # validator configs follow the same convention.
    description=(
        "Validate a table of typed rows: required columns, column types, "
        "numeric ranges, string length, regex, enum membership, and "
        "single/composite uniqueness, plus CEL row assertions for "
        "cross-field and conditional logic."
    ),
    validation_type=ValidationType.TABULAR,
    validator_class=(
        "validibot.validations.validators.tabular.validator.TabularValidator"
    ),
    version=1,
    order=10,
    # CSV is carried as a plain-text file; the reader handles the rest.
    supported_file_types=[SubmissionFileType.TEXT],
    supported_data_formats=[SubmissionDataFormat.CSV],
    allowed_extensions=["csv", "tsv"],
    supports_assertions=True,
    icon="bi-table",
    # Tabular owns its workflow import/export body so a re-imported ruleset's
    # row assertions are re-checked against the declared Table Schema columns.
    step_serializer_class=(
        "validibot.validations.validators.tabular.serializer.TabularStepSerializer"
    ),
)
