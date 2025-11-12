import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from rest_framework.response import Response as APIResponse

from simplevalidations.core.site_settings import (
    MetadataPolicyError,
    get_site_settings,
)
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.projects.models import Project
from simplevalidations.submissions.ingest import (
    prepare_inline_text,
    prepare_uploaded_file,
)
from simplevalidations.submissions.models import Submission
from simplevalidations.users.models import User
from simplevalidations.validations.constants import (
    VALIDATION_RUN_TERMINAL_STATUSES,
)
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.serializers import ValidationRunSerializer
from simplevalidations.validations.services.validation_run import (
    ValidationRunLaunchResults,
    ValidationRunService,
)
from simplevalidations.workflows.constants import (
    SUPPORTED_CONTENT_TYPES,
    WorkflowStartErrorCode,
    preferred_content_type_for_file,
)
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.request_utils import (
    SubmissionRequestMode,
    detect_mode,
    extract_request_basics,
)
from simplevalidations.workflows.views_helpers import (
    describe_workflow_file_type_violation,
    resolve_submission_file_type,
    user_has_executor_role,
)

logger = logging.getLogger(__name__)


@dataclass
class SubmissionBuild:
    """Simple container describing a submission ready for launch."""

    submission: Submission
    metadata: dict[str, Any]


class LaunchValidationError(Exception):
    """Raised when we cannot build a submission from the incoming request."""

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
    """Ensure payload metadata abides by the configured policy."""

    metadata = dict(metadata or {})
    submission_settings.enforce_metadata_policy(metadata)
    return metadata


def launch_web_validation_run(
    *,
    submission_build: SubmissionBuild,
    request: HttpRequest,
    workflow: Workflow,
) -> HttpResponseRedirect:
    """Launches a workflow run initiated through the HTML form.

    Args:
        request: Django request used to build redirect URLs and capture the actor.
        workflow: Workflow that should be executed.
        submission_build: Pre-built submission and metadata for the run.

    Returns:
        HttpResponseRedirect: Redirect to the run detail template for the new run.

    Raises:
        LaunchValidationError: If the workflow cannot accept new submissions.
        PermissionError: If the user is not allowed to execute the workflow.
    """

    submission_build: SubmissionBuild = build_submission_for_workflow_launch(
        workflow=workflow,
        user=request.user,
        project=workflow.project,
        submission_build=submission_build,
        skip_prechecks=True,
    )

    service = ValidationRunService()
    launch_result = service.launch(
        request=request,
        org=workflow.org,
        workflow=workflow,
        submission=submission_build.submission,
        metadata=submission_build.metadata,
        user_id=getattr(request.user, "id", None),
    )
    validation_run = launch_result.validation_run
    run_detail_url = reverse_with_org(
        "workflows:workflow_run_detail",
        request=request,
        kwargs={"pk": workflow.pk, "run_id": validation_run.pk},
    )
    return HttpResponseRedirect(run_detail_url)


