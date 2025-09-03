import base64
import binascii
import json
from typing import Any

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from roscoe.submissions.constants import SubmissionFileType
from roscoe.validations.models import ValidationRun


class ValidationRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ValidationRun
        fields = [
            "id",
            "org",
            "workflow",
            "submission",
            "status",
            "document",
            "metadata",
            "created",
            "started_at",
            "ended_at",
            "log",
            "report",
        ]
        read_only_fields = fields


class ValidationRunStartSerializer(serializers.Serializer):
    """
    Normalizes all supported input shapes to a consistent payload
    your view can use to create a Submission:
      - file (multipart)
      - document (inline text)
      - document_json (typed JSON -> turned into document)
      - content_b64 (base64 -> a bytes file)
      - upload_id (by-reference; the view resolves this)
    """

    # Injected by view from URL
    workflow = serializers.IntegerField(required=True)

    # Exactly one of these must be provided:
    file = serializers.FileField(required=False, allow_empty_file=False)  # multipart
    document = serializers.CharField(required=False)  # inline text (XML/JSON/IDF)
    document_json = serializers.JSONField(
        required=False
    )  # typed JSON -> normalized to document
    content_b64 = serializers.CharField(required=False)  # base64-encoded file content
    upload_id = serializers.CharField(required=False)  # by-reference upload key

    # Optional hints
    filename = serializers.CharField(required=False, allow_blank=True)
    file_type = serializers.ChoiceField(
        choices=SubmissionFileType.choices, required=False
    )

    # Metadata: allow both JSON-native and "stringified JSON" (common in multipart)
    metadata = serializers.JSONField(required=False, default=dict)

    def to_internal_value(self, data: Any):
        """
        Make multipart 'metadata' (string) behave like a JSON object if needed.
        """
        iv = super().to_internal_value(data)
        md = iv.get("metadata")
        if isinstance(md, str):
            md = md.strip()
            if md:
                try:
                    iv["metadata"] = json.loads(md)
                except json.JSONDecodeError as e:
                    raise serializers.ValidationError(
                        {"metadata": f"Invalid JSON: {e}"}
                    ) from e
            else:
                iv["metadata"] = {}
        return iv

    def validate(self, attrs):
        file = attrs.get("file")
        doc_text = attrs.get("document")
        doc_json = attrs.get("document_json")
        content_b64 = attrs.get("content_b64")
        upload_id = attrs.get("upload_id")

        provided = [
            v
            for v in (file, doc_text, doc_json, content_b64, upload_id)
            if v is not None
        ]
        if len(provided) != 1:
            err_msg = _(
                "Provide exactly one of file, document, document_json, "
                "content_b64, or upload_id.",
            )
            raise serializers.ValidationError(err_msg)

        # document_json -> normalize to document
        if doc_json is not None:
            attrs["document"] = json.dumps(doc_json, separators=(",", ":"))
            attrs["file_type"] = attrs.get("file_type") or SubmissionFileType.JSON

        # base64 -> validate now; decoding of bytes is done in the view
        if content_b64 is not None:
            try:
                # quick validation; we don't keep decoded bytes here to avoid
                # large memory in serializer
                base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError) as e:
                err_msg = _("Invalid base64 content.")
                raise serializers.ValidationError(err_msg) from e

        return attrs
