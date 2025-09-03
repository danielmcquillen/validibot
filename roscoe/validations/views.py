import django_filters
from django.conf import settings
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from validations.services.validation_run import ValidationJobLauncher

from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.models import ValidationRun
from roscoe.validations.serializers import ValidationRunSerializer
from roscoe.validations.serializers import ValidationRunStartSerializer


class ValidationRunFilter(django_filters.FilterSet):
    class Meta:
        model = ValidationRun
        fields = []  # We define filters explicitly above

    status = django_filters.ChoiceFilter(choices=ValidationRunStatus.choices)
    workflow = django_filters.NumberFilter()
    submission = django_filters.NumberFilter()
    after = django_filters.DateFilter(field_name="created", lookup_expr="gte")
    before = django_filters.DateFilter(field_name="created", lookup_expr="lte")
    on = django_filters.DateFilter(field_name="created", lookup_expr="date")


class ValidationRunViewSet(viewsets.ModelViewSet):
    queryset = ValidationRun.objects.all()
    serializer_class = ValidationRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ValidationRunFilter
    ordering_fields = ["created", "id", "status"]
    ordering = ["-created", "-id"]

    def get_queryset(self):
        current_org = self.request.user.get_current_org()
        return super().get_queryset().filter(org=current_org)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        data = ValidationRunSerializer(instance).data
        if instance.status in (
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
            getattr(ValidationRunStatus, "CANCELED", "canceled"),
            getattr(ValidationRunStatus, "TIMED_OUT", "timed_out"),
        ):
            return Response(data, status=status.HTTP_200_OK)
        return Response(
            data,
            status=status.HTTP_202_ACCEPTED,
            headers={
                "Retry-After": str(
                    getattr(settings, "VALIDATION_START_ATTEMPT_TIMEOUT", 5),
                ),
            },
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="start",
        permission_classes=[permissions.IsAuthenticated],
    )
    def start(self, request):
        serializer = ValidationRunStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        current_org = user.get_current_org()
        workflow = serializer.validated_data["workflow"]
        submission = serializer.validated_data.get("submission")
        document = serializer.validated_data.get("document")
        metadata = serializer.validated_data.get("metadata", {})

        if workflow.org_id != current_org.id:
            return Response(
                {"detail": "Workflow not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if submission and submission.org_id != current_org.id:
            return Response(
                {"detail": "Submission not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        launcher = ValidationJobLauncher()
        return launcher.launch(
            request=request,
            org=current_org,
            workflow=workflow,
            submission=submission,
            document=document,
            metadata=metadata,
            user_id=getattr(user, "id", None),
        )
