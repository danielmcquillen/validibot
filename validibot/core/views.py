from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import mail_admins
from django.http import HttpRequest
from django.http import HttpResponse
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_http_methods

from validibot.core.forms import SupportMessageForm
from validibot.core.jwks import jwk_from_kms_key


@login_required
@require_http_methods(["POST"])
def submit_support_message(request: HttpRequest) -> HttpResponse:
    """Submit a support message. Marketing contact page moved to separate site."""
    form = SupportMessageForm(request.POST)
    if form.is_valid():
        support_message = form.save(commit=False)
        support_message.user = request.user
        support_message.save()
        _notify_admins(request, form)

        messages.success(
            request,
            _("Thanks for reaching out. A member of the team will respond soon."),
        )
        return redirect(reverse("home:home"))

    messages.error(
        request,
        _(
            "We couldn't send your message. Please correct "
            "the highlighted fields and try again.",
        ),
    )
    return redirect(reverse("home:home"))


@login_required
def app_home_redirect(request: HttpRequest) -> HttpResponse:
    """Redirect `/app/` requests to the user's current organization dashboard."""

    org = request.user.get_current_org()
    if not org or not settings.ENABLE_APP:
        messages.error(request, _("You do not belong to any organizations yet."))
        return redirect("home:home")

    return redirect(reverse("dashboard:my_dashboard"))


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


# Allow multiple keys (old + new) for rotation
def _key_ids():
    # Add old key IDs during rotation so verifiers can see both keys.
    keys = getattr(settings, "GCP_KMS_JWKS_KEYS", [])
    return [key for key in keys if key]


@cache_page(60 * 15)  # cache 15 minutes
def jwks_view(request):
    alg = getattr(settings, "SV_JWKS_ALG", "ES256")
    keys = [jwk_from_kms_key(k, alg) for k in _key_ids()]
    # Best-practice content-type for JWKS:
    resp = JsonResponse({"keys": keys})
    resp["Content-Type"] = "application/jwk-set+json"
    # Cache hints for verifiers / CDNs
    resp["Cache-Control"] = "public, max-age=900"
    return resp
