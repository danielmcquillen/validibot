# ADR-2025-11-28: Public Workflow Access for Anonymous Users

**Status:** Proposed (2025-11-28)  
**Owners:** Platform / Workflows / Security  
**Related ADRs:** 2025-11-28-pricing-system, 2025-11-28-invite-based-signup-and-seat-management, 2025-11-27-idempotency-keys  
**Depends on:** ADR-2025-11-28-pricing-system (billing infrastructure must be in place first)  
**Related docs:** `dev_docs/overview/how_it_works.md`, `workflows/views.py`, `workflows/permissions.py`

---

## Context

### The Opportunity

Validibot currently requires users to be authenticated to launch workflow validations. However, many valuable use cases require public access:

- **Embedded validation widgets** – A building energy consultant wants to embed a "Validate your IDF file" form on their website for prospective clients.
- **Public APIs for CI/CD** – An open-source project wants to let anyone validate configuration files via API without creating an account.
- **Lead generation** – A workflow author wants to offer free validations as a marketing tool, with results driving sign-ups.
- **Community tools** – Organizations want to provide validation services to their community without managing user accounts.

This mirrors patterns from successful SaaS platforms:

| Platform             | Pattern                                                                       |
| -------------------- | ----------------------------------------------------------------------------- |
| **Typeform**         | Authenticated users create forms; anyone can submit responses via public link |
| **Formspree**        | Form endpoints are public; submissions count against the creator's quota      |
| **Supabase**         | "Anon" public API keys allow limited operations; usage tied to project owner  |
| **Zapier/Pipedream** | Webhook URLs are public; executions count against the workspace               |

### Current State

Our existing architecture has partial support for public visibility:

1. **`Workflow.make_info_public`** – Boolean flag that allows unauthenticated users to view workflow details via `PublicWorkflowInfoView`.
2. **`PublicWorkflowListView`** – Lists workflows where `make_info_public=True`.
3. **Launch requires authentication** – Both `WorkflowLaunchDetailView` (web) and `WorkflowViewSet.start_validation` (API) require `IsAuthenticated`.

What's missing:

1. **Per-workflow public launch setting** – Separate from `make_info_public`.
2. **Public access controls** – Rate limiting, CAPTCHA, domain restrictions.
3. **Anonymous user attribution** – Tying public runs to the workflow owner's quota.
4. **Security hardening** – Protection against abuse of public endpoints.
5. **Monitoring and alerting** – Visibility into public usage patterns.

### Security Imperatives

Opening workflows to the public introduces significant risks:

| Risk                   | Impact                                                       | Likelihood |
| ---------------------- | ------------------------------------------------------------ | ---------- |
| **Denial of Service**  | Compute exhaustion, quota depletion                          | High       |
| **Spam/bot abuse**     | Garbage submissions, quota waste                             | High       |
| **Cost amplification** | Expensive validators (EnergyPlus, AI) run at attacker's will | Medium     |
| **Data exfiltration**  | Probing for information via error messages                   | Medium     |
| **Reputation damage**  | Platform used for malicious content                          | Low        |

This ADR prioritizes security at every decision point.

### Input Type Restrictions

**Key Decision: Public access is limited to structured text input (JSON/XML). File uploads require authentication.**

This distinction is critical for security and cost control:

| Input Type                   | Public Allowed? | Requires Auth? | Rationale                                         |
| ---------------------------- | --------------- | -------------- | ------------------------------------------------- |
| **JSON/XML text validation** | ✅ Yes          | ⛔ No          | Fast to parse, easy to rate-limit, low risk       |
| **File uploads (any type)**  | ⛔ No           | ✅ Yes         | Storage costs, parsing overhead, abuse risk       |
| **EnergyPlus IDF files**     | ⛔ No           | ✅ Yes         | Expensive Modal compute, long-running simulations |
| **PDF/DOCX documents**       | ⛔ No           | ✅ Yes         | Parsing complexity, storage, potential malware    |
| **ZIP/archive files**        | ⛔ No           | ✅ Yes         | Unpredictable size, extraction attacks            |
| **Simulation triggers**      | ⛔ No           | ✅ Yes         | Compute-intensive, tied to file inputs            |
| **Validation history**       | ⛔ No           | ✅ Yes         | Account-bound data, privacy concerns              |

**Why this split makes sense:**

1. **Structured text is safe and cheap** – JSON/XML inputs can be quickly validated, rejected, or processed. They're ideal for anonymous trial-style access ("Try this validator") and developer embedding.

2. **File uploads are expensive and risky** – They consume storage, bandwidth, and processing time. Exposing them publicly invites abuse: spam uploads, malware, quota exhaustion attacks.

3. **Compute-heavy validations need accountability** – Modal-based simulations (EnergyPlus, AI analysis) cost real money. Only authenticated users with quota attribution should trigger these.

4. **Better UX and auditability** – File status ("parsing failed," "validation complete") is only useful if you have a user to notify. Anonymous file uploads create orphaned state.

**Implementation:** Workflows using file-based input schemas will have `public_launch_mode` automatically restricted to `DISABLED`, enforced at the model validation level.

### Input Sanitization for JSON/XML

While JSON and XML are safer than file uploads, they still require careful handling to prevent attacks.

#### JSON: Low Risk with Basic Precautions

JSON is pure data with no executable code. Python's `json.loads()` and DRF's built-in parsing are safe and don't execute arbitrary code. However, apply these guardrails:

| Threat                 | Mitigation                                                              |
| ---------------------- | ----------------------------------------------------------------------- |
| **Oversized payloads** | Limit request size via `DATA_UPLOAD_MAX_MEMORY_SIZE` and nginx/gunicorn |
| **Deep nesting (DoS)** | Reject structures nested beyond a threshold (e.g., 20 levels)           |
| **Schema mismatch**    | Validate against JSON Schema or DRF serializer before processing        |
| **Key spoofing**       | Disallow `__proto__`, `constructor` keys (relevant if data reaches JS)  |

```python
# validibot/core/validators.py

MAX_JSON_DEPTH = 20
MAX_JSON_SIZE_BYTES = 1_048_576  # 1 MB


def validate_json_depth(data: Any, current_depth: int = 0) -> None:
    """
    Reject JSON structures nested too deeply (DoS prevention).

    Raises:
        ValidationError: If nesting exceeds MAX_JSON_DEPTH.
    """
    if current_depth > MAX_JSON_DEPTH:
        raise ValidationError(
            _("JSON structure is too deeply nested (max %(max)d levels).") % {
                "max": MAX_JSON_DEPTH,
            },
        )

    if isinstance(data, dict):
        for value in data.values():
            validate_json_depth(value, current_depth + 1)
    elif isinstance(data, list):
        for item in data:
            validate_json_depth(item, current_depth + 1)
```

#### XML: Requires Hardened Parser

XML has historically been more dangerous due to entity expansion attacks (XXE, "Billion Laughs"). **Never use Python's built-in `xml.etree.ElementTree` for untrusted input.**

