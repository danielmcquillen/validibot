"""
THERM validator engine.

Parses THMX/THMZ files, runs domain checks, and extracts signals
for downstream assertion evaluation. Does NOT run THERM simulations.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.registry import register_engine
from validibot.validations.engines.simple_base import SimpleValidatorEngine
from validibot.validations.engines.therm.boundaries import run_boundary_checks
from validibot.validations.engines.therm.geometry import run_geometry_checks
from validibot.validations.engines.therm.materials import run_material_checks
from validibot.validations.engines.therm.parser import parse_therm_file
from validibot.validations.engines.therm.signals import extract_signals

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.validations.engines.therm.models import ThermModel

logger = logging.getLogger(__name__)


@register_engine(ValidationType.THERM)
class ThermValidatorEngine(SimpleValidatorEngine):
    """
    THERM thermal analysis file validator.

    Validates THMX and THMZ files by parsing their XML structure,
    running domain checks, and extracting structured signals for
    use in downstream assertion evaluation.

    This is a parser and checker only — it does not run THERM
    simulations or compute U-factors.
    """

    def validate_file_type(
        self,
        submission: Submission,
    ) -> ValidationIssue | None:
        """Accept XML (THMX) and BINARY (THMZ) submissions."""
        if submission.file_type not in (
            SubmissionFileType.XML,
            SubmissionFileType.BINARY,
        ):
            return ValidationIssue(
                path="",
                message=(
                    "THERM validator requires .thmx (XML) or .thmz (binary archive) "
                    f"files, but received file type '{submission.file_type}'."
                ),
                severity=Severity.ERROR,
            )
        return None

    def parse_content(self, submission: Submission) -> ThermModel:
        """Parse the THMX/THMZ submission into a ThermModel."""
        if submission.file_type == SubmissionFileType.BINARY:
            content = self._read_binary_content(submission)
        else:
            content = submission.get_content()
            if not content:
                msg = "Empty submission content."
                raise ValueError(msg)

        filename = getattr(submission, "original_filename", None)
        return parse_therm_file(content, filename=filename)

    def run_domain_checks(
        self,
        parsed: ThermModel,
    ) -> list[ValidationIssue]:
        """Run all THERM domain checks."""
        issues: list[ValidationIssue] = []
        issues.extend(run_geometry_checks(parsed.polygons))
        issues.extend(run_material_checks(parsed.materials))
        issues.extend(
            run_boundary_checks(
                parsed.polygons,
                parsed.materials,
                parsed.boundary_conditions,
            ),
        )
        return issues

    def extract_signals(self, parsed: ThermModel) -> dict[str, Any]:
        """Extract signals from the parsed ThermModel."""
        return extract_signals(parsed)

    @staticmethod
    def _read_binary_content(submission: Submission) -> bytes:
        """Read raw bytes from a binary submission's file field."""
        if not submission.input_file:
            msg = "Binary submission has no input file."
            raise ValueError(msg)
        try:
            with submission.input_file.open("rb") as fh:
                return fh.read()
        except Exception as exc:
            msg = f"Could not read submission file: {exc}"
            raise ValueError(msg) from exc
