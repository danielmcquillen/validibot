
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import NotAcceptable
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.renderers import JSONRenderer

AGENT_MEDIA = "application/vnd.simplevalidations.agent+json"
A2A_MEDIA = "application/vnd.simplevalidations.a2a+json"
VALID_PROFILES = {"human", "agent", "a2a"}


def _accept_includes(accept_header: str, target: str) -> bool:
    # Case-insensitive exact match per media type token (ignores params/q)
    accept = (accept_header or "").lower()
    target = target.lower()
    return any(part.split(";")[0].strip() == target for part in accept.split(","))


def _accepts_jsonish(accept_header: str) -> bool:
    a = (accept_header or "").lower()
    tokens = [p.split(";")[0].strip() for p in a.split(",") if p.strip()]
    return any(t in {"application/json", "application/*", "*/*"} for t in tokens)


class AgentAwareNegotiation(DefaultContentNegotiation):
    """
    Sets request.agent_profile ∈ {"human","agent","a2a"} based on:
      1) Accept vendor media type (preferred)
      2) X-Agent-Profile header (fallback)
    Maps vendor media types to JSONRenderer without mutating headers.
    """

    def select_renderer(
        self, request, renderers, format_suffix=None
    ) -> tuple[object, str]:
        accept = request.META.get("HTTP_ACCEPT", "")

        if _accept_includes(accept, A2A_MEDIA):
            profile = "a2a"
            wants_vendor_json = True
        elif _accept_includes(accept, AGENT_MEDIA):
            profile = "agent"
            wants_vendor_json = True
        else:
            hdr = (request.META.get("HTTP_X_AGENT_PROFILE", "") or "").lower()
            profile = hdr if hdr in VALID_PROFILES else "human"
            wants_vendor_json = False

        request.agent_profile = profile

        # If the client asked for our vendor JSON, prefer JSONRenderer.
        if wants_vendor_json:
            json_renderer = next(
                (r for r in renderers if isinstance(r, JSONRenderer)), None
            )
            if json_renderer:
                # Tell DRF we’re rendering JSON even though Accept was vendor+json
                return json_renderer, "application/json"

            # If no JSON renderer is available, fall back to DRF default selection
            # (or raise 406 to be strict)
            if not renderers:
                err_msg = _("No renderers configured.")
                raise NotAcceptable(err_msg)
            return super().select_renderer(request, renderers, format_suffix)

        # Otherwise let DRF handle normal negotiation (supports */* etc.).
        # But if the client only accepts JSON-ish and we have JSONRenderer, bias to it.
        if _accepts_jsonish(accept):
            json_renderer = next(
                (r for r in renderers if isinstance(r, JSONRenderer)), None
            )
            if json_renderer:
                return json_renderer, "application/json"

        return super().select_renderer(request, renderers, format_suffix)
