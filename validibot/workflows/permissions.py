from __future__ import annotations

from rest_framework import permissions
from rest_framework.exceptions import ValidationError

from validibot.users.models import Organization
from validibot.users.permissions import PermissionCode


class WorkflowPermission(permissions.BasePermission):
    """
    Enforce workflow access rules for the API:

    - list/retrieve: any authenticated member of the workflow's org
        (handled in queryset)
    - create/update/delete: only users with manager roles (owner/admin/author)
    - start action: checked downstream to preserve existing 404 behaviour
    """

    message = "You do not have permission to perform this action on workflows."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False

        if view.action in ("list", "retrieve", "start_validation"):
            return True

        if view.action == "create":
            org = self._resolve_target_org(request)
            return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, org)

        # update/destroy permissions are enforced at the object level
        return True

    def has_object_permission(self, request, view, obj) -> bool:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False

        if view.action in ("retrieve", "list", "start_validation"):
            return True

        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, obj)

    def _resolve_target_org(self, request) -> Organization:
        """
        Determine which org the request is targeting for create operations.
        """

        org_id = request.data.get("org") or getattr(
            request.user, "current_org_id", None
        )
        if not org_id:
            raise ValidationError({"org": "Organization is required."})

        try:
            org = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            raise ValidationError({"org": "Organization not found."}) from exc

        return org
