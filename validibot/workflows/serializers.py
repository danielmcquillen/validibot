from rest_framework import serializers

from validibot.workflows.models import Workflow


class WorkflowSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workflow
        fields = [
            "id",
            "org",
            "user",
            "name",
            "uuid",
            "slug",
            "version",
            "is_active",
            "allowed_file_types",
        ]
        read_only_fields = [
            "id",
            "org",
            "user",
            "uuid",
        ]