| Threat                          | Mitigation                         |
| ------------------------------- | ---------------------------------- |
| **XML Bomb / Entity Expansion** | Use `defusedxml` library           |
| **External Entity (XXE)**       | Disable external entity resolution |
| **DTD recursion**               | Disable DTD processing entirely    |
| **Oversized payloads**          | Same size limits as JSON           |

```python
# validibot/core/validators.py

from defusedxml.ElementTree import fromstring as safe_xml_parse
from defusedxml import DefusedXmlException

MAX_XML_SIZE_BYTES = 1_048_576  # 1 MB


def parse_xml_safely(xml_string: str) -> Element:
    """
    Parse XML with protection against entity expansion and XXE attacks.

    Uses defusedxml which automatically prevents:
    - Billion Laughs (entity expansion)
    - External entity injection (XXE)
    - DTD retrieval

    Args:
        xml_string: Raw XML content from user input.

    Returns:
        Parsed ElementTree Element.

    Raises:
        ValidationError: If XML is malformed or contains attack patterns.
    """
    if len(xml_string.encode("utf-8")) > MAX_XML_SIZE_BYTES:
        raise ValidationError(
            _("XML payload exceeds maximum size (%(max)s bytes).") % {
                "max": MAX_XML_SIZE_BYTES,
            },
        )

    try:
        return safe_xml_parse(xml_string)
    except DefusedXmlException as e:
        # Caught an attack pattern (entity expansion, XXE, etc.)
        logger.warning("Blocked malicious XML: %s", e)
        raise ValidationError(
            _("XML contains disallowed content."),
        )
    except Exception as e:
        raise ValidationError(
            _("Invalid XML: %(error)s") % {"error": str(e)},
        )
```

**Dependency:** Add `defusedxml` to project requirements:

```toml
# pyproject.toml
dependencies = [
    # ... existing deps ...
    "defusedxml>=0.7.1",
]
```

#### Size Limits in Django Settings

```python
# config/settings/base.py

# Maximum size of request body (applies to all requests)
DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440  # 2.5 MB

# For public endpoints, enforce stricter limits in the view
PUBLIC_PAYLOAD_MAX_SIZE = 1_048_576  # 1 MB
```

---

## Decision

We will implement a comprehensive workflow access control system with multiple trust tiers, defense-in-depth security, and clear separation between authentication (who you are) and authorization (what you can access).

### 1. Access Tier Model

Workflow authors can configure who may launch their workflow. Access is organized into progressive trust tiers:

#### 1.1 Access Tier Summary

| Access Type          | Authentication         | Input Types      | Size Limits | Use Case                        |
| -------------------- | ---------------------- | ---------------- | ----------- | ------------------------------- |
| **PUBLIC**           | None (anonymous)       | JSON/XML only    | 1 MB        | Embedded forms, public demos    |
| **SV_USERS**         | User's API key / login | JSON/XML + files | 10 MB       | Platform-wide shared validators |
| **SV_USERS_SUBSET**  | User's API key / login | JSON/XML + files | 10 MB       | Specific users chosen by author |
| **ORG_USERS**        | User's API key / login | JSON/XML + files | 10 MB       | Internal team workflows         |
| **ORG_USERS_SUBSET** | User's API key / login | JSON/XML + files | 10 MB       | Specific org members/roles      |

**Key principles:**

1. **Authentication vs Authorization** — Users authenticate with their _own_ credentials (API key or session). Authorization checks whether the authenticated user is permitted by the _workflow's access settings_.

2. **Quota always to author** — All launches, regardless of access tier, are charged to the **workflow owner's organization**. "My workflows use my quota."

#### 1.2 URL Structure

**API Routes:**

All API routes are scoped by organization slug to ensure clear ownership and quota attribution:

```
/api/v1/<org_slug>/internal/   # Authenticated users (requires API key or session)
/api/v1/<org_slug>/public/     # Anonymous users (Phase 2, when PUBLIC access enabled)
```

| Access Type       | Web Form URL                            | API Endpoint                                              |
| ----------------- | --------------------------------------- | --------------------------------------------------------- |
| **Authenticated** | `/org/<slug>/workflows/<id>/launch/`    | `POST /api/v1/<org_slug>/internal/workflows/<id>/start/`  |
| **Public**        | `/public/wf/<obfuscated_token>/launch/` | `POST /api/v1/<org_slug>/public/workflows/<token>/start/` |

**Route Design Rationale:**

1. **Org-scoped routes** — All API calls include the org slug, making quota attribution explicit. Even public routes are tied to the workflow owner's org.

2. **`internal` vs `public` prefix** — Clear separation between authenticated and anonymous access. This allows different authentication middleware, rate limiting, and monitoring for each.

3. **Consistent structure** — Both internal and public follow the same pattern: `/api/v1/<org>/<access_type>/workflows/...`

**Example API Calls:**

```bash
# Authenticated user launching a workflow (Phase 1+)
curl -X POST https://validibot.com/api/v1/acme-corp/internal/workflows/123/start/ \
  -H "Authorization: Bearer sv_user_abc123..." \
  -H "Content-Type: application/json" \
  -d '{"input": {...}}'

# Anonymous user launching a public workflow (Phase 2 only)
curl -X POST https://validibot.com/api/v1/acme-corp/public/workflows/abc123token/start/ \
  -H "Content-Type: application/json" \
  -d '{"input": {...}}'
```

**URL Configuration:**

```python
# config/api_router.py

from django.urls import path, include

urlpatterns = [
    # Phase 1+: Authenticated API (internal)
    path(
        "api/v1/<slug:org_slug>/internal/",
        include("validibot.api.internal.urls"),
    ),
    # Phase 2: Public API (anonymous, behind feature flag)
    path(
        "api/v1/<slug:org_slug>/public/",
        include("validibot.api.public.urls"),
    ),
]
```

Public URLs use an **obfuscated token** (long random identifier) rather than the workflow ID to prevent enumeration and provide security through obscurity as an additional layer.

#### 1.3 New Workflow Fields

```python
# validibot/workflows/constants.py

class WorkflowAccessType(models.TextChoices):
    """
    Who can launch this workflow.

    Progressive trust levels from most restrictive to most open.
    """

    ORG_USERS = "ORG_USERS", _("Organization members only")
    ORG_USERS_SUBSET = "ORG_USERS_SUBSET", _("Specific organization members")
    SV_USERS = "SV_USERS", _("Any Validibot user")
    SV_USERS_SUBSET = "SV_USERS_SUBSET", _("Specific Validibot users")
    PUBLIC = "PUBLIC", _("Public (anonymous)")


class PublicChannelMode(models.TextChoices):
    """
    Which channels are enabled for public access.

    Only applies when access_level is PUBLIC.
    """

    DISABLED = "DISABLED", _("Disabled")
    WEB_ONLY = "WEB_ONLY", _("Web form only")
    API_ONLY = "API_ONLY", _("API only")
    BOTH = "BOTH", _("Web form and API")
```

