import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from simplevalidations.core.site_settings import MetadataPolicyError
from simplevalidations.core.site_settings import get_site_settings
from simplevalidations.projects.models import Project
from simplevalidations.submissions.ingest import prepare_inline_text
from simplevalidations.submissions.ingest import prepare_uploaded_file
from simplevalidations.submissions.models import Submission
from simplevalidations.users.models import User
from simplevalidations.validations.services.validation_run import (
    ValidationRunService,
)
from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES
from simplevalidations.workflows.constants import WorkflowStartErrorCode
from simplevalidations.workflows.constants import preferred_content_type_for_file
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.request_utils import SubmissionRequestMode
from simplevalidations.workflows.request_utils import detect_mode
from simplevalidations.workflows.views_helpers import (
    describe_workflow_file_type_violation,
)
from simplevalidations.workflows.views_helpers import resolve_submission_file_type
from simplevalidations.workflows.views_helpers import user_has_executor_role

logger = logging.getLogger(__name__)


@dataclass
class SubmissionBuild:
    submission: Submission
    metadata: dict[str, Any]


@dataclass
class LaunchHelperResult:
    status_code: int
    payload: dict[str, Any]
    headers: dict[str, str] | None = None


class LaunchValidationError(Exception):
    def __init__(
        self,
        *,
        detail: str,
        code: str,
        status_code: int,
        errors: list[dict[str, Any]] | None = None,
    ):
        super().__init__(detail)
        payload: dict[str, Any] = {"detail": detail, "code": code}
        if errors is not None:
            payload["errors"] = errors
        self.payload = payload
        self.status_code = status_code


def enforce_metadata_policy(metadata, submission_settings):
    metadata = dict(metadata or {})
    submission_settings.enforce_metadata_policy(metadata)
    return metadata


def launch_validation_run(
    *,
    request: HttpRequest,
    workflow: Workflow,
    submission: Submission,
    metadata: dict,
    user_id: int | None = None,
) -> LaunchHelperResult:
    """
    Invoke ValidationRunService.launch and normalize its response.
    """
    service = ValidationRunService()
    response = service.launch(
        request=request,
        org=workflow.org,
        workflow=workflow,
        submission=submission,
        metadata=metadata,
        user_id=user_id,
    )
    status_code = getattr(response, "status_code", 200)
    payload = getattr(response, "data", None) or {}
    headers = {}
    if hasattr(response, "items"):
        headers = {key: value for key, value in response.items()}
    return LaunchHelperResult(
        status_code=status_code,
        payload=payload,
        headers=headers or None,
    )


