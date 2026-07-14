"""Bearer API-key authentication for the Validibot API."""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from rest_framework.authentication import BaseAuthentication
from rest_framework.authentication import get_authorization_header
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed

from validibot.users.services.api_keys import verify_api_key

AUTH_HEADER_PARTS = 2


class BearerAuthentication(BaseAuthentication):
    """Authenticate ``Authorization: Bearer ...`` API credentials.

    New Validibot API keys use hashed ``vbk_...`` storage. Legacy DRF
    authtokens remain accepted for compatibility with existing clients and
    setup scripts until those callers have rotated onto the hashed format.
    """

    keyword = b"Bearer"

    def authenticate(self, request):
        """Resolve a bearer credential to ``(user, auth)`` or fail closed."""

        auth = get_authorization_header(request).split()
        if not auth:
            return None

        if auth[0].lower() != self.keyword.lower():
            return None

        if len(auth) != AUTH_HEADER_PARTS:
            raise AuthenticationFailed(_("Invalid bearer header."))

        try:
            raw_key = auth[1].decode()
        except UnicodeError as exc:
            raise AuthenticationFailed(_("Invalid bearer header.")) from exc

        if raw_key.startswith("vbk_"):
            api_key = verify_api_key(raw_key)
            if api_key is None:
                raise AuthenticationFailed(_("Invalid API key."))
            return (api_key.user, api_key)

        token = Token.objects.select_related("user").filter(key=raw_key).first()
        if token is None or not token.user.is_active:
            raise AuthenticationFailed(_("Invalid API key."))
        return (token.user, token)

    def authenticate_header(self, request) -> str:
        """Advertise the Bearer challenge for unauthenticated API callers."""

        return "Bearer"
