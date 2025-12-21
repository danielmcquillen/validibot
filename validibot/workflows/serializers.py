from rest_framework import serializers

from validibot.workflows.models import Workflow


class WorkflowSerializer(serializers.ModelSerializer):
    """
    Serializer for workflow API responses.

    Provides read-only workflow information for CLI and API consumers.
    Internal fields like user and numeric IDs are excluded to minimize
    exposed data. See ADR-2025-12-22 for rationale.
    """

    org_slug = serializers.SlugRelatedField(
        source="org",
        slug_field="slug",
        read_only=True,
        help_text="The organization's slug identifier",
    )

    class Meta:
        model = Workflow
        fields = [
            "id",
            "uuid",
            "slug",
            "name",
            "version",
            "org_slug",
            "is_active",
            "allowed_file_types",
        ]
        read_only_fields = fields
