import logging
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from simplevalidations.actions.utils import create_default_actions
from simplevalidations.projects.models import Project
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import Membership
from simplevalidations.users.models import Organization
from simplevalidations.users.models import Role
from simplevalidations.users.models import User
from simplevalidations.validations.utils import create_default_validators
from simplevalidations.workflows.models import Workflow

logger = logging.getLogger(__name__)


def _manager_for(model):
    return getattr(model, "all_objects", model._default_manager)  # noqa: SLF001


class Command(BaseCommand):
    """
    Prepare baseline data for a Validibot deployment.
    This backfills the behaviours that used to live in data migrations so that
    rebuilding migrations never drops critical records.
    """

    def __init__(self, stdout=None, stderr=None, no_color=False):
        super().__init__(stdout=stdout, stderr=stderr, no_color=no_color)

    def handle(self, *args, **options):
        self.stdout.write("Setting up Validibot.")

        self.stdout.write("Ensuring roles...")
        self._ensure_roles()

        self.stdout.write("Ensuring personal workspaces...")
        self._ensure_personal_workspaces()

        self.stdout.write("Ensuring default projects for personal orgs...")
        self._ensure_default_projects_for_personal_orgs()

        self.stdout.write("Ensuring default validators...")
        self._ensure_default_validators()

        self.stdout.write("Assigning default projects to workflows...")
        self._assign_default_projects_to_workflows()

        self.stdout.write("Setting up local superuser...")
        self._setup_local_superuser()

        self.stdout.write("Setting up default actions...")
        self._setup_default_actions()

        self.stdout.write("DONE setting up Validibot")

    def _setup_default_actions(self):
        create_default_actions()

    # ---------------------------------------------------------------------
    # Superuser helpers
    # ---------------------------------------------------------------------
    def _setup_local_superuser(self):
        """
        Set up a local superuser for development if the env vars are present.
        """
        username = getattr(settings, "SUPERUSER_USERNAME", None)
        password = getattr(settings, "SUPERUSER_PASSWORD", None)
        email = getattr(settings, "SUPERUSER_EMAIL", None)
        name = getattr(settings, "SUPERUSER_NAME", None)

        if not username:
            return

        user = None
        try:  # noqa: SIM105
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            pass

        if not user:
            logger.info("Creating user '%s'", username)
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
            )
        else:
            logger.info("Updating existing user '%s'", username)
            if password:
                user.set_password(password)
        user.name = name
        user.is_staff = True
        user.is_superuser = True
        user.save()

        if email:
            user.emailaddress_set.create(email=email, primary=True, verified=True)

    # ---------------------------------------------------------------------
    # Role seeding
    # ---------------------------------------------------------------------
    def _ensure_roles(self):
        for code, label in RoleCode.choices:
            Role.objects.update_or_create(
                code=code,
                defaults={"name": str(label)},
            )

    # ---------------------------------------------------------------------
    # Workspace + project helpers
    # ---------------------------------------------------------------------
    def _workspace_name(self, user: User) -> str:
        name = (user.name or "").strip() or (user.username or "").strip() or "Workspace"
        if name.endswith("s"):
            return f"{name}' Workspace"
        return f"{name}'s Workspace"

    def _unique_slug(
        self,
        model,
        base: str,
        *,
        prefix: str = "",
        filter_kwargs=None,
    ) -> str:
        base_slug = slugify(base) or uuid4().hex[:8]
        if prefix:
            base_slug = f"{prefix}{base_slug}"
        slug = base_slug
        counter = 2
        manager = _manager_for(model)
        lookup = dict(filter_kwargs or {})
        while manager.filter(slug=slug, **lookup).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    def _ensure_default_project(self, org, project):
        manager = _manager_for(project)
        project = manager.filter(org=org, is_default=True).order_by("id").first()
        if project:
            if not project.is_active:
                project.is_active = True
                project.deleted_at = None
                project.save(update_fields=["is_active", "deleted_at"])
            return project

        name = "Default Project"
        slug = self._unique_slug(
            Project,
            name,
            prefix="default-",
            filter_kwargs={"org": org},
        )
        project = Project.objects.create(
            org=org,
            name=name,
            description="",
            slug=slug,
            is_default=True,
            is_active=True,
        )
        return project

    def _ensure_personal_workspaces(self):
        roles_needed = {
            code: Role.objects.filter(code=code).first()
            for code in (RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR)
        }
        missing = [code for code, role in roles_needed.items() if role is None]
        if missing:
            logger.warning(
                "Cannot assign roles %s – they do not exist.",
                ", ".join(missing),
            )

        for user in User.objects.all():
            membership = (
                Membership.objects.filter(user=user, org__is_personal=True)
                .select_related("org")
                .first()
            )
            if membership:
                roles_to_add = [role for role in roles_needed.values() if role]
                if roles_to_add:
                    membership.roles.add(*roles_to_add)
                self._ensure_default_project(membership.org, Project)
                if user.current_org_id != membership.org_id:
                    user.current_org = membership.org
                    user.save(update_fields=["current_org"])
                continue

            workspace_name = self._workspace_name(user)
            slug = self._unique_slug(
                Organization,
                workspace_name,
                prefix="workspace-",
            )
            org = Organization.objects.create(
                name=workspace_name,
                slug=slug,
                is_personal=True,
            )
            membership = Membership.objects.create(
                user=user,
                org=org,
                is_active=True,
            )
            roles_to_add = [role for role in roles_needed.values() if role]
            if roles_to_add:
                membership.roles.add(*roles_to_add)
            project = self._ensure_default_project(org, Project)
            user.current_org = org
            user.save(update_fields=["current_org"])
            logger.info(
                "Created personal workspace '%s' with default project '%s' for user %s",
                org.slug,
                project.slug,
                user.username,
            )

    def _ensure_default_projects_for_personal_orgs(self):
        for org in Organization.objects.filter(is_personal=True):
            self._ensure_default_project(org, Project)

    # ---------------------------------------------------------------------
    # Feature defaults
    # ---------------------------------------------------------------------
    def _ensure_default_validators(self):
        created, updated = create_default_validators()
        if created:
            logger.info("Created %d default validators.", created)
        if updated:
            logger.info("Updated %d existing default validators.", updated)

    def _assign_default_projects_to_workflows(self):
        for workflow in Workflow.objects.filter(project__isnull=True).select_related(
            "org"
        ):
            if not workflow.org:
                continue
            project = (
                Project.objects.filter(org=workflow.org, is_default=True).first()
                or _manager_for(Project)
                .filter(org=workflow.org, is_default=True)
                .first()
            )
            if not project:
                project = self._ensure_default_project(workflow.org, Project)
            if project:
                Workflow.objects.filter(pk=workflow.pk).update(project=project)
