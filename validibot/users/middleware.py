"""Security middleware for privileged Django admin access.

The normal django-allauth admin wrapper sends anonymous administrators through
the allauth login flow, which challenges users who have already enrolled in
MFA. It does not make enrolment mandatory, and Django's admin permission check
does not know whether the current session actually completed an MFA step.

``AdminMFAMiddleware`` closes both gaps at the common admin dispatch boundary.
When enabled, every staff or superuser must have a primary factor and the
current session must contain an allauth MFA authentication record belonging to
an authenticator that still exists. This applies to every registered admin
view, including third-party and future model-admin views.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import urlencode

from allauth.account.authentication import get_authentication_records
from allauth.mfa.models import Authenticator
from allauth.mfa.utils import is_mfa_enabled
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.http import HttpRequest
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from collections.abc import Callable


class AdminMFAMiddleware(MiddlewareMixin):
    """Require MFA enrolment and session assurance for every admin view.

    The middleware deliberately ignores anonymous and non-staff users so
    Django's normal admin permission handling remains responsible for login and
    denial responses. Authenticated staff without a primary factor are sent to
    TOTP enrolment. Enrolled staff whose current session has not used a live MFA
    authenticator are sent through allauth's MFA reauthentication flow.

    Local and test environments can leave ``DJANGO_ADMIN_REQUIRE_MFA`` false.
    Production settings enable it by default, with an explicit environment
    override available only for documented break-glass recovery.
    """

    def process_view(
        self,
        request: HttpRequest,
        _view_func: Callable[..., HttpResponse],
        _view_args: tuple[Any, ...],
        _view_kwargs: dict[str, Any],
    ) -> HttpResponse | None:
        """Enforce the privileged-session policy before an admin view runs."""
        if not settings.DJANGO_ADMIN_REQUIRE_MFA:
            return None

        match = request.resolver_match
        if match is None or match.namespace != "admin":
            return None

        user = request.user
        if not user.is_authenticated or not user.is_active or not user.is_staff:
            return None

        primary_types = (
            Authenticator.Type.TOTP,
            Authenticator.Type.WEBAUTHN,
        )
        if not is_mfa_enabled(user, types=primary_types):
            messages.error(
                request,
                _(
                    "Django admin requires multi-factor authentication. "
                    "Set up an authenticator before returning to admin.",
                ),
            )
            return self._redirect_with_next(request, "mfa_activate_totp")

        if not self._session_has_current_mfa(request):
            messages.info(
                request,
                _(
                    "Confirm your multi-factor authentication before entering "
                    "Django admin.",
                ),
            )
            return self._redirect_with_next(request, "mfa_reauthenticate")

        return None

    @staticmethod
    def _session_has_current_mfa(request: HttpRequest) -> bool:
        """Return whether this session used an authenticator that still exists.

        Checking the authenticator ID as well as ``method == \"mfa\"`` prevents
        an old session record from remaining sufficient after its factor was
        deleted or replaced. Recovery-code authentication is accepted while a
        primary factor remains enrolled because allauth records the recovery
        code authenticator as the MFA method used for that session.
        """
        authenticator_ids = set(
            Authenticator.objects.filter(user_id=request.user.pk).values_list(
                "pk",
                flat=True,
            ),
        )
        return any(
            record.get("method") == "mfa" and record.get("id") in authenticator_ids
            for record in get_authentication_records(request)
        )

    @staticmethod
    def _redirect_with_next(
        request: HttpRequest,
        url_name: str,
    ) -> HttpResponseRedirect:
        """Redirect to an allauth MFA flow while preserving the admin target."""
        query = urlencode({REDIRECT_FIELD_NAME: request.get_full_path()})
        return HttpResponseRedirect(f"{reverse(url_name)}?{query}")
