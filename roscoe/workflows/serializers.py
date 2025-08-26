from rest_framework import serializers

from roscoe.workflows.models import Workflow


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
        ]
