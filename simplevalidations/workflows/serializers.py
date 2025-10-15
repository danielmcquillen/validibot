from rest_framework import serializers

from simplevalidations.workflows.models import Workflow


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
        ]
