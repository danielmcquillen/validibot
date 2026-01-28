"""
Pydantic schemas for validator job execution envelopes.

These schemas define the contract between Django and Cloud Run Job validators.
See validibot-project/docs/adr/completed/2025-12-04-validator-job-interface.md
for specification details.

This module re-exports schemas from vb_shared for Django app use.
"""

from __future__ import annotations

# Re-export all schemas from vb_shared
from vb_shared.validations.envelopes import ExecutionContext
from vb_shared.validations.envelopes import InputFileItem
from vb_shared.validations.envelopes import MessageLocation
from vb_shared.validations.envelopes import OrganizationInfo
from vb_shared.validations.envelopes import RawOutputs
from vb_shared.validations.envelopes import Severity
from vb_shared.validations.envelopes import SupportedMimeType
from vb_shared.validations.envelopes import ValidationArtifact
from vb_shared.validations.envelopes import ValidationCallback
from vb_shared.validations.envelopes import ValidationInputEnvelope
from vb_shared.validations.envelopes import ValidationMessage
from vb_shared.validations.envelopes import ValidationMetric
from vb_shared.validations.envelopes import ValidationOutputEnvelope
from vb_shared.validations.envelopes import ValidationStatus
from vb_shared.validations.envelopes import ValidationTiming
from vb_shared.validations.envelopes import ValidatorInfo
from vb_shared.validations.envelopes import ValidatorType
from vb_shared.validations.envelopes import WorkflowInfo

__all__ = [
    "ExecutionContext",
    "InputFileItem",
    "MessageLocation",
    "OrganizationInfo",
    "RawOutputs",
    "Severity",
    "SupportedMimeType",
    "ValidationArtifact",
    "ValidationCallback",
    "ValidationInputEnvelope",
    "ValidationMessage",
    "ValidationMetric",
    "ValidationOutputEnvelope",
    "ValidationStatus",
    "ValidationTiming",
    "ValidatorInfo",
    "ValidatorType",
    "WorkflowInfo",
]
