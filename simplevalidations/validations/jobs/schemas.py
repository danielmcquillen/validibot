"""
Pydantic schemas for validator job execution envelopes.

These schemas define the contract between Django and Cloud Run Job validators.
See docs/adr/2025-12-04-validator-job-interface.md for specification details.

This module re-exports schemas from sv_shared for Django app use.
"""

from __future__ import annotations

# Re-export all schemas from sv_shared
from sv_shared.validations.envelopes import ExecutionContext
from sv_shared.validations.envelopes import InputFileItem
from sv_shared.validations.envelopes import MessageLocation
from sv_shared.validations.envelopes import OrganizationInfo
from sv_shared.validations.envelopes import RawOutputs
from sv_shared.validations.envelopes import Severity
from sv_shared.validations.envelopes import SupportedMimeType
from sv_shared.validations.envelopes import ValidationArtifact
from sv_shared.validations.envelopes import ValidationCallback
from sv_shared.validations.envelopes import ValidationInputEnvelope
from sv_shared.validations.envelopes import ValidationMessage
from sv_shared.validations.envelopes import ValidationMetric
from sv_shared.validations.envelopes import ValidationOutputEnvelope
from sv_shared.validations.envelopes import ValidationStatus
from sv_shared.validations.envelopes import ValidationTiming
from sv_shared.validations.envelopes import ValidatorInfo
from sv_shared.validations.envelopes import WorkflowInfo

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
    "WorkflowInfo",
]
