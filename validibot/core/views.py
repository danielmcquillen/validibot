import logging
import mimetypes

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.mail import mail_admins
from django.core.signing import BadSignature
from django.core.signing import SignatureExpired
from django.http import FileResponse
from django.http import HttpRequest
from django.http import HttpResponse
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_http_methods

from validibot.core.filesafety import sanitize_filename
from validibot.core.forms import SupportMessageForm

logger = logging.getLogger(__name__)


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


@login_required
@require_GET
def data_download(request: HttpRequest) -> HttpResponse:
    """
    Serve a signed local-storage file download.

    Query parameters:
        token: Signed payload from ``LocalDataStorage.sign_download()``.
            Contains both the path and max_age, tamper-proof.
        filename: Optional download filename for Content-Disposition.

    This is the local-storage equivalent of GCS signed URLs. The view
    validates the token using Django's ``TimestampSigner`` and streams
    the file via ``FileResponse``.
    """
    from validibot.core.storage.local import LocalDataStorage

    token = request.GET.get("token", "")
    if not token:
        return HttpResponseForbidden("Missing token.")

    try:
        path = LocalDataStorage.unsign_download(token)
    except SignatureExpired:
        logger.debug("Download token expired")
        return HttpResponseForbidden("Download link has expired.")
    except BadSignature:
        logger.warning("Invalid download token received")
        return HttpResponseForbidden("Invalid download link.")

    storage = LocalDataStorage()

    try:
        full_path = storage.get_absolute_path(path)
    except ValueError:
        logger.warning("Path traversal attempt in download: %s", path)
        return HttpResponseForbidden("Invalid path.")

    if not full_path.exists():
        logger.warning("Download path does not exist: %s", path)
        return HttpResponseForbidden("File not found.")

    raw_filename = request.GET.get("filename") or full_path.name
    filename = sanitize_filename(raw_filename, fallback=full_path.name)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return FileResponse(
        full_path.open("rb"),
        content_type=content_type,
        as_attachment=True,
        filename=filename,
    )


@login_required
@require_GET
def help_drawer_content(request: HttpRequest, slug: str) -> HttpResponse:
    """Serve help drawer content for the offcanvas panel.

    Renders ``help/drawers/{slug}.html`` as an HTML fragment (no base
    template).  The slug is restricted to alphanumeric + hyphens/underscores
    by the URL pattern (``<slug:slug>``), preventing path traversal.

    Returns a 404 if the template does not exist.
    """
    from django.template import TemplateDoesNotExist
    from django.template.loader import get_template

    template_name = f"help/drawers/{slug}.html"
    try:
        tpl = get_template(template_name)
    except TemplateDoesNotExist as exc:
        from django.http import Http404

        raise Http404(f"Help topic '{slug}' not found.") from exc

    return HttpResponse(tpl.render({}, request))
