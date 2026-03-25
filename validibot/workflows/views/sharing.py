"""Guest access, invites, and sharing views.

Views for accepting workflow invites, listing guest workflows, managing
sharing settings (visibility, guest access grants, invitations), and
invite lifecycle (create, cancel, resend, revoke).
"""

import logging
from http import HTTPStatus

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import ListView
from django.views.generic import TemplateView

from validibot.core.utils import reverse_with_org
from validibot.workflows.mixins import WorkflowObjectMixin

logger = logging.getLogger(__name__)


# Workflow Invite Views
# ------------------------------------------------------------------------------


class WorkflowInviteAcceptView(View):
    """
    Handle workflow invite acceptance.

    This view handles the invite accept flow:
    1. For logged-in users: Accepts the invite immediately and redirects to workflow
    2. For anonymous users: Stores the invite token in session and redirects to signup

    The invite token is passed as a URL parameter.
    """

    WORKFLOW_INVITE_SESSION_KEY = "workflow_invite_token"

    def get(self, request, token):
        from validibot.workflows.models import WorkflowInvite

        invite = get_object_or_404(
            WorkflowInvite.objects.select_related("workflow", "inviter"),
            token=token,
        )

        if not invite.is_pending:
            messages.error(
                request,
                _("This invite is no longer valid (status: %(status)s).")
                % {"status": invite.get_status_display()},
            )
            return HttpResponseRedirect(reverse("home:home"))

        if request.user.is_authenticated:
            # Accept immediately for logged-in users
            try:
                grant = invite.accept(user=request.user)
                # Send acceptance notification to the inviter
                from validibot.workflows.emails import (
                    send_workflow_invite_accepted_email,
                )

                send_workflow_invite_accepted_email(grant)
                messages.success(
                    request,
                    _(
                        "You now have access to the workflow '%(name)s'. "
                        "You can run validations on this workflow."
                    )
                    % {"name": invite.workflow.name},
                )
                # Redirect to the workflow launch page
                return HttpResponseRedirect(
                    reverse(
                        "workflows:workflow_launch",
                        kwargs={"pk": invite.workflow.pk},
                    ),
                )
            except ValueError as e:
                messages.error(request, str(e))
                return HttpResponseRedirect(reverse("home:home"))

        # For anonymous users, store token in session and redirect to signup
        request.session[self.WORKFLOW_INVITE_SESSION_KEY] = str(token)
        messages.info(
            request,
            _(
                "Please sign up or log in to accept your invitation "
                "to workflow '%(name)s'."
            )
            % {"name": invite.workflow.name},
        )
        return HttpResponseRedirect(reverse("account_signup"))


# Guest Workflow Views
# ------------------------------------------------------------------------------


