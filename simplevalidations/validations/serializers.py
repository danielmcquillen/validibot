from __future__ import annotations

import base64
import binascii
import json
from typing import Any
from uuid import UUID

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.relations import PrimaryKeyRelatedField
from rest_framework.relations import SlugRelatedField

from simplevalidations.validations.models import ValidationRun
from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES


class ValidationRunSerializer(serializers.ModelSerializer):
    """
    Provides a read-only view into the status of a ValidationRun.
    This is the serializer used by the API to return information
    to the user about an existing, in-progress or completed run.
    """

    workflow = SlugRelatedField(
        read_only=True,
        slug_field="slug",
    )

    org = SlugRelatedField(
        read_only=True,
        slug_field="slug",
    )

    submission = PrimaryKeyRelatedField(
        read_only=True,
    )

    # Map steps to summary["steps"], defaulting to []
    steps = serializers.SerializerMethodField()

    def get_steps(self, obj: ValidationRun) -> list[dict]:
        summary = getattr(obj, "summary", None)
        if not summary:
            return []
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except Exception:
                return []
        if isinstance(summary, dict):
            steps = summary.get("steps")
            return steps if isinstance(steps, list) else []
        return []

    class Meta:
        model = ValidationRun
        fields = [
            "id",
            "status",
            "org",
            "workflow",
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
      * Mode 2 (application/json envelope) – we accept strings, dicts, or lists
        in ``content`` and coerce them to text via ``FlexibleContentField``.
      * Mode 3 (multipart/form-data uploads) – we expect a ``file`` part plus
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
        content = attrs.get("content")
        content_type = attrs.get("content_type")
        content_encoding = attrs.get("content_encoding")

        if isinstance(content, (dict, list)):
            content = json.dumps(content)
        elif content is not None and not isinstance(content, str):
            content = str(content)

        self._check_content(content, content_encoding)

        # Exactly one of file OR content
        if (file_obj is None and content is None) or (
            file_obj is not None and content is not None
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
        if content_type is None:
            raise serializers.ValidationError(
                {
                    "content_type": _("content_type is required with content."),
                },
            )
        lowered, file_type = self._map_content_type(content_type)
        attrs["content_type"] = lowered
        attrs["file_type"] = file_type

        # Base64 decode if requested
        if content_encoding == "base64":
            try:
                decoded = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError) as e:
                raise serializers.ValidationError(
                    {
                        "content": _("Invalid base64 content."),
                    },
                ) from e
            try:
                content = decoded.decode("utf-8")
            except UnicodeDecodeError:
                # Fallback...best effort
                content = decoded.decode("latin-1")
        attrs["normalized_content"] = content
        attrs["content"] = content
        return attrs

    def _check_content(self, content: str | None, content_encoding: str | None) -> bool:
        """
        Basic sanity checks on textual content field.
        """
        # cap on input JSON field length to avoid massive strings
        if content is None:
            return
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
