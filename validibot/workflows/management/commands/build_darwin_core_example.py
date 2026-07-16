"""Generate the committed Darwin Core workflow import example.

Writes ``tests/workflows/darwin_core.json`` (the bare definition) and
``tests/workflows/darwin_core.vaf`` (the same definition packaged as an archive)
so both import paths have a fixture. The definition is built from real enum
values and the real Table Schema asset, so it can't drift from what the importer
expects — running this command after a schema change regenerates a valid pair.

This is a pure construction (no database): it assembles the ``workflow.json``
dict directly and packs it. The round-trip *correctness* (export a live workflow,
re-import it) is proven by the tests; this command just materialises the
canonical, human-readable example.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.workflows.services.io import schema
from validibot.workflows.services.io import vaf

# The four manual assertions from the Darwin Core example: two cross-field rules
# and two "schema-valid but meaningless" rules. Each is a row-stage CEL assertion.
_ROW_ASSERTIONS = [
    (
        "row.minimumDepthInMeters <= row.maximumDepthInMeters",
        "Minimum depth exceeds maximum depth.",
    ),
    (
        "!(row.decimalLatitude == 0.0 && row.decimalLongitude == 0.0)",
        "Coordinates are at Null Island (0, 0).",
    ),
    (
        'row.occurrenceStatus != "present" || row.individualCount >= 1',
        "A present occurrence must record at least one individual.",
    ),
    (
        "row.coordinateUncertaintyInMeters > 0.0",
        "coordinateUncertaintyInMeters must be greater than zero.",
    ),
]


class Command(BaseCommand):
    """Write the Darwin Core ``.json``/``.vaf`` import fixtures."""

    help = (
        "Generate tests/workflows/darwin_core.{json,vaf} from the Table Schema asset."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        tests_dir = Path(settings.BASE_DIR) / "tests"
        table_schema = (
            tests_dir / "assets" / "csv" / "darwin_core" / "occurrence_schema.json"
        ).read_text(encoding="utf-8")

        definition = _build_definition(table_schema)

        out_dir = tests_dir / "workflows"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / "darwin_core.json"
        vaf_path = out_dir / "darwin_core.vaf"

        json_path.write_text(
            json.dumps(definition, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        vaf_path.write_bytes(
            vaf.pack(
                definition,
                manifest_extra={"workflow_name": definition["workflow"]["name"]},
            ),
        )

        self.stdout.write(self.style.SUCCESS(f"Wrote {json_path}"))
        self.stdout.write(self.style.SUCCESS(f"Wrote {vaf_path}"))


def _build_definition(table_schema: str) -> dict[str, Any]:
    """Assemble the Darwin Core workflow definition dict."""
    assertions = [
        {
            "order": index + 1,
            "assertion_type": AssertionType.CEL_EXPRESSION.value,
            "operator": AssertionOperator.LE.value,
            "target_data_path": "",
            "target_io_definition_ref": None,
            "severity": Severity.ERROR.value,
            "when_expression": "",
            "message_template": message,
            "success_message": "",
            "cel_cache": "",
            "spec_version": 1,
            "rhs": {"expr": expression},
            "options": {"tabular_stage": "row"},
        }
        for index, (expression, message) in enumerate(_ROW_ASSERTIONS)
    ]

    step = {
        "order": 10,
        "step_key": "check_incoming_csv",
        "name": "Check incoming CSV",
        "description": "Validate a Darwin Core occurrence CSV against the "
        "occurrence Table Schema and four marine quality rules.",
        "notes": "",
        "show_success_messages": True,
        "config": {},
        "kind": "validator",
        "validator_ref": {
            "validation_type": ValidationType.TABULAR.value,
            "slug": "tabular-validator",
            "version": 1,
            "is_system": True,
            "name": "Tabular Validator",
        },
        "ruleset": {
            "name": "Darwin Core Occurrence Table Schema",
            "ruleset_type": RulesetType.TABULAR.value,
            "version": "1",
            "rules_text": table_schema,
            "metadata": {},
            "assertions": assertions,
        },
        "step_io_definitions": [],
        "input_bindings": [],
        "derivations": [],
        "io_promotions": [],
        "resources": [],
    }

    return {
        "format_version": schema.FORMAT_VERSION,
        "workflow": {
            "name": "Darwin Core Occurrence QA",
            "description": "Quality-gate Darwin Core occurrence records: column "
            "schema plus cross-field and domain assertions, mirroring OBIS QC.",
            "slug": "darwin-core-occurrence-qa",
            "allowed_file_types": [SubmissionFileType.TEXT.value],
            "success_message": "",
            "public_info": None,
            "signal_mappings": [],
        },
        "steps": [step],
    }
