from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin

from simplevalidations.notifications.models import Notification
from simplevalidations.users.models import PendingInvite


class NotificationListView(LoginRequiredMixin, TemplateView):
    """Display notifications for the active org and current user."""

    template_name = "notifications/notification_list.html"

    def get_queryset(self):
        org = getattr(self.request, "active_org", None) or getattr(
            self.request.user, "current_org", None
        )
        if not org:
            return Notification.objects.none()
        qs = Notification.objects.filter(user=self.request.user, org=org)
        # Lazy expire invites on read
        for notification in qs.select_related("invite"):
            invite = notification.invite
            if invite:
                invite.mark_expired_if_needed()
        return qs.order_by("-created_at")

    def get(self, request, *args, **kwargs):
        notifications = self.get_queryset()
        return render(
            request,
            self.template_name,
            {"notifications": notifications},
        )


def _notify_inviter(invite: PendingInvite, message: str):
    if not invite.inviter:
        return
    Notification.objects.create(
        user=invite.inviter,
        org=invite.org,
        type=Notification.Type.INVITE,
        invite=invite,
        payload={"message": message},
    )


class AcceptInviteView(View):
    """Allow an invitee to accept an invite notification."""

    def post(self, request, *args, **kwargs):
        notification = get_object_or_404(
            Notification.objects.select_related("invite"),
            pk=kwargs.get("pk"),
            user=request.user,
        )
        invite = notification.invite
        if invite is None:
            raise Http404
        invite.mark_expired_if_needed()
        if invite.status != PendingInvite.Status.PENDING:
            messages.error(request, _("Invite is no longer valid."))
            return HttpResponseRedirect(reverse("notifications:notification-list"))
        if invite.invitee_user and invite.invitee_user_id != request.user.id:
            messages.error(request, _("This invite was sent to a different user."))
            return HttpResponseRedirect(reverse("notifications:notification-list"))
        if invite.invitee_user is None:
            # Bind to current user if emails match
            if invite.invitee_email and invite.invitee_email.lower() == (request.user.email or "").lower():
                invite.invitee_user = request.user
                invite.save(update_fields=["invitee_user"])
            else:
                messages.error(request, _("This invite is not addressed to your account."))
                return HttpResponseRedirect(reverse("notifications:notification-list"))
        invite.accept()
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
        _notify_inviter(
            invite,
            _(f"{request.user.username} accepted your invite to {invite.org.name}"),
        )
        messages.success(request, _("Invitation accepted."))
        if request.headers.get("HX-Request"):
            notification.refresh_from_db()
            invite.refresh_from_db()
            return render(
                request,
                "notifications/partials/invite_row.html",
                {"notification": notification},
                status=200,
            )
        return HttpResponseRedirect(reverse("notifications:notification-list"))


class DeclineInviteView(View):
    """Allow an invitee to decline an invite notification."""

    def post(self, request, *args, **kwargs):
        notification = get_object_or_404(
            Notification.objects.select_related("invite"),
            pk=kwargs.get("pk"),
            user=request.user,
        )
        invite = notification.invite
        if invite is None:
            raise Http404
        if invite.invitee_user and invite.invitee_user_id != request.user.id:
            messages.error(request, _("This invite was sent to a different user."))
            return HttpResponseRedirect(reverse("notifications:notification-list"))
        invite.decline()
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])
        _notify_inviter(
            invite,
            _(f"{request.user.username} declined your invite to {invite.org.name}"),
        )
        messages.info(request, _("Invitation declined."))
        if request.headers.get("HX-Request"):
            notification.refresh_from_db()
            invite.refresh_from_db()
            return render(
                request,
                "notifications/partials/invite_row.html",
                {"notification": notification},
                status=200,
            )
        return HttpResponseRedirect(reverse("notifications:notification-list"))

# Create your views here.
