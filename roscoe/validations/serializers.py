from rest_framework import serializers


class ValidationRunStartSerializer(serializers.Serializer):
    workflow = serializers.IntegerField(required=True)
    document = serializers.JSONField(required=True)
    metadata = serializers.JSONField(required=False, default=dict)
