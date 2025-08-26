from rest_framework import serializers

from roscoe.validations.models import ValidationRun


class ValidationRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ValidationRun
        fields = "__all__"