```python
# validibot/workflows/models.py

class Workflow(FeaturedImageMixin, TimeStampedModel):
    # ... existing fields ...

    # === Access Control ===

    access_type = models.CharField(
        max_length=20,
        choices=WorkflowAccessType.choices,
        default=WorkflowAccessType.ORG_USERS,
        help_text=_(
            "Controls who can launch this workflow. "
            "Higher access levels have more restrictions on input types."
        ),
    )

    public_channel_mode = models.CharField(
        max_length=20,
        choices=PublicChannelMode.choices,
        default=PublicChannelMode.DISABLED,
        help_text=_(
            "Which channels allow public (anonymous) access. "
            "Only applies when access_level is PUBLIC."
        ),
    )

    # Obfuscated URL token for public access (regeneratable if compromised)
    public_url_token = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        null=True,
        help_text=_(
            "Random token used in public URLs. Regenerate if URL is compromised."
        ),
    )

    # Scoped access lists (for SV_SCOPED and ORG_SCOPED levels)
    allowed_users = models.ManyToManyField(
        "users.User",
        blank=True,
        related_name="explicitly_allowed_workflows",
        help_text=_("Users explicitly allowed (for scoped access levels)."),
    )

    allowed_roles = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_(
            "Org role codes allowed (for ORG_SCOPED). "
            "E.g., ['EXECUTOR', 'ADMIN']."
        ),
    )

    # === Public Access Settings ===

    public_allowed_domains = ArrayField(
        base_field=models.CharField(max_length=255),
        default=list,
        blank=True,
        help_text=_(
            "Restrict public web launches to these referrer domains. "
            "Use *.example.com for subdomains. Empty = allow all."
        ),
    )

    public_rate_limit_per_minute = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Max public launches per minute. Leave blank for default (20)."
        ),
    )

    def generate_public_url_token(self) -> str:
        """Generate or regenerate the public URL token."""
        import secrets
        self.public_url_token = secrets.token_urlsafe(32)
        self.save(update_fields=["public_url_token"])
        return self.public_url_token

    def get_public_launch_url(self) -> str | None:
        """Get the obfuscated public launch URL, if enabled."""
        if self.access_type != WorkflowAccessType.PUBLIC:
            return None
        if not self.public_url_token:
            self.generate_public_url_token()
        return reverse(
            "public_workflow_launch",
            kwargs={"token": self.public_url_token},
        )
```

#### 1.4 Access Check Logic

```python
# validibot/workflows/permissions.py

from validibot.workflows.constants import WorkflowAccessLevel

class WorkflowLaunchPermission:
    """
    Determines whether a user can launch a specific workflow.

    Implements the access type model:
    - PUBLIC: Anyone (with restrictions on input type/size)
    - SV_USERS: Any authenticated Validibot user
    - SV_USERS_SUBSET: Specific SV users chosen by author
    - ORG_USERS: Members of the workflow's organization
    - ORG_USERS_SUBSET: Specific org members/roles
    """

    def can_launch(
        self,
        workflow: Workflow,
        user: User | None,
        is_public_channel: bool = False,
    ) -> tuple[bool, str | None]:
        """
        Check if user can launch this workflow.

        Args:
            workflow: The workflow to launch.
            user: The authenticated user, or None for public access.
            is_public_channel: True if accessing via public URL/endpoint.

        Returns:
            (allowed, denial_reason) tuple.
        """
        access_type = workflow.access_type

        # === PUBLIC ===
        if access_type == WorkflowAccessType.PUBLIC:
            if is_public_channel:
                # Anonymous access allowed via public channel
                return (True, None)
            elif user:
                # Authenticated users can also use public workflows
                return (True, None)
            else:
                # Anonymous but not via public channel
                return (False, "Authentication required.")

        # === All other access types require authentication ===
        if user is None:
            return (False, "Authentication required.")

        # === SV_USERS ===
        if access_type == WorkflowAccessType.SV_USERS:
            if user.is_active:
                return (True, None)
            return (False, "Account is not active.")

        # === SV_USERS_SUBSET ===
        if access_type == WorkflowAccessType.SV_USERS_SUBSET:
            if workflow.allowed_users.filter(pk=user.pk).exists():
                return (True, None)
            return (False, "You are not in the allowed users list.")

        # === ORG_USERS ===
        if access_type == WorkflowAccessType.ORG_USERS:
            if workflow.org.memberships.filter(
                user=user,
                is_active=True,
            ).exists():
                return (True, None)
            return (False, "You must be a member of this organization.")

        # === ORG_USERS_SUBSET ===
        if access_type == WorkflowAccessType.ORG_USERS_SUBSET:
            membership = workflow.org.memberships.filter(
                user=user,
                is_active=True,
            ).first()
            if not membership:
                return (False, "You must be a member of this organization.")

            user_roles = set(membership.get_role_codes())
            allowed_roles = set(workflow.allowed_roles or [])
            if user_roles & allowed_roles:
                return (True, None)
            return (
                False,
                "You do not have the required role to launch this workflow.",
            )

        return (False, "Unknown access level.")


def get_input_restrictions(
    workflow: Workflow,
    user: User | None,
    is_public_channel: bool,
) -> dict:
    """
    Get input type and size restrictions based on access context.

    Returns:
        Dict with keys: allow_file_upload, max_payload_bytes, allowed_formats.
    """
    if is_public_channel and user is None:
        # Anonymous public access - most restrictive
        return {
            "allow_file_upload": False,
            "max_payload_bytes": 1_048_576,  # 1 MB
            "allowed_formats": ["json", "xml"],
        }
    else:
        # Authenticated access - full capabilities
        return {
            "allow_file_upload": True,
            "max_payload_bytes": 10_485_760,  # 10 MB
            "allowed_formats": ["json", "xml", "file"],
        }
```

### 2. Authentication vs Authorization

**Key design principle:** Users always authenticate with their _own_ credentials, then we check if they're _authorized_ for the target workflow.

#### 2.1 API Authentication

```python
# API request flow:

# 1. User authenticates with their own API key
#    Authorization: Bearer sv_user_abc123...

# 2. We identify the user from the API key
user = authenticate_api_key(request)

# 3. We check if user is authorized for this workflow
workflow = get_workflow(workflow_id)
allowed, reason = WorkflowLaunchPermission().can_launch(
    workflow=workflow,
    user=user,
    is_public_channel=False,
)

# 4. If authorized, launch runs against user's current org quota
if allowed:
    launch_validation_run(
        workflow=workflow,
        user=user,
        org=user.current_org,  # User's org, not workflow's org
    )
```

**Why this works for cross-org workflows:**

- Author sets `access_level = ANY_SV_USER` on their workflow.
- Any authenticated SV user can call the API with their own API key.
- The user is identified, authorized by the workflow's settings.
- Usage is charged to the _user's_ current org, not the workflow author's org.

This is fair because:

- The user chose to run the validation.
- Their org benefits from the results.
- The workflow author provided the value but isn't paying for others' usage.

#### 2.2 Web Form Authentication