class GuestWorkflowListView(LoginRequiredMixin, ListView):
    """
    List workflows that a guest user has access to via WorkflowAccessGrants.

    This view is for workflow guests (users with grants but no org memberships).
    It shows workflows from all organizations the user has been granted access to,
    with the org name displayed on each workflow card.
    """

    template_name = "workflows/guest_workflow_list.html"
    context_object_name = "workflows"
    paginate_by = 20

    def get_queryset(self):
        from validibot.workflows.models import Workflow

        # Get workflows the user has grants for
        return (
            Workflow.objects.for_user(self.request.user)
            .filter(is_archived=False, is_active=True, is_tombstoned=False)
            .select_related("org", "project")
            .order_by("org__name", "name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search_query"] = self.request.GET.get("q", "")
        return context


class WorkflowSharingView(WorkflowObjectMixin, TemplateView):
    """
    View for managing workflow sharing settings (visibility and guest access).

    This is the "Sharing" tab in workflow settings. It allows:
    - Setting workflow visibility (private/public)
    - Viewing/managing guest access grants
    - Inviting guests to this workflow
    """

    template_name = "workflows/workflow_sharing.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_sharing():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()

        # Get active access grants for this workflow
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.models import WorkflowInvite

        access_grants = (
            WorkflowAccessGrant.objects.filter(workflow=workflow, is_active=True)
            .select_related("user", "granted_by")
            .order_by("-created")
        )

        pending_invites = (
            WorkflowInvite.objects.filter(
                workflow=workflow,
                status=WorkflowInvite.Status.PENDING,
            )
            .select_related("inviter", "invitee_user")
            .order_by("-created")
        )

        context.update(
            {
                "workflow": workflow,
                "access_grants": access_grants,
                "pending_invites": pending_invites,
                "can_manage_sharing": self.user_can_manage_sharing(),
            },
        )
        return context

    def get_breadcrumbs(self):
        workflow = self.get_workflow()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Sharing")})
        return breadcrumbs


class WorkflowVisibilityUpdateView(WorkflowObjectMixin, View):
    """Toggle workflow visibility between private and public."""

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        raw_state = (request.POST.get("is_public") or "").strip().lower()

        if raw_state in {"true", "1", "on"}:
            new_state = True
        elif raw_state in {"false", "0", "off"}:
            new_state = False
        else:
            # Toggle if no explicit value
            new_state = not workflow.is_public

        if workflow.is_public != new_state:
            workflow.is_public = new_state
            # Note: make_info_page_public auto-synced in model.save()
            workflow.save(update_fields=["is_public", "make_info_page_public"])

        # Return updated visibility section for HTMX
        context = {
            "workflow": workflow,
            "can_manage_sharing": self.user_can_manage_sharing(),
        }
        html = render_to_string(
            "workflows/partials/workflow_visibility_section.html",
            context,
            request=request,
        )
        return HttpResponse(html)


class WorkflowGuestInviteView(WorkflowObjectMixin, View):
    """
    Invite a guest to access this specific workflow.

    Creates a WorkflowInvite and optionally a notification if the invitee
    is an existing user.
    """

    def get(self, request, *args, **kwargs):
        """Return the invite form modal content."""
        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        context = {
            "workflow": workflow,
        }
        return render(
            request,
            "workflows/partials/workflow_guest_invite_form.html",
            context,
        )

    def post(self, request, *args, **kwargs):
        """Process the guest invite form."""
        from validibot.notifications.models import Notification
        from validibot.users.models import User
        from validibot.workflows.models import WorkflowInvite

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        email = (request.POST.get("email") or "").strip().lower()

        if not email:
            messages.error(request, _("Email address is required."))
            return self._render_form_response(request, workflow, email)

        # Check if user is already a member of the org
        existing_membership = workflow.org.memberships.filter(
            user__email__iexact=email,
            is_active=True,
        ).exists()
        if existing_membership:
            messages.error(
                request,
                _("This user is already a member of the organization."),
            )
            return self._render_form_response(request, workflow, email)

        # Check if user already has access
        existing_grant = workflow.access_grants.filter(
            user__email__iexact=email,
            is_active=True,
        ).exists()
        if existing_grant:
            messages.error(
                request,
                _("This user already has access to this workflow."),
            )
            return self._render_form_response(request, workflow, email)

        # Check for pending invite
        pending_invite = WorkflowInvite.objects.filter(
            workflow=workflow,
            invitee_email__iexact=email,
            status=WorkflowInvite.Status.PENDING,
        ).exists()
        if pending_invite:
            messages.error(
                request,
                _("An invitation is already pending for this email."),
            )
            return self._render_form_response(request, workflow, email)

        # Find existing user by email
        invitee_user = User.objects.filter(email__iexact=email).first()

        # Create the invite
        # Email is only sent if invitee is NOT already a registered user
        # (registered users receive in-app notifications instead)
        invite = WorkflowInvite.create_with_expiry(
            workflow=workflow,
            inviter=request.user,
            invitee_email=email,
            invitee_user=invitee_user,
            send_email=(invitee_user is None),
        )

        # Create notification if invitee is an existing user
        if invitee_user:
            Notification.objects.create(
                user=invitee_user,
                org=workflow.org,
                type=Notification.Type.WORKFLOW_INVITE,
                workflow_invite=invite,
                payload={
                    "workflow_name": workflow.name,
                    "inviter_name": request.user.name or request.user.email,
                },
            )

        messages.success(
            request,
            _("Invitation sent to %(email)s.") % {"email": email},
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_form_response(self, request, workflow, email=""):
        """Render the form with errors."""
        context = {
            "workflow": workflow,
            "email": email,
        }
        return render(
            request,
            "workflows/partials/workflow_guest_invite_form.html",
            context,
            status=HTTPStatus.OK,
        )

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section."""
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.models import WorkflowInvite

        access_grants = (
            WorkflowAccessGrant.objects.filter(workflow=workflow, is_active=True)
            .select_related("user", "granted_by")
            .order_by("-created")
        )
        pending_invites = (
            WorkflowInvite.objects.filter(
                workflow=workflow,
                status=WorkflowInvite.Status.PENDING,
            )
            .select_related("inviter", "invitee_user")
            .order_by("-created")
        )

        context = {
            "workflow": workflow,
            "access_grants": access_grants,
            "pending_invites": pending_invites,
            "can_manage_sharing": self.user_can_manage_sharing(),
        }
        response = render(
            request,
            "workflows/partials/workflow_guest_access_section.html",
            context,
        )
        # Retarget to the guest section (form targets modal content by default)
        response["HX-Retarget"] = "#guest-access-section"
        response["HX-Reswap"] = "outerHTML"
        response["HX-Trigger"] = "close-modal"
        return response


class WorkflowGuestRevokeView(WorkflowObjectMixin, View):
    """Revoke a guest's access to this workflow."""

    def post(self, request, *args, **kwargs):
        from validibot.notifications.models import Notification
        from validibot.workflows.models import WorkflowAccessGrant

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        grant_id = kwargs.get("grant_id")

        grant = get_object_or_404(
            WorkflowAccessGrant,
            pk=grant_id,
            workflow=workflow,
            is_active=True,
        )

        # Deactivate the grant
        grant.is_active = False
        grant.save(update_fields=["is_active", "modified"])

        # Notify the guest
        Notification.objects.create(
            user=grant.user,
            org=workflow.org,
            type=Notification.Type.SYSTEM_ALERT,
            payload={
                "action": "access_revoked",
                "workflow_name": workflow.name,
                "changed_by": request.user.id,
                "message": str(
                    _("Your access to '%(workflow)s' has been removed.")
                    % {"workflow": workflow.name}
                ),
            },
        )

        messages.success(
            request,
            _("Access revoked for %(email)s.") % {"email": grant.user.email},
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section."""
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.models import WorkflowInvite

        access_grants = (
            WorkflowAccessGrant.objects.filter(workflow=workflow, is_active=True)
            .select_related("user", "granted_by")
            .order_by("-created")
        )
        pending_invites = (
            WorkflowInvite.objects.filter(
                workflow=workflow,
                status=WorkflowInvite.Status.PENDING,
            )
            .select_related("inviter", "invitee_user")
            .order_by("-created")
        )

        context = {
            "workflow": workflow,
            "access_grants": access_grants,
            "pending_invites": pending_invites,
            "can_manage_sharing": self.user_can_manage_sharing(),
        }
        return render(
            request,
            "workflows/partials/workflow_guest_access_section.html",
            context,
        )


class WorkflowInviteCancelView(WorkflowObjectMixin, View):
    """Cancel a pending workflow invite."""

    def post(self, request, *args, **kwargs):
        from validibot.workflows.models import WorkflowInvite

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        invite_id = kwargs.get("invite_id")

        invite = get_object_or_404(
            WorkflowInvite,
            pk=invite_id,
            workflow=workflow,
            status=WorkflowInvite.Status.PENDING,
        )

        invite.cancel()

        messages.success(
            request,
            _("Invitation canceled."),
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section."""
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.models import WorkflowInvite

        access_grants = (
            WorkflowAccessGrant.objects.filter(workflow=workflow, is_active=True)
            .select_related("user", "granted_by")
            .order_by("-created")
        )
        pending_invites = (
            WorkflowInvite.objects.filter(
                workflow=workflow,
                status=WorkflowInvite.Status.PENDING,
            )
            .select_related("inviter", "invitee_user")
            .order_by("-created")
        )

        context = {
            "workflow": workflow,
            "access_grants": access_grants,
            "pending_invites": pending_invites,
            "can_manage_sharing": self.user_can_manage_sharing(),
        }
        return render(
            request,
            "workflows/partials/workflow_guest_access_section.html",
            context,
        )


class WorkflowInviteResendView(WorkflowObjectMixin, View):
    """Resend a workflow invite (creates a new invite with fresh expiry)."""

    def post(self, request, *args, **kwargs):
        from validibot.notifications.models import Notification
        from validibot.workflows.models import WorkflowInvite

        if not self.user_can_manage_sharing():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        workflow = self.get_workflow()
        invite_id = kwargs.get("invite_id")

        old_invite = get_object_or_404(
            WorkflowInvite,
            pk=invite_id,
            workflow=workflow,
        )

        # Cancel the old invite if still pending
        if old_invite.status == WorkflowInvite.Status.PENDING:
            old_invite.cancel()

        # Create a new invite
        # Email is only sent if invitee is NOT already a registered user
        # (registered users receive in-app notifications instead)
        new_invite = WorkflowInvite.create_with_expiry(
            workflow=workflow,
            inviter=request.user,
            invitee_email=old_invite.invitee_email,
            invitee_user=old_invite.invitee_user,
            send_email=(old_invite.invitee_user is None),
        )

        # Create notification if invitee is an existing user
        if new_invite.invitee_user:
            Notification.objects.create(
                user=new_invite.invitee_user,
                org=workflow.org,
                type=Notification.Type.WORKFLOW_INVITE,
                workflow_invite=new_invite,
                payload={
                    "workflow_name": workflow.name,
                    "inviter_name": request.user.name or request.user.email,
                },
            )

        messages.success(
            request,
            _("Invitation resent."),
        )

        # Return updated guest access section
        return self._render_guest_section_response(request, workflow)

    def _render_guest_section_response(self, request, workflow):
        """Render the updated guest access section."""
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.models import WorkflowInvite

        access_grants = (
            WorkflowAccessGrant.objects.filter(workflow=workflow, is_active=True)
            .select_related("user", "granted_by")
            .order_by("-created")
        )
        pending_invites = (
            WorkflowInvite.objects.filter(
                workflow=workflow,
                status=WorkflowInvite.Status.PENDING,
            )
            .select_related("inviter", "invitee_user")
            .order_by("-created")
        )

        context = {
            "workflow": workflow,
            "access_grants": access_grants,
            "pending_invites": pending_invites,
            "can_manage_sharing": self.user_can_manage_sharing(),
        }
        return render(
            request,
            "workflows/partials/workflow_guest_access_section.html",
            context,
        )
