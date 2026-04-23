"""Pro-gated audit log views.

The views surface the append-only ``AuditLogEntry`` table to members
of an organisation, gated by ``CommercialFeature.AUDIT_LOG``. That
feature flag is part of the Pro and Enterprise bundles.

``AUDIT_LOG`` is deliberately separate from ``ADVANCED_ANALYTICS``;
the ADR-2026-04-16 four-pillar taxonomy treats product analytics
and the audit log as distinct concerns with different models,
retention, and UI surfaces. Pro currently ships both.

Three access controls stack here, in decreasing order of abstraction:

1. **Feature gate** (``FeatureRequiredMixin``) — if the running
   deployment doesn't have AUDIT_LOG in its licence, every URL in
   this module 404s. Community-only deployments never see these
   pages.
2. **Auth** (``LoginRequiredMixin``) — anonymous traffic gets
   redirected to login.
3. **Org scope** (``OrgMixin``) — the queryset is filtered to
   ``request.active_org``. A user with no active org sees an empty
   list (``filter(org=None)`` → empty set), which is safer than
   "all entries with no org" if an operator ever forgets to set the
   predicate.

The detail view applies the same org check, so a member of org A
cannot probe for org B's entries by guessing ids.

**Session-scope.** 4a shipped list + detail. 4b adds the shared
``AuditLogFilterForm`` (action / actor / target / date range) and
``AuditLogExportView`` (CSV + JSONL streaming, rate-limited to
10/hr/org to make audit-log scraping an unattractive exfiltration
channel).
"""

from __future__ import annotations

import csv
import json
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import HttpResponse
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView
from django.views.generic import ListView

from validibot.audit.forms import AuditLogFilterForm
from validibot.audit.models import AuditLogEntry
from validibot.core.features import CommercialFeature
from validibot.core.mixins import BreadcrumbMixin
from validibot.core.mixins import FeatureRequiredMixin
from validibot.users.mixins import OrgMixin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)


# Rate-limit configuration for the export endpoint. The ADR fixes this
# at 10 requests per hour per org. Pulling the numbers into constants
# keeps the export view readable and the test suite pinnable on a
# single source of truth.
_EXPORT_RATE_LIMIT = 10
_EXPORT_RATE_WINDOW_SECONDS = 3600


class _AuditLogBaseMixin(
    LoginRequiredMixin,
    FeatureRequiredMixin,
    OrgMixin,
):
    """Shared gating + org-scope for every audit view.

    Any new audit endpoint should inherit this so the gate rules live
    in exactly one place.
    """

    required_commercial_feature = CommercialFeature.AUDIT_LOG

    def get_queryset(self):
        """Restrict to the active organisation's entries.

        ``OrgMixin`` populates ``self.org`` from ``request.active_org``.
        If no active org is resolved (user has no memberships yet, or
        the org cookie is stale), the queryset filters to an empty
        set via the impossible ``org=None`` predicate — safer than
        "all entries with no org", which would be a cross-org leak.
        """

        return (
            AuditLogEntry.objects.filter(org=self.org)
            .select_related("actor", "actor__user", "org")
            .order_by("-occurred_at")
        )


class AuditLogListView(_AuditLogBaseMixin, BreadcrumbMixin, ListView):
    """Paginated, filterable list of audit entries.

    The filter form is GET-bound so every filtered state is a
    linkable URL. Ordering matches the ``(org, -occurred_at)``
    composite index on :class:`AuditLogEntry`, so pagination doesn't
    pathologically scan the table as it grows.
    """

    template_name = "audit/audit_log_list.html"
    context_object_name = "entries"
    paginate_by = 50
    breadcrumbs = [
        {"name": "Audit log", "url": ""},
    ]

    def get_queryset(self):
        """Start from the org-scoped base queryset, then apply filters."""

        base = super().get_queryset()
        return self._filter_form().apply_to_queryset(base)

    def _filter_form(self) -> AuditLogFilterForm:
        """Build (and cache) the filter form from the query string.

        Cached per request so ``get_queryset`` and ``get_context_data``
        share the same form instance (including its validation
        errors), avoiding a double-parse.
        """

        if not hasattr(self, "_cached_filter_form"):
            self._cached_filter_form = AuditLogFilterForm(
                self.request.GET or None,
            )
        return self._cached_filter_form

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["organization"] = self.org
        context["filter_form"] = self._filter_form()
        # The export button carries the same query string so the
        # export reflects exactly what the user is currently looking
        # at.
        context["export_querystring"] = self.request.GET.urlencode()
        return context