| Access Type          | Web Form Behavior                                      |
| -------------------- | ------------------------------------------------------ |
| **PUBLIC**           | Public URL, no login required, CAPTCHA enforced        |
| **SV_USERS**         | Standard URL, login required, redirects to login first |
| **SV_USERS_SUBSET**  | Standard URL, login required, checks allowed list      |
| **ORG_USERS**        | Org-scoped URL, login required, checks membership      |
| **ORG_USERS_SUBSET** | Org-scoped URL, login required, checks role            |

### 3. Quota Attribution

**All usage is always attributed to the workflow author's organization**, regardless of who launches the workflow or how they authenticate.

> **Note:** For full details on metering, credits, and billing, see [ADR-2025-11-28-pricing-system](./2025-11-28-pricing-system.md).

| Scenario                                  | Quota Charged To     |
| ----------------------------------------- | -------------------- |
| Public anonymous user launches via web    | Workflow owner's org |
| Any SV user launches via API              | Workflow owner's org |
| Org member launches via web form          | Workflow owner's org |
| Cross-org user launches (SV_USERS access) | Workflow owner's org |

**Rationale:**

1. **Author controls access** — The workflow author decides who can launch their workflow. If they open it to the public or all SV users, they accept the quota cost.

2. **Simple mental model** — "My workflows use my quota" is easy to understand. No surprises about which org gets charged.

3. **Prevents quota gaming** — Without this, a user could launch expensive validations against someone else's quota by finding public workflows.

4. **Encourages thoughtful sharing** — Authors will think carefully before setting `access_type=PUBLIC` or `SV_USERS` since it costs them.

5. **Natural upgrade path** — Heavy public usage drives the author to upgrade their plan, which is the desired business outcome.

### 4. Rate Limiting (Critical)

#### 3.1 Multi-Layer Rate Limiting

Implement rate limits at multiple levels to prevent abuse:

```python
# validibot/workflows/rate_limiting.py

from django.core.cache import cache
from django.conf import settings

# Default limits (can be overridden per-org or per-workflow)
DEFAULT_PUBLIC_RATE_LIMITS = {
    "per_ip_per_minute": 5,          # Per IP address
    "per_workflow_per_minute": 20,   # Per workflow across all IPs
    "per_org_per_hour": 100,         # Per organization across all workflows
}


class RateLimitExceeded(Exception):
    """Raised when a rate limit is exceeded."""

    def __init__(self, detail: str, retry_after: int = 60):
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(detail)


class PublicRateLimiter:
    """
    Rate limiter for public workflow launches.

    Uses Redis/cache backend for distributed rate limiting.
    Implements token bucket algorithm for smooth rate limiting.
    """

    def check_and_increment(
        self,
        *,
        workflow: Workflow,
        client_ip: str,
    ) -> None:
        """
        Check rate limits and increment counters.

        Raises:
            RateLimitExceeded: If any rate limit is exceeded.
        """
        org = workflow.org

        # 1. Per-IP limit (strictest)
        ip_key = f"public_rate:ip:{client_ip}:wf:{workflow.pk}"
        ip_limit = DEFAULT_PUBLIC_RATE_LIMITS["per_ip_per_minute"]
        if not self._check_limit(ip_key, ip_limit, window_seconds=60):
            raise RateLimitExceeded(
                detail=_("Too many requests. Please wait before trying again."),
                retry_after=60,
            )

        # 2. Per-workflow limit
        wf_key = f"public_rate:wf:{workflow.pk}"
        wf_limit = (
            workflow.public_rate_limit_per_minute
            or DEFAULT_PUBLIC_RATE_LIMITS["per_workflow_per_minute"]
        )
        if not self._check_limit(wf_key, wf_limit, window_seconds=60):
            raise RateLimitExceeded(
                detail=_("This workflow is receiving too many requests."),
                retry_after=60,
            )

        # 3. Per-org limit (hourly)
        org_key = f"public_rate:org:{org.pk}"
        org_limit = getattr(org.quota, "public_launches_per_hour", None) or DEFAULT_PUBLIC_RATE_LIMITS["per_org_per_hour"]
        if not self._check_limit(org_key, org_limit, window_seconds=3600):
            raise RateLimitExceeded(
                detail=_("Service temporarily unavailable. Please try again later."),
                retry_after=300,
            )

    def _check_limit(self, key: str, limit: int, window_seconds: int) -> bool:
        """
        Check if under limit and increment counter.

        Returns True if under limit, False if exceeded.
        """
        current = cache.get(key, 0)
        if current >= limit:
            return False
        cache.set(key, current + 1, timeout=window_seconds)
        return True
```

#### 3.2 Rate Limit Headers

Return standard rate limit headers in responses:

```http
HTTP/1.1 201 Created
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 3
X-RateLimit-Reset: 1732800000

HTTP/1.1 429 Too Many Requests
Retry-After: 60
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1732800000
```

### 4. Bot Protection

#### 4.1 CAPTCHA Integration

Integrate Cloudflare Turnstile (privacy-friendly alternative to reCAPTCHA):

```python
# validibot/core/captcha.py

import httpx
from django.conf import settings

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class CaptchaVerificationError(Exception):
    """Raised when CAPTCHA verification fails."""
    pass


async def verify_turnstile_token(token: str, client_ip: str) -> bool:
    """
    Verify a Cloudflare Turnstile token.

    Args:
        token: The cf-turnstile-response token from the client.
        client_ip: The client's IP address.

    Returns:
        True if verification succeeded.

    Raises:
        CaptchaVerificationError: If verification fails.
    """
    if not settings.TURNSTILE_SECRET_KEY:
        # CAPTCHA disabled in development
        return True

    async with httpx.AsyncClient() as client:
        response = await client.post(
            TURNSTILE_VERIFY_URL,
            data={
                "secret": settings.TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": client_ip,
            },
        )

    result = response.json()
    if not result.get("success"):
        raise CaptchaVerificationError(
            _("CAPTCHA verification failed. Please try again."),
        )

    return True
```

#### 4.2 Workflow-Level CAPTCHA Settings

```python
# validibot/workflows/models.py

class CaptchaRequirement(models.TextChoices):
    """When to require CAPTCHA for public launches."""

    NEVER = "NEVER", _("Never – Not recommended")
    SUSPICIOUS = "SUSPICIOUS", _("Suspicious activity only")
    ALWAYS = "ALWAYS", _("Always require")


class Workflow(FeaturedImageMixin, TimeStampedModel):
    # ... existing fields ...

    public_captcha_mode = models.CharField(
        max_length=20,
        choices=CaptchaRequirement.choices,
        default=CaptchaRequirement.ALWAYS,
        help_text=_(
            "When to require CAPTCHA verification for public launches."
        ),
    )
```

#### 4.3 Honeypot Fields

Add invisible honeypot fields to web forms:

