from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.backends import BaseBackend

from simplevalidations.users.constants import PermissionCode, RoleCode
from simplevalidations.users.models import Membership, Organization, User


@dataclass(frozen=True)
class PermissionDefinition:
    """
    Declarative schema for a permission code and its role bindings.
    """

    code: PermissionCode
    name: str
    app_label: str
    model: str
    roles: frozenset[str]


PERMISSION_DEFINITIONS: tuple[PermissionDefinition, ...] = (
    PermissionDefinition(
        code=PermissionCode.WORKFLOW_LAUNCH,
        name="Can launch workflows",
        app_label="workflows",
        model="workflow",
        roles=frozenset(
            (
                RoleCode.EXECUTOR,
                RoleCode.ADMIN,
                RoleCode.OWNER,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.VALIDATION_RESULTS_VIEW_ALL,
        name="Can view all validation results",
        app_label="validations",
        model="validationrun",
        roles=frozenset(
            (
                RoleCode.VALIDATION_RESULTS_VIEWER,
                RoleCode.ADMIN,
                RoleCode.OWNER,
                RoleCode.AUTHOR,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.VALIDATION_RESULTS_VIEW_OWN,
        name="Can view own validation results",
        app_label="validations",
        model="validationrun",
        roles=frozenset(
            (
                RoleCode.EXECUTOR,
                RoleCode.VALIDATION_RESULTS_VIEWER,
                RoleCode.ADMIN,
                RoleCode.OWNER,
                RoleCode.AUTHOR,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.WORKFLOW_VIEW,
        name="Can view workflows",
        app_label="workflows",
        model="workflow",
        roles=frozenset(
            (
                RoleCode.WORKFLOW_VIEWER,
                RoleCode.EXECUTOR,
                RoleCode.AUTHOR,
                RoleCode.ADMIN,
                RoleCode.OWNER,
                RoleCode.VALIDATION_RESULTS_VIEWER,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.WORKFLOW_EDIT,
        name="Can edit workflows",
        app_label="workflows",
        model="workflow",
        roles=frozenset(
            (
                RoleCode.AUTHOR,
                RoleCode.ADMIN,
                RoleCode.OWNER,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.ADMIN_MANAGE_ORG,
        name="Can manage organizations and members",
        app_label="users",
        model="organization",
        roles=frozenset(
            (
                RoleCode.ADMIN,
                RoleCode.OWNER,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.VALIDATOR_VIEW,
        name="Can view validators",
        app_label="validations",
        model="validator",
        roles=frozenset(
            (
                RoleCode.AUTHOR,
                RoleCode.ADMIN,
                RoleCode.OWNER,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.VALIDATOR_EDIT,
        name="Can create or edit validators",
        app_label="validations",
        model="validator",
        roles=frozenset(
            (
                RoleCode.AUTHOR,
                RoleCode.ADMIN,
                RoleCode.OWNER,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.ANALYTICS_VIEW,
        name="Can view analytics",
        app_label="validations",
        model="validationrun",
        roles=frozenset(
            (
                RoleCode.ANALYTICS_VIEWER,
                RoleCode.ADMIN,
                RoleCode.OWNER,
                RoleCode.AUTHOR,
            ),
        ),
    ),
    PermissionDefinition(
        code=PermissionCode.ANALYTICS_REVIEW,
        name="Can review analytics",
        app_label="validations",
        model="validationrun",
        roles=frozenset(
            (
                RoleCode.ANALYTICS_VIEWER,
                RoleCode.ADMIN,
                RoleCode.OWNER,
                RoleCode.AUTHOR,
            ),
        ),
    ),
)

PERMISSIONS_BY_CODE = {
    definition.code: definition for definition in PERMISSION_DEFINITIONS
}


def normalize_perm_code(perm: str | PermissionCode) -> PermissionCode | None:
    """
    Accept plain codenames (``workflow_launch``), Django-style strings
    (``workflows.workflow_launch``), or ``PermissionCode`` instances and
    normalize to a ``PermissionCode`` enum.
    """

    if isinstance(perm, PermissionCode):
        return perm
    if not isinstance(perm, str):
        return None
    _, _, codename = perm.rpartition(".")
    candidate = codename or perm
    if candidate in PermissionCode.values:
        return PermissionCode(candidate)
    return None


def roles_for_permission(perm: PermissionCode) -> frozenset[str]:
    """Return the role codes that grant the provided permission."""
    definition = PERMISSIONS_BY_CODE.get(perm)
    return definition.roles if definition else frozenset()


def membership_grants_permission(
    membership: Membership | None, perm: str | PermissionCode
) -> bool:
    """
    Evaluate whether a membership satisfies the given permission using the
    centralized role-to-permission mapping.
    """

    perm_code = normalize_perm_code(perm)
    if not perm_code or membership is None or not membership.is_active:
        return False
    if membership.has_role(RoleCode.OWNER):
        return True

    definition = PERMISSIONS_BY_CODE.get(perm_code)
    if not definition:
        return False

    return membership.has_any_role(set(definition.roles))


class OrgPermissionBackend(BaseBackend):
    """
    Permission backend that evaluates Django ``has_perm`` calls against
    organization-scoped membership roles.

    It keeps SimpleValidations' per-org RBAC while letting callers use
    first-class Django permission checks with an object carrying an ``org``
    or ``org_id`` attribute (e.g., Workflow, ValidationRun, Organization).
    """

    supports_object_permissions = True

    def authenticate(self, request, username=None, password=None, **kwargs):
        return None

    def has_perm(self, user: User, perm: str, obj=None) -> bool:
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True

        perm_code = normalize_perm_code(perm)
        if not perm_code:
            return False

        perm_definition = PERMISSIONS_BY_CODE.get(perm_code)
        if not perm_definition:
            return False

        org = self._resolve_org(user=user, obj=obj)
        if org is None:
            return False

        membership = (
            Membership.objects.filter(user=user, org=org, is_active=True)
            .select_related("org")
            .prefetch_related("membership_roles__role")
            .first()
        )
        if membership is None:
            return False
        if membership.has_role(RoleCode.OWNER):
            return True

        if (
            perm_code == PermissionCode.VALIDATION_RESULTS_VIEW_OWN
            and obj is not None
            and getattr(obj, "user_id", None) == getattr(user, "id", None)
        ):
            return True

        return membership.has_any_role(set(perm_definition.roles))

    def get_all_permissions(
        self,
        user_obj: User,
        obj=None,
    ) -> set[str]:
        """
        Minimal implementation required by BaseBackend for completeness.
        """

        if not user_obj or not getattr(user_obj, "is_authenticated", False):
            return set()
        org = self._resolve_org(user=user_obj, obj=obj)
        if org is None:
            return set()

        membership = (
            Membership.objects.filter(user=user_obj, org=org, is_active=True)
            .prefetch_related("membership_roles__role")
            .first()
        )
        if membership is None:
            return set()
        codes = set()
        for definition in PERMISSION_DEFINITIONS:
            if membership.has_any_role(set(definition.roles)):
                codes.add(f"{definition.app_label}.{definition.code.value}")
        return codes

    def _resolve_org(
        self,
        *,
        user: User,
        obj,
    ) -> Organization | None:
        if isinstance(obj, Organization):
            return obj

        if hasattr(obj, "org"):
            org = getattr(obj, "org")
            if isinstance(org, Organization):
                return org

        org_id = getattr(obj, "org_id", None) if obj is not None else None
        if org_id:
            return Organization.objects.filter(id=org_id).first()

        return getattr(user, "current_org", None)