def handle_raw_body_mode(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    content_type_header: str,
    body_bytes: bytes,
    headers: dict[str, str],
) -> SubmissionBuild:
    """
    Process Mode 1 (raw body) submission.
    """
    encoding = headers.get("Content-Encoding")
    full_ct = headers.get("Content-Type", "")
    filename = headers.get("X-Filename") or ""
    raw = body_bytes
    max_inline = getattr(settings, "SUBMISSION_INLINE_MAX_BYTES", 10_000_000)
    if len(raw) > max_inline:
        raise LaunchValidationError(
            detail=_("Payload too large."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=413,
        )

    if encoding:
        if encoding.lower() != "base64":
            raise LaunchValidationError(
                detail=_("Unsupported Content-Encoding (only base64)."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=400,
            )
        try:
            raw = base64.b64decode(raw, validate=True)
        except Exception:
            raise LaunchValidationError(
                detail=_("Invalid base64 payload."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=400,
            )

    ct_norm = (content_type_header or "").split(";")[0].strip().lower()
    file_type = SUPPORTED_CONTENT_TYPES.get(ct_norm)
    if not file_type:
        err_msg = _("Unsupported Content-Type '%s'. ") % full_ct
        err_msg += _("Supported : %s") % ", ".join(SUPPORTED_CONTENT_TYPES.keys())
        raise LaunchValidationError(
            detail=err_msg,
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
        )

    charset = "utf-8"
    if ";" in full_ct:
        for part in full_ct.split(";")[1:]:
            k, _, v = part.partition("=")
            if k.strip().lower() == "charset" and v.strip():
                charset = v.strip()
                break
    try:
        text_content = raw.decode(charset)
    except UnicodeDecodeError:
        text_content = raw.decode("utf-8", errors="replace")

    resolved_file_type = resolve_submission_file_type(
        requested=file_type,
        filename=filename,
        inline_text=text_content,
    )
    violation = describe_workflow_file_type_violation(
        workflow=workflow,
        file_type=resolved_file_type,
    )
    if violation:
        raise LaunchValidationError(
            detail=violation,
            code=WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED,
            status_code=400,
            errors=[],
        )
    ingest_content_type = ct_norm
    if resolved_file_type != file_type:
        ingest_content_type = preferred_content_type_for_file(
            resolved_file_type,
            filename=filename,
        )

    safe_filename, ingest = prepare_inline_text(
        text=text_content,
        filename=filename,
        content_type=ingest_content_type,
        deny_magic_on_text=True,
    )

    submission = Submission(
        org=workflow.org,
        workflow=workflow,
        user=user,
        project=project,
        name=safe_filename,
        checksum_sha256=ingest.sha256,
        metadata={},
    )
    submission.set_content(
        inline_text=text_content,
        filename=safe_filename,
        file_type=resolved_file_type,
    )

    with transaction.atomic():
        submission.full_clean()
        submission.save()
        return SubmissionBuild(submission=submission, metadata={})


def process_structured_payload(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    payload: Any,
    serializer_factory: Callable[..., Any],
    submission_settings=None,
) -> SubmissionBuild:
    """
    Shared serializer handling for JSON envelope and multipart submissions.
    """
    if hasattr(payload, "copy"):
        payload = payload.copy()
    else:
        payload = dict(payload or {})
    payload["workflow"] = workflow.pk
    serializer = serializer_factory(data=payload)
    serializer.is_valid(raise_exception=True)

    vd = serializer.validated_data
    file_obj = vd.get("file", None)
    if file_obj is not None:
        max_file = getattr(settings, "SUBMISSION_FILE_MAX_BYTES", 1_000_000_000)
        if getattr(file_obj, "size", 0) > max_file:
            raise LaunchValidationError(
                detail=_("File too large."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=413,
            )

    submission_settings = submission_settings or get_site_settings().api_submission
    metadata = vd.get("metadata") or {}
    try:
        metadata = enforce_metadata_policy(metadata, submission_settings)
    except MetadataPolicyError as exc:
        raise LaunchValidationError(
            detail=_("Invalid request payload."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
            errors=[
                {
                    "field": "metadata",
                    "message": str(exc),
                },
            ],
        )

    if vd.get("file") is not None:
        file_obj = vd["file"]
        ct = vd["content_type"]
        filename_value = vd.get("filename") or getattr(file_obj, "name", "") or ""
        resolved_file_type = resolve_submission_file_type(
            requested=vd["file_type"],
            filename=filename_value,
        )
        violation = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=resolved_file_type,
        )
        if violation:
            raise LaunchValidationError(
                detail=violation,
                code=WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED,
                status_code=400,
                errors=[],
            )
        if resolved_file_type != vd["file_type"]:
            ct = preferred_content_type_for_file(
                resolved_file_type,
                filename=filename_value,
            )
        max_file = getattr(settings, "SUBMISSION_FILE_MAX_BYTES", 1_000_000_000)
        ingest = prepare_uploaded_file(
            uploaded_file=file_obj,
            filename=filename_value,
            content_type=ct,
            max_bytes=max_file,
        )
        safe_filename = ingest.filename

        submission = Submission(
            org=workflow.org,
            workflow=workflow,
            user=user if getattr(user, "is_authenticated", False) else None,
            project=project,
            name=safe_filename,
            metadata={},
            checksum_sha256=ingest.sha256,
        )
        submission.set_content(
            uploaded_file=file_obj,
            filename=safe_filename,
            file_type=resolved_file_type,
        )

    elif vd.get("normalized_content") is not None:
        ct = vd["content_type"]
        filename_value = vd.get("filename") or ""
        resolved_file_type = resolve_submission_file_type(
            requested=vd["file_type"],
            filename=filename_value,
            inline_text=vd["normalized_content"],
        )
        violation = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=resolved_file_type,
        )
        if violation:
            raise LaunchValidationError(
                detail=violation,
                code=WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED,
                status_code=400,
                errors=[],
            )
        if resolved_file_type != vd["file_type"]:
            ct = preferred_content_type_for_file(
                resolved_file_type,
                filename=filename_value,
            )
        safe_filename, ingest = prepare_inline_text(
            text=vd["normalized_content"],
            filename=filename_value,
            content_type=ct,
            deny_magic_on_text=True,
        )
        metadata = {**metadata, "sha256": ingest.sha256}
        try:
            metadata = enforce_metadata_policy(metadata, submission_settings)
        except MetadataPolicyError as exc:
            raise LaunchValidationError(
                detail=_("Invalid request payload."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=400,
                errors=[
                    {
                        "field": "metadata",
                        "message": str(exc),
                    },
                ],
            )

        submission = Submission(
            org=workflow.org,
            workflow=workflow,
            user=user if getattr(user, "is_authenticated", False) else None,
            project=project,
            name=safe_filename,
            metadata=metadata,
        )
        submission.set_content(
            inline_text=vd["normalized_content"],
            filename=safe_filename,
            file_type=resolved_file_type,
        )
    else:
        raise LaunchValidationError(
            detail=_("No content provided."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
        )

    with transaction.atomic():
        submission.full_clean()
        submission.save()
        return SubmissionBuild(submission=submission, metadata=metadata)


def handle_json_envelope(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    envelope: dict[str, Any],
    submission_settings,
    serializer_factory: Callable[..., Any],
) -> SubmissionBuild:
    payload = dict(envelope or {})
    return process_structured_payload(
        workflow=workflow,
        user=user,
        project=project,
        payload=payload,
        serializer_factory=serializer_factory,
        submission_settings=submission_settings,
    )


def handle_multipart_mode(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    payload,
    submission_settings,
    serializer_factory: Callable[..., Any],
) -> SubmissionBuild:
    return process_structured_payload(
        workflow=workflow,
        user=user,
        project=project,
        payload=payload,
        serializer_factory=serializer_factory,
        submission_settings=submission_settings,
    )


def start_validation_run_for_workflow(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    headers: dict[str, str] | None = None,
    content_type_header: str | None = None,
    body_bytes: bytes | None = None,
    serializer_factory: Callable[..., Any] | None = None,
    multipart_payload=None,
    submission_settings=None,
    submission_build: SubmissionBuild | None = None,
) -> SubmissionBuild:
    headers = headers or {}
    _ensure_workflow_ready(workflow, user)

    if submission_build is not None:
        return submission_build

    if serializer_factory is None:
        raise ValueError("serializer_factory is required when parsing submission data.")
    if content_type_header is None or body_bytes is None:
        raise ValueError("content_type_header/body_bytes required when parsing submission data.")

    detection_result = detect_mode(
        request=None,
        content_type_header=content_type_header,
        body_bytes=body_bytes,
    )
    if detection_result.has_error:
        error_message = detection_result.error or _("Invalid request payload.")
        logger.warning(
            "Submission mode detection failed",
            extra={
                "workflow_id": workflow.pk,
                "content_type": detection_result.content_type,
                "error": detection_result.error,
            },
        )
        raise LaunchValidationError(
            detail=_("Invalid request payload."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=400,
            errors=[
                {
                    "field": "content",
                    "message": error_message,
                },
            ],
        )

    submission_settings = submission_settings or get_site_settings().api_submission

    if detection_result.mode == SubmissionRequestMode.RAW_BODY:
        return handle_raw_body_mode(
            workflow=workflow,
            user=user,
            project=project,
            content_type_header=content_type_header,
            body_bytes=body_bytes,
            headers=headers,
        )

    if detection_result.mode == SubmissionRequestMode.JSON_ENVELOPE:
        return handle_json_envelope(
            workflow=workflow,
            user=user,
            project=project,
            envelope=detection_result.parsed_envelope or {},
            submission_settings=submission_settings,
            serializer_factory=serializer_factory,
        )

    if detection_result.mode == SubmissionRequestMode.MULTIPART:
        payload_value = (
            multipart_payload()
            if callable(multipart_payload)
            else multipart_payload
        )
        return handle_multipart_mode(
            workflow=workflow,
            user=user,
            project=project,
            payload=payload_value or {},
            submission_settings=submission_settings,
            serializer_factory=serializer_factory,
        )

    logger.warning(
        "Unsupported submission mode detected",
        extra={
            "workflow_id": workflow.pk,
            "content_type": detection_result.content_type,
        },
    )
    raise LaunchValidationError(
        detail=_("Unsupported request content type."),
        code=WorkflowStartErrorCode.INVALID_PAYLOAD,
        status_code=400,
    )


def _ensure_workflow_ready(workflow: Workflow, user: User) -> None:
    if not user_has_executor_role(user, workflow):
        raise LaunchValidationError(
            detail=_("You do not have permission to run this workflow."),
            code=WorkflowStartErrorCode.INVALID_PAYLOAD,
            status_code=403,
        )
    if not workflow.is_active:
        raise LaunchValidationError(
            detail="",
            code=WorkflowStartErrorCode.WORKFLOW_INACTIVE,
            status_code=409,
        )
    if not workflow.steps.exists():
        raise LaunchValidationError(
            detail=_("This workflow has no steps defined and cannot be executed."),
            code=WorkflowStartErrorCode.NO_WORKFLOW_STEPS,
            status_code=400,
        )
