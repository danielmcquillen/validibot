from rest_framework import serializers
from rest_framework.reverse import reverse

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


class OrgScopedWorkflowSerializer(serializers.ModelSerializer):
    """
    Serializer for org-scoped workflow API responses.

    Used by the new org-scoped API routes (ADR-2026-01-06).
    Includes a `url` field pointing to the canonical API endpoint.
    """

    org_slug = serializers.SlugRelatedField(
        source="org",
        slug_field="slug",
        read_only=True,
        help_text="The organization's slug identifier",
    )

    url = serializers.SerializerMethodField(
        help_text="Canonical API URL for this workflow",
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
            "url",
        ]
        read_only_fields = fields

    def get_url(self, obj: Workflow) -> str:
        """Generate the canonical org-scoped API URL for this workflow."""
        request = self.context.get("request")
        org_slug = self.context.get("org_slug") or obj.org.slug
        return reverse(
            "api:org-workflows-detail",
            kwargs={"org_slug": org_slug, "pk": obj.slug},
            request=request,
        )