```python
# validibot/workflows/forms.py

class PublicLaunchForm(forms.Form):
    """Form for public workflow launches with anti-spam measures."""

    # Real fields
    payload = forms.CharField(widget=forms.Textarea)
    file = forms.FileField(required=False)

    # Honeypot fields (should remain empty)
    website = forms.CharField(required=False, widget=forms.HiddenInput)
    email_confirm = forms.CharField(required=False, widget=forms.HiddenInput)

    # Timing check (form should take >2 seconds to fill)
    form_timestamp = forms.CharField(widget=forms.HiddenInput)

    def clean(self):
        cleaned_data = super().clean()

        # Honeypot check
        if cleaned_data.get("website") or cleaned_data.get("email_confirm"):
            raise ValidationError(_("Spam detected."))

        # Timing check
        timestamp = cleaned_data.get("form_timestamp")
        if timestamp:
            try:
                form_time = float(timestamp)
                elapsed = time.time() - form_time
                if elapsed < 2.0:  # Form filled too fast
                    raise ValidationError(_("Please slow down."))
            except ValueError:
                pass

        return cleaned_data
```

### 5. Domain Restrictions

For embedded forms, allow workflow authors to restrict submissions to specific referrer domains:

```python
# validibot/workflows/security.py

from urllib.parse import urlparse

class DomainRestrictionError(Exception):
    """Raised when a request comes from an unauthorized domain."""
    pass


def check_domain_restriction(
    workflow: Workflow,
    request: HttpRequest,
) -> None:
    """
    Verify the request origin matches allowed domains.

    Only applies to public web launches with configured domain restrictions.
    """
    allowed_domains = workflow.public_allowed_domains or []
    if not allowed_domains:
        return  # No restriction

    # Check Referer header
    referer = request.META.get("HTTP_REFERER", "")
    origin = request.META.get("HTTP_ORIGIN", "")

    source_url = referer or origin
    if not source_url:
        # No referer/origin – might be direct access or privacy mode
        # Decision: allow but log for monitoring
        logger.warning(
            "Public launch without referer/origin for workflow %s",
            workflow.pk,
        )
        return

    parsed = urlparse(source_url)
    source_domain = parsed.netloc.lower()

    # Check against allowed domains (supports wildcards)
    for allowed in allowed_domains:
        allowed = allowed.lower().strip()
        if allowed.startswith("*."):
            # Wildcard subdomain match
            base = allowed[2:]
            if source_domain == base or source_domain.endswith("." + base):
                return
        elif source_domain == allowed:
            return

    raise DomainRestrictionError(
        _(
            "This workflow cannot be accessed from %(domain)s."
        ) % {"domain": source_domain},
    )
```

### 6. API Authentication for Public Access

#### 6.1 Public API Token

For API access, require a public API token (distinct from user API keys):

```python
# validibot/workflows/models.py

class Workflow(FeaturedImageMixin, TimeStampedModel):
    # ... existing fields ...

    public_api_token = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        unique=True,
        help_text=_(
            "Token required for public API launches. "
            "Regenerate if compromised."
        ),
    )

    def regenerate_public_api_token(self) -> str:
        """Generate a new public API token."""
        import secrets
        self.public_api_token = f"sv_pub_{secrets.token_urlsafe(32)}"
        self.save(update_fields=["public_api_token"])
        return self.public_api_token
```

#### 6.2 API Request Format

Public API requests use the workflow's UUID and public token:

```http
POST /api/public/workflows/{workflow_uuid}/start/
X-Public-Token: sv_pub_abc123...
Content-Type: application/json

{
  "name": "my-file.json",
  "content": "{...}"
}
```

This differs from the authenticated API:

- Uses `/api/public/` prefix.
- Requires `X-Public-Token` header instead of user authentication.
- Does not require user session or API key.

### 7. Monitoring and Alerting

#### 7.1 Public Usage Metrics

Track detailed metrics for public launches:

```python
# validibot/workflows/models.py

class PublicLaunchMetrics(TimeStampedModel):
    """
    Aggregated metrics for public workflow launches.

    Rolled up hourly for dashboards and alerting.
    """

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="public_metrics",
    )
    hour = models.DateTimeField()  # Truncated to hour

    # Counts
    total_launches = models.IntegerField(default=0)
    successful_launches = models.IntegerField(default=0)
    failed_launches = models.IntegerField(default=0)
    rate_limited_requests = models.IntegerField(default=0)
    captcha_failures = models.IntegerField(default=0)

    # Sources
    web_launches = models.IntegerField(default=0)
    api_launches = models.IntegerField(default=0)

    # Unique counts
    unique_ips = models.IntegerField(default=0)
    unique_referers = models.IntegerField(default=0)

    class Meta:
        unique_together = [("workflow", "hour")]
        indexes = [
            models.Index(fields=["workflow", "hour"]),
            models.Index(fields=["hour"]),
        ]
```

#### 7.2 Anomaly Detection and Alerts

```python
# validibot/workflows/monitoring.py

class PublicUsageMonitor:
    """
    Monitors public workflow usage for anomalies.
    """

    ALERT_THRESHOLDS = {
        "sudden_spike_multiplier": 5,  # 5x normal traffic
        "rate_limit_percentage": 20,   # >20% requests rate limited
        "captcha_failure_percentage": 30,
        "single_ip_percentage": 50,    # >50% traffic from one IP
    }

    def check_anomalies(self, workflow: Workflow, window_hours: int = 1) -> list[str]:
        """
        Check for usage anomalies and return alert messages.
        """
        alerts = []
        recent_metrics = self._get_recent_metrics(workflow, window_hours)
        baseline = self._get_baseline_metrics(workflow)

        # Spike detection
        if recent_metrics.total_launches > baseline.avg_hourly * self.ALERT_THRESHOLDS["sudden_spike_multiplier"]:
            alerts.append(
                f"Traffic spike: {recent_metrics.total_launches} launches "
                f"(baseline: {baseline.avg_hourly})"
            )

        # Rate limit abuse
        if recent_metrics.total_launches > 0:
            rate_limit_pct = (
                recent_metrics.rate_limited_requests / recent_metrics.total_launches * 100
            )
            if rate_limit_pct > self.ALERT_THRESHOLDS["rate_limit_percentage"]:
                alerts.append(
                    f"High rate limiting: {rate_limit_pct:.1f}% of requests"
                )

        # CAPTCHA failures
        if recent_metrics.total_launches > 10:
            captcha_pct = (
                recent_metrics.captcha_failures / recent_metrics.total_launches * 100
            )
            if captcha_pct > self.ALERT_THRESHOLDS["captcha_failure_percentage"]:
                alerts.append(
                    f"High CAPTCHA failure rate: {captcha_pct:.1f}%"
                )

        return alerts


def send_abuse_alert(workflow: Workflow, alerts: list[str]) -> None:
    """
    Notify the workflow owner of potential abuse.
    """
    from validibot.notifications.models import Notification

    Notification.objects.create(
        user=workflow.user,
        org=workflow.org,
        type=Notification.Type.SECURITY_ALERT,
        payload={
            "workflow_id": workflow.pk,
            "workflow_name": workflow.name,
            "alerts": alerts,
        },
    )
```

