from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import mail_admins
from django.http import HttpRequest
from django.http import HttpResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from simplevalidations.core.forms import SupportMessageForm
from simplevalidations.core.utils import is_htmx


@login_required
@require_http_methods(["POST"])
def submit_support_message(request: HttpRequest) -> HttpResponse:
    form = SupportMessageForm(request.POST)
    if form.is_valid():
        support_message = form.save(commit=False)
        support_message.user = request.user
        support_message.save()
        _notify_admins(request, form)

        success_context = {
            "headline": _("Message received"),
            "body": _(
                "Thanks for reaching out. A member of the team will respond soon.",
            ),
        }
        if is_htmx(request):
            return render(
                request,
                "marketing/partial/support_message_success.html",
                success_context,
                status=201,
            )

        messages.success(request, success_context["body"])
        return redirect(reverse("marketing:contact"))

    if is_htmx(request):
        return render(
            request,
            "marketing/partial/support_message_form.html",
            {"form": form},
            status=400,
        )

    messages.error(
        request,
        _(
            "We couldn't send your message. Please correct "
            "the highlighted fields and try again.",
        ),
    )
    return redirect(reverse("marketing:contact"))


@login_required
def app_home_redirect(request: HttpRequest) -> HttpResponse:
    """Redirect `/app/` requests to the user's current organization dashboard."""

    org = request.user.get_current_org()
    if not org or not settings.APP_ENABLED:
        messages.error(request, _("You do not belong to any organizations yet."))
        return redirect("marketing:home")

    return redirect(reverse("dashboard:my_dashboard", kwargs={"org_id": org.pk}))


def _notify_admins(request: HttpRequest, form: SupportMessageForm) -> None:
    user = request.user
    display_name = user.get_full_name() or user.email or str(user)
    subject = _("New support message from %(name)s") % {"name": display_name}
    body = _(
        "Subject: %(subject)s\n"
        "Message:\n%(message)s\n\n"
        "Submitted by: %(user)s (id=%(user_id)s)",
    ) % {
        "subject": form.cleaned_data["subject"],
        "message": form.cleaned_data["message"],
        "user": display_name,
        "user_id": user.pk,
    }
    mail_admins(subject=subject, message=body, fail_silently=True)
