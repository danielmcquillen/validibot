from pathlib import Path

from django.db import models
from django.utils.translation import gettext_lazy as _

from simplevalidations.submissions.constants import SubmissionFileType

WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY = "workflow_launch_input_mode"


class AccessScope(models.TextChoices):
    ORG_ALL = "ORG_ALL", _("All members of the workflow's organization")
    RESTRICTED = "RESTRICTED", _("Restricted to allowed users and/or roles")


class FailPolicy(models.TextChoices):
    CONTINUE = "CONTINUE", _("Continue on failure")
    FAIL_FAST = "FAIL_FAST", _("Fail fast")


class TriggerType(models.TextChoices):
    MANUAL = "MANUAL", _("Manual")
    API = "API", _("API")
    SCHEDULE = "SCHEDULE", _("Schedule")
    GITHUB_APP = "GITHUB_APP", _("GitHub App")


class WorkflowStartErrorCode(models.TextChoices):
    NO_WORKFLOW_STEPS = "NO_WORKFLOW_STEPS", _("Workflow has no steps to execute")
    WORKFLOW_INACTIVE = "WORKFLOW_INACTIVE", _("Workflow is inactive")
    INVALID_PAYLOAD = "INVALID_PAYLOAD", _("Invalid request payload")
    FILE_TYPE_UNSUPPORTED = (
        "FILE_TYPE_UNSUPPORTED",
        _("Workflow cannot accept the submitted file type"),
    )
    PERMISSION_DENIED = (
        "PERMISSION_DENIED",
        _("You do not have permission to run this workflow"),
    )


SUPPORTED_CONTENT_TYPES = {
    "application/json": SubmissionFileType.JSON,
    "application/xml": SubmissionFileType.XML,
    "text/plain": SubmissionFileType.TEXT,
    "text/x-idf": SubmissionFileType.TEXT,
    "text/yaml": SubmissionFileType.YAML,
    "application/yaml": SubmissionFileType.YAML,
    "application/octet-stream": SubmissionFileType.BINARY,
}

DEFAULT_CONTENT_TYPE_BY_FILE_TYPE = {
    SubmissionFileType.JSON: "application/json",
    SubmissionFileType.XML: "application/xml",
    SubmissionFileType.TEXT: "text/plain",
    SubmissionFileType.YAML: "text/yaml",
    SubmissionFileType.BINARY: "application/octet-stream",
}

_SPECIAL_EXT_CONTENT_TYPES: dict[str, dict[str, str]] = {
    SubmissionFileType.TEXT: {
        ".idf": "text/x-idf",
    },
    SubmissionFileType.YAML: {
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    },
}


def preferred_content_type_for_file(
    file_type: str,
    *,
    filename: str | None = None,
) -> str:
    """
    Return the most appropriate MIME type for a logical submission file type.

    Some logical types (like TEXT) map to more specific MIME values based on
    filename extension—for example, IDF uploads should stay ``text/x-idf`` so we
    preserve ``.idf`` extensions on sanitized filenames.
    """

    if filename:
        ext = Path(filename).suffix.lower()
        mapping = _SPECIAL_EXT_CONTENT_TYPES.get(file_type)
        if mapping:
            hint = mapping.get(ext)
            if hint:
                return hint

    return DEFAULT_CONTENT_TYPE_BY_FILE_TYPE.get(
        file_type,
        DEFAULT_CONTENT_TYPE_BY_FILE_TYPE[SubmissionFileType.TEXT],
    )