### 8. UI Implementation

#### 8.1 Public Launch Form Integration

Extend `PublicWorkflowInfoView` to include the launch form:

```python
# validibot/workflows/views.py

class PublicWorkflowInfoView(DetailView):
    """
    Public display of workflow information with optional launch form.
    """

    template_name = "workflows/public/workflow_info.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = context["workflow"]
        user = self.request.user

        # Determine if public launch is available
        can_public_launch = (
            workflow.public_launch_mode in [
                PublicLaunchMode.PUBLIC_WEB,
                PublicLaunchMode.PUBLIC_BOTH,
            ]
            and workflow.is_active
        )

        # Check if authenticated user should use regular launch instead
        if user.is_authenticated and workflow.can_execute(user=user):
            context["show_authenticated_launch"] = True
            context["show_public_launch"] = False
        else:
            context["show_authenticated_launch"] = False
            context["show_public_launch"] = can_public_launch

        if context["show_public_launch"]:
            context["public_launch_form"] = PublicLaunchForm(
                initial={"form_timestamp": time.time()},
            )
            context["captcha_site_key"] = settings.TURNSTILE_SITE_KEY
            context["captcha_required"] = (
                workflow.public_captcha_mode == CaptchaRequirement.ALWAYS
            )

        return context

    def post(self, request, *args, **kwargs):
        """Handle public launch form submission."""
        workflow = self.get_object()

        # Security checks
        if workflow.public_launch_mode not in [
            PublicLaunchMode.PUBLIC_WEB,
            PublicLaunchMode.PUBLIC_BOTH,
        ]:
            return HttpResponseForbidden(_("Public launches are not enabled."))

        try:
            # Domain restriction check
            check_domain_restriction(workflow, request)

            # Rate limit check
            client_ip = get_client_ip(request)
            PublicRateLimiter().check_and_increment(
                workflow=workflow,
                client_ip=client_ip,
            )

            # CAPTCHA verification (if required)
            if workflow.public_captcha_mode == CaptchaRequirement.ALWAYS:
                token = request.POST.get("cf-turnstile-response")
                verify_turnstile_token(token, client_ip)

        except (DomainRestrictionError, RateLimitExceeded, CaptchaVerificationError) as e:
            messages.error(request, str(e))
            return self.get(request, *args, **kwargs)

        # Process the form
        form = PublicLaunchForm(request.POST, request.FILES)
        if not form.is_valid():
            context = self.get_context_data(public_launch_form=form)
            return self.render_to_response(context)

        # Build and launch
        try:
            submission_build = build_submission_from_form(
                request=request,
                workflow=workflow,
                cleaned_data=form.cleaned_data,
                anonymous=True,
            )
            launch_result = launch_public_validation_run(
                submission_build=submission_build,
                request=request,
                workflow=workflow,
            )
            # Redirect to results (read-only public view)
            return redirect(
                "public_validation_result",
                run_uuid=launch_result.validation_run.uuid,
            )
        except QuotaExceededError:
            messages.error(
                request,
                _("This workflow has reached its daily limit. Please try again tomorrow."),
            )
            return self.get(request, *args, **kwargs)
```

#### 8.2 Workflow Settings UI

Add public launch settings to the workflow edit form:

```django
{# templates/workflows/partials/workflow_public_settings.html #}

<div class="card mb-4">
  <div class="card-header">
    <h5>{% trans "Public Access Settings" %}</h5>
  </div>
  <div class="card-body">
    <div class="alert alert-info">
      <i class="bi bi-info-circle"></i>
      {% blocktrans %}
      Public launches allow anyone to use this workflow without signing in.
      All usage counts against your organization's quota.
      {% endblocktrans %}
    </div>

    <div class="mb-3">
      <label class="form-label">{% trans "Public Launch Mode" %}</label>
      {{ form.public_launch_mode|crispy }}
    </div>

    {% if form.public_launch_mode.value != 'DISABLED' %}
      <div class="mb-3">
        <label class="form-label">{% trans "CAPTCHA Requirement" %}</label>
        {{ form.public_captcha_mode|crispy }}
        <small class="text-muted">
          {% trans "Protects against automated abuse. 'Always' is recommended." %}
        </small>
      </div>

      <div class="mb-3">
        <label class="form-label">{% trans "Allowed Domains (optional)" %}</label>
        {{ form.public_allowed_domains|crispy }}
        <small class="text-muted">
          {% trans "Restrict to specific domains. Use *.example.com for subdomains." %}
        </small>
      </div>

      <div class="mb-3">
        <label class="form-label">{% trans "Rate Limit (requests/minute)" %}</label>
        {{ form.public_rate_limit_per_minute|crispy }}
        <small class="text-muted">
          {% trans "Leave blank for default (20/minute)." %}
        </small>
      </div>

      {% if form.public_launch_mode.value in ['PUBLIC_API', 'PUBLIC_BOTH'] %}
        <div class="mb-3">
          <label class="form-label">{% trans "Public API Token" %}</label>
          <div class="input-group">
            <input type="text"
                   class="form-control font-monospace"
                   value="{{ workflow.public_api_token|default:'Not generated' }}"
                   readonly>
            <button type="button"
                    class="btn btn-outline-secondary"
                    hx-post="{% org_url 'workflows:regenerate_public_token' pk=workflow.pk %}"
                    hx-confirm="{% trans 'Regenerate token? Existing integrations will stop working.' %}">
              {% trans "Regenerate" %}
            </button>
          </div>
          <small class="text-muted">
            {% trans "Include this token in the X-Public-Token header for API requests." %}
          </small>
        </div>
      {% endif %}
    {% endif %}
  </div>
</div>
```

### 9. Public API Endpoint

#### 9.1 Dedicated Public ViewSet

```python
# validibot/workflows/views.py

class PublicWorkflowLaunchViewSet(viewsets.ViewSet):
    """
    Public API endpoint for launching workflows without authentication.

    Security:
        - Requires valid public API token in X-Public-Token header
        - Subject to rate limiting
        - Usage attributed to workflow owner's organization
    """

    authentication_classes = []  # No authentication
    permission_classes = [AllowAny]
    throttle_classes = []  # We handle rate limiting ourselves

    @action(detail=True, methods=["post"], url_path="start")
    def start_validation(self, request, workflow_uuid=None):
        # Lookup workflow by UUID
        workflow = get_object_or_404(
            Workflow.objects.filter(
                uuid=workflow_uuid,
                is_active=True,
                public_launch_mode__in=[
                    PublicLaunchMode.PUBLIC_API,
                    PublicLaunchMode.PUBLIC_BOTH,
                ],
            ),
        )

        # Verify public API token
        provided_token = request.META.get("HTTP_X_PUBLIC_TOKEN")
        if not provided_token or provided_token != workflow.public_api_token:
            return APIResponse(
                {"detail": _("Invalid or missing public API token.")},
                status=HTTPStatus.UNAUTHORIZED,
            )

        # Rate limiting
        client_ip = get_client_ip(request)
        try:
            PublicRateLimiter().check_and_increment(
                workflow=workflow,
                client_ip=client_ip,
            )
        except RateLimitExceeded as e:
            return APIResponse(
                {"detail": str(e.detail), "code": "rate_limited"},
                status=HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": str(e.retry_after)},
            )

        # Quota check
        if not check_org_quota(workflow.org):
            return APIResponse(
                {"detail": _("Quota exceeded."), "code": "quota_exceeded"},
                status=HTTPStatus.TOO_MANY_REQUESTS,
            )

        # Build submission and launch
        try:
            submission_build = build_submission_from_api(
                request=request,
                workflow=workflow,
                user=None,  # Anonymous
                project=workflow.project,
                serializer_factory=self.get_serializer,
                multipart_payload=lambda: request.data,
            )
        except LaunchValidationError as exc:
            return APIResponse(exc.payload, status=exc.status_code)

        return launch_public_api_validation_run(
            request=request,
            workflow=workflow,
            submission_build=submission_build,
        )
```

