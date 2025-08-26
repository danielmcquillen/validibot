from django.shortcuts import render
from rest_framework import permissions, viewsets

from roscoe.workflows.models import Workflow
from roscoe.workflows.serializers import WorkflowSerializer

# API Views
# ------------------------------------------------------------------------------


class WorkflowViewSet(viewsets.ModelViewSet):
    queryset = Workflow.objects.all()
    serializer_class = WorkflowSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        current_org = self.request.user.get_current_org()
        qs = qs.filter(org=current_org)
        return qs


# Template Views
# ------------------------------------------------------------------------------

# TODO ...