class AuditLogDetailView(_AuditLogBaseMixin, BreadcrumbMixin, DetailView):
    """Detail view for a single audit entry.

    The queryset filter from the mixin is what enforces org
    isolation here: ``DetailView.get_object()`` calls
    ``get_queryset().get(pk=...)``, so an attempt to open an id from
    another org resolves to a 404 rather than a forbidden.
    """

    template_name = "audit/audit_log_detail.html"
    context_object_name = "entry"
    pk_url_kwarg = "entry_id"

    def get_breadcrumbs(self) -> list[dict[str, str]]:
        """Breadcrumb trail: Audit log › #<id>."""

        from django.urls import reverse

        return [
            {"name": "Audit log", "url": reverse("audit:list")},
            {"name": f"#{self.kwargs[self.pk_url_kwarg]}", "url": ""},
        ]


class AuditLogExportView(_AuditLogBaseMixin, View):
    """Stream the filtered audit log as CSV or JSONL.

    Shares :class:`AuditLogFilterForm` with the list view so "Export
    current view" produces exactly what the operator is looking at.

    **Streaming.** The view returns a ``StreamingHttpResponse`` so the
    DB cursor drives the response rather than materialising the whole
    queryset in memory. Even a year of audit entries for a busy Pro
    customer stays comfortably below the Cloud Run instance memory
    budget this way.

    **Rate limiting.** ADR fixes the limit at 10 requests per hour
    per org. Keyed by org id (not user) so two admins on the same
    team share the budget — otherwise a disgruntled insider could
    rotate through team members to exfiltrate at will. Legitimate
    operators under the limit see no effect.
    """

    SUPPORTED_FORMATS: tuple[str, ...] = ("csv", "jsonl")

    def get(self, request: HttpRequest) -> HttpResponse:
        """Route to CSV or JSONL streaming, or 400 for unknown formats."""

        export_format = request.GET.get("format", "csv").lower()
        if export_format not in self.SUPPORTED_FORMATS:
            return HttpResponse(
                f"Unsupported export format: {export_format!r}. "
                f"Supported: {', '.join(self.SUPPORTED_FORMATS)}.",
                status=HTTPStatus.BAD_REQUEST,
                content_type="text/plain",
            )

        form = AuditLogFilterForm(request.GET or None)
        base_queryset = super().get_queryset()
        queryset = form.apply_to_queryset(base_queryset)

        rate_limited = self._check_rate_limit(request)
        if rate_limited is not None:
            return rate_limited

        timestamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        filename = f"validibot-audit-{timestamp}.{export_format}"
        if export_format == "csv":
            response = StreamingHttpResponse(
                self._stream_csv(queryset),
                content_type="text/csv",
            )
        else:  # jsonl
            response = StreamingHttpResponse(
                self._stream_jsonl(queryset),
                content_type="application/x-ndjson",
            )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    # ── Rate limiting ──────────────────────────────────────────────

    def _check_rate_limit(self, request: HttpRequest) -> HttpResponse | None:
        """Return a 429 response if the org exceeded the export limit.

        Same cache-atomic ``set + incr`` pattern as
        ``validibot.core.ratelimit`` — reimplemented here because that
        decorator is IP-keyed, while the policy on this endpoint is
        per-org (10 requests per hour per organisation).

        When the org has no active membership we skip the check;
        ``get_queryset`` already returns an empty result for those
        callers, so there's nothing to exfiltrate.
        """

        if self.org is None:
            return None

        cache_key = f"audit:export:org:{self.org.pk}"
        current = cache.get(cache_key)
        if current is None:
            cache.set(cache_key, 1, _EXPORT_RATE_WINDOW_SECONDS)
            return None

        try:
            current = cache.incr(cache_key)
        except ValueError:
            # Key expired between get and incr — reset.
            cache.set(cache_key, 1, _EXPORT_RATE_WINDOW_SECONDS)
            return None

        if current > _EXPORT_RATE_LIMIT:
            logger.warning(
                "Audit export rate limit exceeded: org=%s (%d/%d in %ds window)",
                self.org.pk,
                current,
                _EXPORT_RATE_LIMIT,
                _EXPORT_RATE_WINDOW_SECONDS,
            )
            response = HttpResponse(
                "Too many audit-log exports in the last hour. "
                "Please wait before retrying.",
                status=HTTPStatus.TOO_MANY_REQUESTS,
                content_type="text/plain",
            )
            response["Retry-After"] = str(_EXPORT_RATE_WINDOW_SECONDS)
            return response
        return None

    # ── Serialisation ──────────────────────────────────────────────

    # Column order for CSV — forensically important fields on the
    # left, fuller payload columns on the right.
    _CSV_FIELDS: tuple[str, ...] = (
        "occurred_at",
        "action",
        "actor_email",
        "actor_user_id",
        "actor_ip_address",
        "target_type",
        "target_id",
        "target_repr",
        "request_id",
        "changes",
        "metadata",
    )

    @classmethod
    def _row_dict(cls, entry: AuditLogEntry) -> dict[str, Any]:
        """Flatten an entry into the dict used by both CSV and JSONL.

        Takes the two-layer (actor, entry) model and projects
        common-use fields onto one flat dict. Keeps the two output
        formats symmetric — no silent shape drift between them.

        Erased actors render as ``(erased)`` so an export doesn't
        leak a residual ``user_id`` without a flag. The foreign-key
        id alone is pseudonymous but still correlatable against the
        users table; labelling it explicitly keeps the intent clear.
        """

        actor = entry.actor
        if actor.erased_at is not None:
            actor_email = "(erased)"
            actor_ip_address = "(erased)"
            actor_user_id: Any = "(erased)"
        else:
            actor_email = (
                actor.email
                or (getattr(actor.user, "email", None) if actor.user_id else None)
                or ""
            )
            actor_ip_address = actor.ip_address or ""
            actor_user_id = actor.user_id

        return {
            "occurred_at": entry.occurred_at.isoformat() if entry.occurred_at else "",
            "action": entry.action,
            "actor_email": actor_email,
            "actor_user_id": actor_user_id if actor_user_id is not None else "",
            "actor_ip_address": actor_ip_address,
            "target_type": entry.target_type,
            "target_id": entry.target_id,
            "target_repr": entry.target_repr,
            "request_id": entry.request_id,
            "changes": entry.changes or {},
            "metadata": entry.metadata or {},
        }

    def _stream_csv(
        self,
        queryset: Iterable[AuditLogEntry],
    ) -> Iterable[str]:
        """Generator yielding one CSV line at a time.

        The two dict-shaped columns (``changes``, ``metadata``) are
        serialised with ``json.dumps`` so a downstream CSV parser can
        round-trip the nested structure. Plain ``str(dict)`` would
        produce ``{'key': 'value'}``, which isn't valid JSON.
        """

        buffer = _StringEcho()
        writer = csv.DictWriter(buffer, fieldnames=self._CSV_FIELDS)
        yield buffer.write_header(writer)

        for entry in queryset.iterator(chunk_size=500):
            row = self._row_dict(entry)
            row["changes"] = json.dumps(row["changes"], default=str)
            row["metadata"] = json.dumps(row["metadata"], default=str)
            writer.writerow(row)
            yield buffer.drain()

    def _stream_jsonl(
        self,
        queryset: Iterable[AuditLogEntry],
    ) -> Iterable[str]:
        """Generator yielding one JSON object per line."""

        for entry in queryset.iterator(chunk_size=500):
            yield json.dumps(self._row_dict(entry), default=str) + "\n"


class _StringEcho:
    """csv.writer-compatible buffer that yields its last write.

    ``csv.writer.writerow()`` writes to the provided buffer but
    returns ``None`` — to stream we need each row back as a string.
    This helper captures the writer's output and hands it to the
    generator.

    A ``StringIO`` would work too, but it tracks position and
    supports seeks we don't use. A one-slot buffer is enough.
    """

    def __init__(self) -> None:
        self._last: str = ""

    def write(self, value: str) -> int:
        """``csv.writer`` calls ``write(line)`` — stash the line for drain."""

        self._last = value
        return len(value)

    def write_header(self, writer: csv.DictWriter) -> str:
        """Emit the header row and return it."""

        writer.writeheader()
        return self.drain()

    def drain(self) -> str:
        """Return the captured line and reset the buffer."""

        out = self._last
        self._last = ""
        return out