#### 9.2 URL Configuration

```python
# config/api_router.py

from validibot.workflows.views import PublicWorkflowLaunchViewSet

# Authenticated API
router.register("workflows", WorkflowViewSet, basename="workflow")

# Public API (separate prefix)
public_router = DefaultRouter()
public_router.register(
    "public/workflows",
    PublicWorkflowLaunchViewSet,
    basename="public-workflow",
)

urlpatterns = router.urls + public_router.urls
```

---

## Data Model Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                          Workflow                                │
│  + access_type: ORG_USERS | ORG_USERS_SUBSET | SV_USERS |       │
│                 SV_USERS_SUBSET | PUBLIC                         │
│  + public_channel_mode: DISABLED | WEB_ONLY | API_ONLY | BOTH   │
│  + public_url_token: "abc123..." (obfuscated URL identifier)    │
│  + allowed_users: M2M → User (for SV_USERS_SUBSET)              │
│  + allowed_roles: ["EXECUTOR", "ADMIN"] (for ORG_USERS_SUBSET)  │
│  + public_allowed_domains: ["example.com", "*.acme.org"]        │
│  + public_rate_limit_per_minute: 20 (nullable)                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       ValidationRun                              │
│  + user: User or NULL for anonymous launches                     │
│  + source: LAUNCH_PAGE | API | PUBLIC_WEB | PUBLIC_API          │
│  + org: ALWAYS workflow owner's org (quota attribution)         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      UsageCounter                                │
│  + submissions: Incremented for ALL launches                     │
│  + org: ALWAYS workflow owner's org                              │
└─────────────────────────────────────────────────────────────────┘

Quota Attribution (simple rule):
┌──────────────────────────────────────────────────────────────────┐
│  ALL launches (any access tier) → Workflow owner's organization  │
│                                                                  │
│  "My workflows use my quota" — author pays for all usage.        │
└──────────────────────────────────────────────────────────────────┘
```

---

## MVP Scope

### Phased Rollout

Anonymous (PUBLIC) access introduces significant security surface and requires careful monitoring. We will roll out access types in two phases:

| Phase       | Timeline      | Access Types Enabled                                   | Anonymous Allowed |
| ----------- | ------------- | ------------------------------------------------------ | ----------------- |
| **Phase 1** | January Alpha | ORG_USERS, ORG_USERS_SUBSET, SV_USERS, SV_USERS_SUBSET | ❌ No             |
| **Phase 2** | Post-Alpha    | All above + PUBLIC                                     | ✅ Yes            |

**System-Level Feature Flag:**

```python
# config/settings/base.py

# Feature flag to enable/disable anonymous (PUBLIC) workflow access.
# Phase 1: False (all access requires authentication)
# Phase 2: True (PUBLIC access type becomes available)
WORKFLOW_PUBLIC_ACCESS_ENABLED = env.bool(
    "WORKFLOW_PUBLIC_ACCESS_ENABLED",
    default=False,
)
```

**Enforcement:**

```python
# validibot/workflows/models.py

class Workflow(FeaturedImageMixin, TimeStampedModel):

    def clean(self):
        super().clean()
        # Enforce system-level PUBLIC access restriction
        if self.access_type == WorkflowAccessType.PUBLIC:
            if not settings.WORKFLOW_PUBLIC_ACCESS_ENABLED:
                raise ValidationError({
                    "access_type": _(
                        "Public (anonymous) access is not yet available. "
                        "Please choose a different access type."
                    ),
                })
```

**UI Enforcement:**

When `WORKFLOW_PUBLIC_ACCESS_ENABLED=False`, the PUBLIC option should be hidden or disabled in the workflow settings form, with a tooltip explaining it's coming soon.

### Phase 1: In Scope (January Alpha)

1. **`access_type` field** on Workflow with UI selector.
2. **Access types**: ORG_USERS, SV_USERS (subset types future).
3. **System feature flag** to disable PUBLIC access type.
4. **Quota attribution** – All launches → workflow owner's org.

### Phase 2: In Scope (Post-Alpha)

1. **Enable `WORKFLOW_PUBLIC_ACCESS_ENABLED`** feature flag.
2. **`public_channel_mode`** for PUBLIC access channel control.
3. **Obfuscated public URLs** via `public_url_token`.
4. **Public web form** at `/public/wf/<token>/launch/`.
5. **Text-only input restriction** – Public launches limited to JSON/XML (1 MB); authenticated users get file uploads (10 MB).
6. **Input sanitization** – `defusedxml` for XML, depth limits for JSON.
7. **Rate limiting** (per-IP, per-workflow, per-org) via cache.
8. **CAPTCHA integration** (Cloudflare Turnstile) for public web forms.
9. **Honeypot fields** for basic bot protection.

### Out of Scope (Future)

- Subset access types (SV_USERS_SUBSET, ORG_USERS_SUBSET with allowed_users/roles).
- Public API endpoint (focus on web form for alpha).
- Domain restrictions for embedded forms.
- Per-workflow rate limit overrides (use defaults).
- Anomaly detection and alerting.
- `PublicLaunchMetrics` detailed tracking.
- Suspicious activity CAPTCHA mode (just use ALWAYS).
- Embeddable widget/iframe mode.
- Public file uploads (may reconsider with "auth-lite" options like temporary tokens shared by authenticated users).

### Future: Reusable User Picker Component

For `SV_USERS_SUBSET` and `ORG_USERS_SUBSET` access types, workflow settings will need a user selection UI similar to the "Invite member" feature. We should extract the existing invite user search into a reusable component.

**Existing implementation to refactor:**

- `members/views.py::InviteSearchView` – Type-ahead search returning matching users
- `members/partials/invite_search_results.html` – Radio button list of matching users
- `members/member_list.html` – HTMx search input with `hx-get` to search endpoint

**Proposed reusable components:**

```
core/
  partials/
    user_picker.html           # Search input + results container
    user_picker_results.html   # Radio or checkbox list of users

  views.py
    class UserSearchView       # Generic type-ahead search
      - search_scope: "all_sv_users" | "org_members"
      - exclude_users: QuerySet to exclude
      - multi_select: bool (radio vs checkbox)