def launch_api_validation_run(
    *,
    request: HttpRequest,
    workflow: Workflow,
    project: Project | None,
    serializer_factory: Callable[..., Any],
) -> APIResponse:
    """Launches a workflow run initiated by the REST API.

    Args:
        request: DRF/Django request received by the API endpoint.
        workflow: Workflow requested by the caller.
        project: Optional project context derived from request parameters.
        serializer_factory: Factory returning ValidationRunStartSerializer.

    Returns:
        APIResponse: Serializer payload, headers, and status for the run request.
    """

    try:
        submission_build: SubmissionBuild = build_submission_for_workflow_launch(
            request=request,
            workflow=workflow,
            user=request.user,
            project=project,
            serializer_factory=serializer_factory,
            multipart_payload=lambda: request.data,
        )
    except LaunchValidationError as exc:
        status_code = exc.status_code
        if status_code == HTTPStatus.FORBIDDEN:
            status_code = HTTPStatus.NOT_FOUND
        return APIResponse(exc.payload, status=status_code)

    service = ValidationRunService()
    try:
        launch_result: ValidationRunLaunchResults = service.launch(
            request=request,
            org=workflow.org,
            workflow=workflow,
            submission=submission_build.submission,
            metadata=submission_build.metadata,
            user_id=getattr(request.user, "id", None),
        )
    except PermissionError:
        payload = {"detail": _("You do not have permission to run this workflow.")}
        return APIResponse(payload, status=HTTPStatus.NOT_FOUND)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Run service errored for workflow %s", workflow.pk)
        payload = {"detail": _("Could not run the workflow. Please try again.")}
        return APIResponse(payload, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    validation_run: ValidationRun = launch_result.validation_run
    data = ValidationRunSerializer(validation_run).data
    per_attempt = int(getattr(settings, "VALIDATION_START_ATTEMPT_TIMEOUT", 5))
    location = request.build_absolute_uri(
        reverse(
            "api:validation-runs-detail",
            kwargs={"pk": validation_run.id},
        ),
    )
    headers = {"Location": location}
    if validation_run.status not in VALIDATION_RUN_TERMINAL_STATUSES:
        data["url"] = location
        data["poll"] = location
        headers["Retry-After"] = str(per_attempt)

    status_code = launch_result.status or HTTPStatus.CREATED
    return APIResponse(data=data, status=status_code, headers=headers)


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
        err_msg = _("Payload too large.")
        raise LaunchValidationError(
            detail=err_msg,
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
        except Exception as e:
            raise LaunchValidationError(
                detail=_("Invalid base64 payload."),
                code=WorkflowStartErrorCode.INVALID_PAYLOAD,
                status_code=400,
            ) from e

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
    """Handle Mode 2 (JSON envelope) submission."""
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
    """Handle Mode 3 (multipart/form-data) submissions.

    Args:
        workflow: Workflow accepting the submission.
        user: Authenticated user (may be anonymous for API token uses).
        project: Optional project the submission should be associated with.
        payload: Raw multipart payload dict (request.data).
        submission_settings: System submission policy settings.
        serializer_factory: Callable returning the DRF serializer for validation.

    Returns:
        SubmissionBuild: Persisted submission and metadata bundle.
    """
    return process_structured_payload(
        workflow=workflow,
        user=user,
        project=project,
        payload=payload,
        serializer_factory=serializer_factory,
        submission_settings=submission_settings,
    )


def build_submission_for_workflow_launch(
    *,
    workflow: Workflow,
    user: User,
    project: Project | None,
    request: HttpRequest | None = None,
    serializer_factory: Callable[..., Any] | None = None,
    multipart_payload=None,
    submission_settings=None,
    submission_build: SubmissionBuild | None = None,
    skip_prechecks: bool = False,
) -> SubmissionBuild:
    """Builds a Submission object for a workflow run request.

    The helper normalizes the incoming payload (raw body, JSON envelope, or
    multipart) and persists the resulting Submission. It is invoked by both
    `launch_api_validation_run` and `launch_web_validation_run` so that every
    entry point enforces the same workflow pre-checks before attempting to
    launch a run.

    Args:
        workflow: Workflow targeted by the request.
        user: User initiating the run.
        project: Project context resolved for the workflow.
        request: HTTP request carrying the submission payload (API only).
        serializer_factory: Factory that yields the DRF serializer used to
            validate structured payloads (required for API invocations).
        multipart_payload: Callable returning multipart data when request is not
            available (mainly for tests).
        submission_settings: Optional overrides for metadata policy enforcement.
        submission_build: Pre-built submission metadata (web form code path).

    Returns:
        SubmissionBuild: Saved submission and metadata ready for launch.
    """

    if not skip_prechecks:
        ensure_launch_preconditions(workflow=workflow, user=user)

    if submission_build is not None:
        return submission_build

    if serializer_factory is None:
        raise ValueError(
            _("serializer_factory is required when parsing submission data.")
        )

    if request is None:
        raise ValueError(_("request is required when parsing API submissions."))

    headers = {key: value for key, value in request.headers.items()}
    content_type_header, body_bytes = extract_request_basics(request)

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
            multipart_payload() if callable(multipart_payload) else multipart_payload
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


def ensure_launch_preconditions(*, workflow: Workflow, user: User) -> None:
    """Shared entry point for workflow readiness + permission checks."""

    ensure_workflow_ready_for_launch(workflow)
    ensure_user_can_launch_workflow(workflow=workflow, user=user)


def ensure_workflow_ready_for_launch(workflow: Workflow) -> None:
    """
    Guard conditions that prevent a workflow from running.
    """

    if not workflow.is_active:
        raise LaunchValidationError(
            detail="",
            code=WorkflowStartErrorCode.WORKFLOW_INACTIVE,
            status_code=HTTPStatus.CONFLICT,
        )
    if not workflow.steps.exists():
        raise LaunchValidationError(
            detail=_("This workflow has no steps defined and cannot be executed."),
            code=WorkflowStartErrorCode.NO_WORKFLOW_STEPS,
            status_code=HTTPStatus.BAD_REQUEST,
        )


def ensure_user_can_launch_workflow(*, workflow: Workflow, user: User) -> None:
    """Ensure the user can execute the workflow before creating submissions."""

    if not getattr(user, "is_authenticated", False):
        raise LaunchValidationError(
            detail=_("You do not have permission to run this workflow."),
            code=WorkflowStartErrorCode.PERMISSION_DENIED,
            status_code=HTTPStatus.FORBIDDEN,
        )

    if not user_has_executor_role(user, workflow):
        raise LaunchValidationError(
            detail=_("You do not have permission to run this workflow."),
            code=WorkflowStartErrorCode.PERMISSION_DENIED,
            status_code=HTTPStatus.FORBIDDEN,
        )
