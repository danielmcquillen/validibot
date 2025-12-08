from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.relations import PrimaryKeyRelatedField
from rest_framework.relations import SlugRelatedField

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.models import ValidationRun
from validibot.workflows.constants import SUPPORTED_CONTENT_TYPES

CONTENT_TYPE_BY_FILE_TYPE = {
    file_type: content_type
    for content_type, file_type in SUPPORTED_CONTENT_TYPES.items()
}


class ValidationRunSerializer(serializers.ModelSerializer):
    """
    Provides a read-only view into the status of a ValidationRun.
    This is the serializer used by the API to return information
    to the user about an existing, in-progress or completed run.
    """

    workflow = PrimaryKeyRelatedField(read_only=True)

    workflow_slug = SlugRelatedField(
        source="workflow",
        read_only=True,
        slug_field="slug",
    )

    org = SlugRelatedField(
        read_only=True,
        slug_field="slug",
    )

    user = PrimaryKeyRelatedField(read_only=True)

    submission = PrimaryKeyRelatedField(
        read_only=True,
    )

    steps = serializers.SerializerMethodField()

    def get_steps(self, obj: ValidationRun) -> list[dict]:
        step_runs = list(obj.step_runs.all())
        if not step_runs:
            return []
        step_runs.sort(key=lambda sr: (sr.step_order or 0, sr.pk))
        payload: list[dict] = []
        for step_run in step_runs:
            workflow_step = getattr(step_run, "workflow_step", None)
            findings = list(step_run.findings.all())
            payload.append(
                {
                    "step_id": step_run.workflow_step_id or step_run.pk,
                    "name": getattr(workflow_step, "name", _("Step")),
                    "status": step_run.status,
                    "issues": [
                        {
                            "id": finding.id,
                            "message": finding.message,
                            "path": finding.path,
                            "severity": finding.severity,
                            "code": finding.code,
                            "assertion_id": finding.ruleset_assertion_id,
                        }
                        for finding in findings
                    ],
                    "error": step_run.error,
                },
            )
        return payload

    class Meta:
        model = ValidationRun
        fields = [
            "id",
            "status",
            "source",
            "org",
            "user",
            "workflow",
            "workflow_slug",
            # "project", # Not implemented yet...
            "submission",
            "started_at",
            "ended_at",
            "duration_ms",
            # "summary", # We use "steps" field to dig into summary and get steps.
            "steps",
            "error",
        ]
        read_only_fields = fields


class FlexibleContentField(serializers.CharField):
    """
    Accepts either a string payload or JSON-like objects.
    Dict/list values are passed through for later coercion in ``validate``.
    """

    def to_internal_value(self, data):
        if isinstance(data, (dict, list)):
            return data
        if isinstance(data, (bytes, bytearray)):
            try:
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                data = data.decode("latin-1")
        return super().to_internal_value(data)