```

**Usage in workflow settings:**

```django
{# For SV_USERS_SUBSET: search all SV users #}
{% include "core/partials/user_picker.html" with
    search_url=url_for_sv_user_search
    target_field="allowed_users"
    multi_select=True
%}

{# For ORG_USERS_SUBSET: search only org members #}
{% include "core/partials/user_picker.html" with
    search_url=url_for_org_user_search
    target_field="allowed_users"
    multi_select=True
%}
```

This allows the workflow author to search and select specific users who can access the workflow, using the same UX pattern as member invitations.

---

## Security Checklist

### Before Launch

- [ ] Rate limiting tested under load
- [ ] CAPTCHA integration verified
- [ ] Honeypot fields functional
- [ ] Quota enforcement tested
- [ ] Error messages don't leak sensitive info
- [ ] Public runs properly attributed to org
- [ ] Logging captures client IP for abuse investigation
- [ ] Terms of Service updated for public usage

### Ongoing

- [ ] Monitor rate limit hit rates
- [ ] Review usage patterns weekly
- [ ] Respond to abuse reports within 24 hours
- [ ] Quarterly security review of public endpoints

---

## Consequences

### Positive

1. **New use cases** – Embedded forms, public APIs, lead generation.
2. **Reduced friction** – End users don't need accounts.
3. **Revenue opportunity** – Heavy public usage drives plan upgrades.
4. **Marketing value** – Public workflows showcase platform capabilities.

### Negative

1. **Security surface** – More attack vectors to defend.
2. **Abuse risk** – Requires active monitoring.
3. **Complexity** – More code paths, more settings.
4. **Support burden** – Public users may need help.

### Risks and Mitigations

| Risk                        | Mitigation                                |
| --------------------------- | ----------------------------------------- |
| DDoS via public endpoints   | Multi-layer rate limiting, CDN protection |
| Quota theft (spam launches) | CAPTCHA, honeypots, rate limits           |
| Expensive compute abuse     | Per-org hourly limits, alert on spikes    |
| Token leakage               | Separate tokens, easy regeneration        |
| Reputation risk             | Domain restrictions, content monitoring   |

---

## Implementation Checklist

### Models & Migrations

- [ ] Add `access_type` to Workflow (WorkflowAccessType choices)
- [ ] Add `public_channel_mode` to Workflow (PublicChannelMode choices)
- [ ] Add `public_url_token` to Workflow (obfuscated URL identifier)
- [ ] Add `allowed_users` M2M to Workflow (for subset access - future)
- [ ] Add `allowed_roles` ArrayField to Workflow (for org-subset access - future)
- [ ] Add `public_allowed_domains` ArrayField to Workflow
- [ ] Add `public_rate_limit_per_minute` to Workflow
- [ ] Add `PUBLIC_WEB`, `PUBLIC_API` to `ValidationRunSource`
- [ ] Migration with sensible defaults (access_type=ORG_USERS)

### Authorization Layer

- [ ] Implement `WorkflowLaunchPermission.can_launch()` for all access types
- [ ] Implement `get_input_restrictions()` for size/format limits
- [ ] Implement `determine_quota_org()` for attribution logic
- [ ] Add URL routes for public launch (`/public/wf/<token>/launch/`)

### API Routes (Phase 1)

- [ ] Set up `/api/v1/<org_slug>/internal/` route namespace
- [ ] Create `validibot/api/internal/urls.py` with workflow endpoints
- [ ] Ensure all internal API endpoints require authentication
- [ ] Verify org slug matches authenticated user's org membership

### API Routes (Phase 2 - Public)

- [ ] Set up `/api/v1/<org_slug>/public/` route namespace (behind feature flag)
- [ ] Create `validibot/api/public/urls.py` with public workflow endpoints
- [ ] Public routes use obfuscated token instead of workflow ID
- [ ] Public routes do NOT require authentication
- [ ] Guard public routes with `WORKFLOW_PUBLIC_ACCESS_ENABLED` feature flag

### Security Layer

- [ ] Implement `PublicRateLimiter` with cache backend
- [ ] Integrate Cloudflare Turnstile for public web forms
- [ ] Add honeypot fields to public forms
- [ ] Implement domain restriction checking (future)
- [ ] Add rate limit headers to responses
- [ ] Add `defusedxml` dependency for safe XML parsing
- [ ] Implement `validate_json_depth()` for nesting limits
- [ ] Implement `parse_xml_safely()` wrapper
- [ ] Configure size limits: 1 MB public, 10 MB authenticated
- [ ] Harden `_detect_xml_schema_type` (workflows/forms.py) to align with defused XML guidance or replace it with `defusedxml`

### Views & Forms

- [ ] Create `PublicWorkflowLaunchView` at obfuscated URL
- [ ] Create `PublicLaunchForm` with anti-spam measures
- [ ] Add access level settings to workflow edit UI
- [ ] Generate `public_url_token` on first public enable
- [ ] Update authenticated launch views to check `access_level`

### Testing

- [ ] Access type enforcement (each type tested)
- [ ] Quota attribution: ALL launches → workflow owner's org
- [ ] Cross-org access with SV_USERS type (quota still to author)
- [ ] Rate limit enforcement tests
- [ ] CAPTCHA flow tests
- [ ] Honeypot detection tests
- [ ] Input restrictions: public JSON/XML only, no files
- [ ] Input restrictions: authenticated allows files
- [ ] JSON depth limit rejects deeply nested payloads
- [ ] XML entity expansion attack blocked (Billion Laughs)
- [ ] XXE attack blocked
- [ ] Size limits enforced (1 MB public, 10 MB auth)

### Documentation

- [ ] API documentation for access tiers
- [ ] User guide for configuring workflow access
- [ ] Security best practices guide
- [ ] Abuse response playbook

---

## References

- [Typeform: Share your form](https://www.typeform.com/help/a/share-your-typeform-360052795352/) – Public form sharing patterns
- [Formspree: Restrict to Domain](https://help.formspree.io/hc/en-us/articles/360013580873-Restrict-to-Domain) – Domain restriction implementation
- [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/) – Privacy-friendly CAPTCHA
- [Stripe: Rate Limiting](https://stripe.com/docs/rate-limits) – API rate limit patterns
- [OWASP: Rate Limiting](https://cheatsheetseries.owasp.org/cheatsheets/Denial_of_Service_Cheat_Sheet.html) – Security best practices
- [OWASP: XXE Prevention](https://cheatsheetseries.owasp.org/cheatsheets/XML_External_Entity_Prevention_Cheat_Sheet.html) – XML entity attack prevention
- [defusedxml Documentation](https://github.com/tiran/defusedxml) – Safe XML parsing for Python
- [Supabase: API Keys](https://supabase.com/docs/guides/api#api-keys) – Public vs private key patterns

```

```
