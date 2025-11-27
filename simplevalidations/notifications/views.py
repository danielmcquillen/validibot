from __future__ import annotations

from django.contrib import messages
from django.db import models
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView, ListView
from django.contrib.auth.mixins import LoginRequiredMixin

from simplevalidations.notifications.models import Notification
from simplevalidations.tracking.constants import TrackingEventType
from simplevalidations.tracking.services import TrackingEventService
from simplevalidations.events.constants import AppEventType
from simplevalidations.users.models import PendingInvite, User


class NotificationListView(LoginRequiredMixin, ListView):
    """Display notifications for the current user (all orgs) with paging."""

    model = Notification
    template_name = "notifications/notification_list.html"
    context_object_name = "notifications"
    paginate_by = 20
    page_size_options = (10, 20, 50)

    def get_paginate_by(self, queryset):
        per_page = self.request.GET.get("per_page")
        page_size = self.paginate_by
        if per_page:
            try:
                per_page_value = int(per_page)
            except (TypeError, ValueError):
                per_page_value = self.paginate_by
            else:
                if per_page_value in self.page_size_options:
                    page_size = per_page_value
        self.page_size = page_size
        return page_size

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user)
        show_dismissed = self.request.GET.get("show_dismissed") == "on"
        if not show_dismissed:
            qs = qs.filter(dismissed_at__isnull=True)
        qs = qs.select_related("invite").order_by("-created_at")
        # Lazy expire invites on read
        for notification in qs:
            invite = notification.invite
            if invite:
                invite.mark_expired_if_needed()
        return qs

    def _query_string(self) -> str:
        params = self.request.GET.copy()
        params.pop("page", None)
        return params.urlencode()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "show_dismissed": self.request.GET.get("show_dismissed") == "on",
                "query_string": self._query_string(),
                "page_size_options": self.page_size_options,
                "current_page_size": getattr(self, "page_size", self.paginate_by),
            },
        )
        return context


def _invitee_label(invite: PendingInvite) -> str:
    if invite.invitee_user:
        return invite.invitee_user.username
    return invite.invitee_email or _("unknown user")


def _notify_inviter(invite: PendingInvite, *, action: str):
    if not invite.inviter:
        return
    invitee_name = _invitee_label(invite)
    org_name = invite.org.name
    message = _("Invitation to '%(username)s' to join %(org)s was %(action)s.") % {
        "username": invitee_name,
        "org": org_name,
        "action": action,
    }
    Notification.objects.create(
        user=invite.inviter,
        org=invite.org,
        type=Notification.Type.INVITE,
        invite=invite,
        payload={"message": str(message)},
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
        _notify_inviter(invite, action=_("accepted"))
        TrackingEventService().log_tracking_event(
            event_type=TrackingEventType.APP_EVENT,
            app_event_type=AppEventType.INVITE_ACCEPTED,
            project=None,
            org=invite.org,
            user=request.user,
            extra_data={
                "invite_id": str(invite.id),
                "inviter_id": getattr(invite.inviter, "id", None),
                "invitee_user_id": getattr(invite.invitee_user, "id", None),
                "invitee_email": invite.invitee_email,
                "roles": invite.roles,
            },
            channel="web",
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
        _notify_inviter(invite, action=_("declined"))
        TrackingEventService().log_tracking_event(
            event_type=TrackingEventType.APP_EVENT,
            app_event_type=AppEventType.INVITE_DECLINED,
            project=None,
            org=invite.org,
            user=request.user,
            extra_data={
                "invite_id": str(invite.id),
                "inviter_id": getattr(invite.inviter, "id", None),
                "invitee_user_id": getattr(invite.invitee_user, "id", None),
                "invitee_email": invite.invitee_email,
                "roles": invite.roles,
            },
            channel="web",
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


class DismissNotificationView(LoginRequiredMixin, View):
    """Dismiss a notification."""

    def post(self, request, *args, **kwargs):
        notification = get_object_or_404(
            Notification, pk=kwargs.get("pk"), user=request.user
        )
        notification.dismissed_at = timezone.now()
        notification.save(update_fields=["dismissed_at"])
        if request.headers.get("HX-Request"):
            show_dismissed = request.POST.get("show_dismissed") == "on"
            if show_dismissed:
                return render(
                    request,
                    "notifications/partials/invite_row.html",
                    {
                        "notification": notification,
                        "show_dismissed": True,
                    },
                    status=200,
                )
            # For the non-dismissed view, returning empty HTML ensures the
            # hx-swap="outerHTML" removes the row from the DOM.
            return HttpResponse("", status=200)
        return HttpResponseRedirect(reverse("notifications:notification-list"))
