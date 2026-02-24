"""
AI-assist validator.

**STATUS: NOT YET IMPLEMENTED**

This validator is a placeholder for future AI-assisted validation functionality.
The intended design is to connect to an AI model (e.g., Claude, GPT) to provide
intelligent analysis and validation of submissions.

Future capabilities may include:
- Natural language policy evaluation
- Semantic understanding of data relationships
- Anomaly detection beyond simple heuristics
- Context-aware validation suggestions

Currently raises NotImplementedError when called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.base.registry import register_validator

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator


@register_validator(ValidationType.AI_ASSIST)
class AIValidator(BaseValidator):
    """
    AI-assisted validator.

    **STATUS: NOT YET IMPLEMENTED**

    This validator is intended to provide AI-powered validation by connecting to
    a language model (e.g., Claude API) to analyze submissions and provide
    intelligent validation feedback.

    Future implementation will include:
    - Integration with AI model APIs (Claude, etc.)
    - Prompt templates for different validation scenarios
    - Cost management and rate limiting
    - Structured output parsing for validation results

    Currently raises NotImplementedError when called.
    """

    # PUBLIC METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate a submission using AI-assisted analysis.

        **NOT YET IMPLEMENTED**

        This method will eventually:
        1. Extract relevant content from the submission
        2. Build a prompt with validation context and rules
        3. Call an AI model API for analysis
        4. Parse the response into ValidationResult format

        Args:
            validator: The AI validator instance.
            submission: The submission to validate.
            ruleset: Optional ruleset with validation rules.
            run_context: Execution context for the validation.

        Raises:
            NotImplementedError: AI validation is not yet implemented.
        """
        raise NotImplementedError(
            "AI-assisted validation is not yet implemented. "
            "This feature requires integration with an AI model API (e.g., Claude). "
            "See the module docstring for planned capabilities."
        )