class ValidationRunStartSerializer(serializers.Serializer):
    """
    Normalizes Workflow start requests for JSON-envelope and multipart inputs.

    The view instantiates this serializer for:
      * Mode 2 (application/json envelope) - we accept strings, dicts, or lists
        in ``content`` and coerce them to text via ``FlexibleContentField``.
      * Mode 3 (multipart/form-data uploads) - we expect a ``file`` part plus
        optional metadata overrides.

    Validated data always contains exactly one of ``normalized_content`` (text)
    or ``file``; downstream submission creation relies on that contract.
    """

    # Optional org for sanity checking (not required; view can enforce match)
    org = serializers.IntegerField(required=False)

    # Envelope textual content
    content = FlexibleContentField(required=False)  # plain or base64 text
    content_type = serializers.CharField(required=False)
    content_encoding = serializers.ChoiceField(
        choices=["base64"],
        required=False,
        allow_null=True,
        help_text="Only 'base64' if provided.",
    )

    filename = serializers.CharField(required=False, allow_blank=True)
    metadata = serializers.JSONField(required=False, default=dict)

    # Multipart binary file
    file = serializers.FileField(required=False)

    def to_internal_value(self, data: Any):
        """
        Allow metadata to arrive as a JSON string (multipart) and coerce.
        """
        iv = super().to_internal_value(data)
        meta = iv.get("metadata")
        if isinstance(meta, str):
            meta = meta.strip()
            if meta:
                try:
                    iv["metadata"] = json.loads(meta)
                except json.JSONDecodeError as e:
                    raise serializers.ValidationError(
                        {
                            "metadata": f"Invalid JSON: {e}",
                        },
                    ) from e
            else:
                iv["metadata"] = {}
        return iv

    def _map_content_type(self, ct: str):
        if not ct:
            raise serializers.ValidationError(
                {
                    "content_type": _("content_type is required."),
                },
            )
        lowered = ct.split(";")[0].strip().lower()
        if lowered not in SUPPORTED_CONTENT_TYPES:
            raise serializers.ValidationError(
                {
                    "content_type": _(
                        "Unsupported content_type '%(ct)s'. Supported: %(supported)s"
                    )
                    % {
                        "ct": ct,
                        "supported": ", ".join(SUPPORTED_CONTENT_TYPES),
                    },
                },
            )
        return lowered, SUPPORTED_CONTENT_TYPES[lowered]

    def validate(self, attrs):
        file_obj = attrs.get("file")
        raw_content = attrs.get("content")
        content_type = attrs.get("content_type")
        content_encoding = attrs.get("content_encoding")
        filename = attrs.get("filename")

        normalized_content = raw_content
        if isinstance(normalized_content, (dict, list)):
            # Preserve a consistent storage format by converting Python lists/dicts
            # into JSON strings. This matches the raw JSON payload workflows expect
            # when they ingest submissions later in the pipeline.
            normalized_content = json.dumps(normalized_content)
        elif normalized_content is not None and not isinstance(normalized_content, str):
            normalized_content = str(normalized_content)

        self._check_content(normalized_content, content_encoding)

        # Exactly one of file OR content
        if (file_obj is None and normalized_content is None) or (
            file_obj is not None and normalized_content is not None
        ):
            raise serializers.ValidationError(
                _(
                    "Provide exactly one of 'file' (multipart) "
                    "or 'content' (JSON envelope).",
                ),
            )

        # File path
        if file_obj is not None:
            # tries to read file_obj.content_type (Django's UploadedFile usually
            # sets this from the multipart part's header)
            guessed_ct = content_type or getattr(file_obj, "content_type", None)
            if not guessed_ct:
                raise serializers.ValidationError(
                    {
                        "content_type": _(
                            "content_type required (or detectable from file).",
                        ),
                    },
                )
            lowered, file_type = self._map_content_type(guessed_ct)
            attrs["content_type"] = lowered
            attrs["file_type"] = file_type
            return attrs

        # Textual path
        lowered = file_type = None
        if content_type:
            lowered, file_type = self._map_content_type(content_type)
        else:
            lowered, file_type = self._infer_content_type(
                raw_content=raw_content,
                normalized_content=normalized_content,
                filename=filename,
                content_encoding=content_encoding,
            )
            if lowered is None or file_type is None:
                raise serializers.ValidationError(
                    {
                        "content_type": _("content_type is required with content."),
                    },
                )

        attrs["content_type"] = lowered
        attrs["file_type"] = file_type

        # Base64 decode if requested
        if content_encoding == "base64":
            if normalized_content is None:
                raise serializers.ValidationError(
                    {
                        "content": _("Invalid base64 content."),
                    },
                )
            try:
                decoded = base64.b64decode(normalized_content, validate=True)
            except (binascii.Error, ValueError) as e:
                raise serializers.ValidationError(
                    {
                        "content": _("Invalid base64 content."),
                    },
                ) from e
            try:
                normalized_content = decoded.decode("utf-8")
            except UnicodeDecodeError:
                # Fallback...best effort
                normalized_content = decoded.decode("latin-1")

        attrs["normalized_content"] = normalized_content
        attrs["content"] = normalized_content
        return attrs

    def _infer_content_type(
        self,
        *,
        raw_content: Any,
        normalized_content: str | None,
        filename: str | None,
        content_encoding: str | None,
    ) -> tuple[str | None, SubmissionFileType | None]:
        """
        Attempt to derive a supported content type when the client did not
        supply one explicitly.
        """
        guess = self._guess_file_type(
            raw_content=raw_content,
            normalized_content=normalized_content,
            filename=filename,
            content_encoding=content_encoding,
        )
        if guess and guess in CONTENT_TYPE_BY_FILE_TYPE:
            ct = CONTENT_TYPE_BY_FILE_TYPE[guess]
            return ct, guess

        request = self.context.get("request") if hasattr(self, "context") else None
        if request:
            for header_name in (
                "X-Content-Type",
                "X-Submission-Content-Type",
                "Content-Type",
            ):
                header_ct = request.headers.get(header_name)
                if not header_ct:
                    continue
                try:
                    return self._map_content_type(header_ct)
                except serializers.ValidationError:
                    continue

        return None, None

    def _guess_file_type(
        self,
        *,
        raw_content: Any,
        normalized_content: str | None,
        filename: str | None,
        content_encoding: str | None,
    ) -> SubmissionFileType | None:
        """
        Lightweight heuristics so envelopes can omit content_type when obvious.
        """
        name = (filename or "").lower()
        if name.endswith((".json", ".epjson")):
            return SubmissionFileType.JSON
        if name.endswith(".xml"):
            return SubmissionFileType.XML
        if name.endswith(".idf") or "energyplus" in name:
            return SubmissionFileType.TEXT

        if isinstance(raw_content, (dict, list)):
            return SubmissionFileType.JSON

        if content_encoding == "base64":
            return None  # cannot inspect encoded payload safely

        sample = None
        if isinstance(raw_content, str):
            sample = raw_content.lstrip()
        elif isinstance(normalized_content, str):
            sample = normalized_content.lstrip()

        if not sample:
            return None
        if sample.startswith(("{", "[")):
            return SubmissionFileType.JSON
        if sample.startswith("<"):
            return SubmissionFileType.XML
        return None

    def _check_content(self, content: str | None, content_encoding: str | None) -> bool:
        """
        Basic sanity checks on textual content field.
        """
        # cap on input JSON field length to avoid massive strings
        if content is None:
            return False
        if content_encoding == "base64":
            # Base64 inflates size by ~33%, so limit pre-decode size
            max_b64_b = getattr(settings, "SUBMISSION_BASE64_MAX_BYTES", 13_000_000)
            if len(content.encode("utf-8", errors="ignore")) > max_b64_b:
                raise serializers.ValidationError(
                    {
                        "content": _("Base64 content exceeds size limit."),
                    },
                )
        else:
            max_inline_b = getattr(settings, "SUBMISSION_INLINE_MAX_BYTES", 10_000_000)
            # Base64 payload size will be enforced before decode in validate()
            if len(content.encode("utf-8", errors="ignore")) > max_inline_b:
                raise serializers.ValidationError(
                    {
                        "content": _("Inline content exceeds size limit."),
                    },
                )
        return True
