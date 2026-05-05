"""Support bundle — schemas + redaction rules for operator diagnostics.

A support bundle is the answer to "we need help; what do we send?" Operators
run ``just self-hosted collect-support-bundle`` (or the GCP equivalent), and
the recipe assembles a zip archive containing enough deployment metadata
for support to diagnose problems — without leaking secrets or raw user
content.

Two layers contribute to a bundle:

1. **App-side snapshot.** Captured by the ``collect_support_bundle``
   Django management command. This module's
   :class:`SupportBundleAppSnapshot` is the schema. It includes the
   doctor's most recent verdict, the live migration head, the
   redacted settings inventory, and version info — everything Django
   can introspect about its own deployment.

2. **Host-side artefacts.** Captured by the just recipe outside the
   container: ``docker compose ps`` / ``gcloud run services
   describe``, recent container logs, disk usage, validator backend
   inventory. The recipe zips both halves together.

Why redaction is data-driven, not regex-bashing
================================================

The naive approach is to capture everything, then run regex over the
final blob to scrub secrets. That's fragile — secrets can appear in
unexpected formats (URL-encoded, base64-wrapped, multi-line PEM
keys), and a missed pattern is a quiet leak.

Instead, this module enumerates Django settings *by name* and applies
a sensitive-pattern allowlist. A setting whose name matches any
pattern (`SECRET`, `KEY`, `TOKEN`, `PASSWORD`, etc.) gets
`[REDACTED]` regardless of its value's shape. Setting names are
stable, declared by code; values are user-controlled and arbitrary.
Pinning on the name is the more honest contract.

What the bundle does NOT include
=================================

- Raw submission contents — never. Submissions live in
  ``DATA_STORAGE_ROOT`` and are excluded from the bundle entirely.
- Validator output payloads — same reasoning.
- Database contents — the support bundle is metadata, not a data
  dump. Operators who need a data dump run ``just self-hosted
  backup`` and share that out-of-band.
- Signing keys — never. Pro signing private keys live in
  ``/run/validibot-keys/`` and are filtered by name and by location.
- Live secret values — only the *names* of secrets and the *fact
  that they are set* are recorded.

Schema versioning
=================

``SUPPORT_BUNDLE_SCHEMA_VERSION`` is the contract string. Additive
fields (new optional fields with defaults) preserve the version;
removing or renaming fields requires a v2 bump. Self-hosted and GCP
producers share this single schema, so support tooling can be
written without per-target branches.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

# Bump only on breaking changes; additive changes stay v1.
SUPPORT_BUNDLE_SCHEMA_VERSION = "validibot.support-bundle.v1"

# Sentinel used to replace sensitive values. The literal string makes the
# redaction visible in tooling that ingests the bundle (e.g. a support
# engineer who greps for "[REDACTED]" knows where the gaps are).
REDACTED_SENTINEL = "[REDACTED]"

# Setting-name fragments that mark a value as sensitive. Matching is case-
# insensitive substring — so ``DJANGO_SECRET_KEY``, ``OIDC_CLIENT_SECRET``,
# ``SENTRY_DSN_PRIVATE`` all redact correctly without listing every variant.
#
# Order doesn't matter for correctness, but is roughly "most common first"
# for readability.
SENSITIVE_NAME_FRAGMENTS = frozenset(
    {
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "TOKEN",
        # ``KEY`` catches both bare ``API_KEY``, ``ENCRYPTION_KEY``,
        # ``MFA_ENCRYPTION_KEY``, and the more specific variants. False
        # positives (a name like ``KEY_FOR_X`` that isn't really a
        # credential) are acceptable — redacting a non-secret is a
        # small support inconvenience; missing a real secret is a
        # data leak.
        "KEY",
        "ENCRYPTION",
        "PRIVATE",
        "CREDENTIAL",
        "AUTH",
        "DSN",  # Sentry DSN, etc. — contains an embedded auth secret.
        "WEBHOOK",  # Webhook URLs often embed signing keys.
        "SIGNING",
    },
)

# Setting-name allowlist that should NOT be redacted even though their
# name matches a sensitive fragment. Without this, e.g. ``USE_AUTH``
# would get redacted purely on string match. We keep the allowlist tiny
# and explicit so each entry is auditable.
SENSITIVE_NAME_EXCEPTIONS = frozenset(
    {
        "USE_AUTH",  # Boolean flag, not a credential.
        "AUTH_USER_MODEL",  # Django model path string.
        "AUTHENTICATION_BACKENDS",  # List of class paths.
        "PASSWORD_HASHERS",  # List of hasher class paths.
        "AUTH_PASSWORD_VALIDATORS",  # List of validator config dicts.
    },
)


def is_sensitive_setting(name: str) -> bool:
    """Return True if a Django setting name should have its value redacted.

    Matching is case-insensitive substring against
    :data:`SENSITIVE_NAME_FRAGMENTS`. Names in
    :data:`SENSITIVE_NAME_EXCEPTIONS` are explicitly NOT redacted —
    they happen to match a sensitive fragment but are well-known
    non-credential settings (booleans, class paths, etc.).

    Examples:
        >>> is_sensitive_setting("DJANGO_SECRET_KEY")
        True
        >>> is_sensitive_setting("DATABASE_PASSWORD")
        True
        >>> is_sensitive_setting("OIDC_CLIENT_SECRET")
        True
        >>> is_sensitive_setting("DEBUG")
        False
        >>> is_sensitive_setting("USE_AUTH")
        False  # explicit exception
    """
    if name in SENSITIVE_NAME_EXCEPTIONS:
        return False
    upper = name.upper()
    return any(fragment in upper for fragment in SENSITIVE_NAME_FRAGMENTS)


# Setting-value patterns that indicate a secret-like value even when the
# *name* didn't trigger redaction. Used as a defense-in-depth pass so
# accidentally-named settings (e.g. an operator named a setting ``MY_KEY``
# that we missed) don't leak. We recognise:
#   - 32+ character hex strings (e.g. SHA-256 hashes used as secret keys)
#   - JWT tokens (eyJ... base64)
#   - PEM-formatted private keys
#   - Bearer tokens (Bearer ...)
#   - Connection strings with embedded credentials (postgres://user:pass@...)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"^[A-Fa-f0-9]{32,}$"),  # long hex
    re.compile(r"^eyJ[A-Za-z0-9_-]+\."),  # JWT prefix
    # PEM with or without a key-type prefix (RSA / EC / OPENSSH / etc.).
    # Both ``-----BEGIN PRIVATE KEY-----`` and
    # ``-----BEGIN RSA PRIVATE KEY-----`` match. The
    # ``detect-private-key`` pre-commit hook excludes this file
    # explicitly — see .pre-commit-config.yaml.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"^Bearer [A-Za-z0-9_.-]+", re.IGNORECASE),
    re.compile(r"://[^/:]+:[^/]+@"),  # user:pass@ in URLs
)


def looks_like_secret_value(value: str) -> bool:
    """Return True if a value's shape suggests it carries a credential.

    This is the defense-in-depth fallback for settings we couldn't
    classify by name. Pattern hits are conservative — false positives
    (a non-secret value that happens to look like one) are
    acceptable; false negatives (a real secret slipping through) are
    not.
    """
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


# ─────────────────────────────────────────────────────────────────────
# Text redaction (logs, command output, free-form blobs)
# ─────────────────────────────────────────────────────────────────────

# Substitution patterns for free-form text — applied to support-bundle
# logs (container stdout/stderr, gcloud output) before they're zipped
# into the bundle. Each pattern (regex, replacement) replaces matching
# substrings with a redaction sentinel that preserves enough shape for
# support staff to see WHAT was redacted but never the secret itself.
#
# Order matters: more-specific patterns first. The patterns are kept
# conservative — false positives (redacting a non-secret) are
# acceptable; false negatives (a credential leaking through) are not.
_LOG_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # HTTP header values that carry credentials or trust-boundary
    # data — redact the entire value through end-of-line. Using
    # ``\S+`` here would stop at the first space, leaving the value
    # visible (``Authorization: Bearer <token>`` would match only
    # ``Bearer`` and leave the token behind).
    #
    # The header set covers:
    #   - ``authorization`` — HTTP Bearer / Basic / etc.
    #   - ``cookie`` / ``set-cookie`` — session IDs, CSRF tokens,
    #     remember-me tokens; the whole value is redacted because
    #     individual cookie names can carry sensitive payloads.
    #   - ``x-api-key`` / ``x-auth-token`` — common API auth headers
    #   - ``x-csrftoken`` / ``x-csrf-token`` — Django + JS-framework
    #     CSRF tokens, sensitive for session-fixation reasons
    #   - ``x-x402-payment`` — internal alias used by some legacy
    #     code paths for x402 payment receipts
    #   - ``payment-signature`` — x402 v2 spec header carrying the
    #     buyer's signed payment payload (base64 EIP-712 signature
    #     + payload). Definitely sensitive — the actual money
    #     authorisation lives here.
    #   - ``payment-required`` / ``payment-response`` — x402 v2
    #     server-side payment-protocol headers. ``PAYMENT-REQUIRED``
    #     advertises receiving addresses + price; ``PAYMENT-RESPONSE``
    #     contains settlement transaction hashes. Not "secrets" in
    #     the strict sense, but financial PII worth scrubbing from
    #     a support bundle that may end up on Slack or Linear.
    #   - ``x-validibot-service-identity`` — Cloud Run OIDC identity
    #     token forwarded between the MCP server and the Django API
    #   - ``x-validibot-api-token`` / ``x-validibot-user-sub`` —
    #     end-user identity headers the MCP helper API resolves
    #   - ``x-mcp-service-key`` — local-dev service-to-service auth
    #     between the MCP server and the Django API
    #   - ``proxy-authorization`` — HTTP proxy credentials
    (
        re.compile(
            r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|"
            r"x-api-key|x-auth-token|x-csrf-?token|x-x402-payment|"
            r"payment-signature|payment-required|payment-response|"
            r"x-validibot-service-identity|x-validibot-api-token|"
            r"x-validibot-user-sub|x-mcp-service-key)"
            r"\s*[:=][^\r\n]*",
        ),
        r"\1: [REDACTED]",
    ),
    # ``sessionid=`` / ``csrftoken=`` style cookie *attributes*
    # appearing in URL-encoded form or request-body fragments. The
    # header-level pattern above catches the whole header; this
    # pattern catches the same names when they show up bare in a
    # log line (e.g. ``Set-Cookie: sessionid=abc123``).
    (
        re.compile(
            r"(?i)\b(sessionid|csrftoken|csrf_token|remember_token|jsessionid)"
            r"\s*=\s*([^\s,;]+)",
        ),
        r"\1=[REDACTED]",
    ),
    # ``Bearer <token>`` in any context.
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.-]+"), "Bearer [REDACTED]"),
    # JWTs (three base64-ish segments separated by dots, opening with eyJ).
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "[REDACTED-JWT]",
    ),
    # PEM private-key blocks (multi-line). DOTALL so ``.`` matches newlines.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED-PEM-PRIVATE-KEY]",
    ),
    # URLs with embedded basic-auth credentials (``user:pass@host``).
    # Replace the credential portion only, preserving the URL shape so
    # support can still see which endpoint was being hit.
    (re.compile(r"://([^/:@\s]+):([^@\s]+)@"), r"://[REDACTED]:[REDACTED]@"),
    # Long hex strings (32+ characters) — likely SHA-256ish secret keys
    # or hashes. SHA-256 image digests with the ``sha256:`` prefix are
    # NOT redacted because they're public identifiers, not secrets.
    (
        re.compile(r"(?<!sha256:)\b[A-Fa-f0-9]{32,}\b"),
        "[REDACTED-HEX]",
    ),
    # ``key=value`` and ``KEY: value`` patterns where the key name is
    # itself sensitive (mirrors the settings name-based redaction).
    # The value capture excludes ``[`` so an already-redacted
    # ``[REDACTED-JWT]`` (or any other sentinel) isn't double-
    # processed when it follows a sensitive key name. Earlier
    # patterns (JWT, PEM, etc.) get first crack at the value; this
    # pattern is the catch-all for sensitive-named values that don't
    # match any specific shape.
    (
        re.compile(
            r"(?i)\b((?:[A-Z_]+_)?(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|"
            r"PRIVATE[_-]?KEY|CREDENTIAL|WEBHOOK[_-]?SECRET|SIGNING[_-]?KEY)"
            r"[A-Z_]*)\s*[:=]\s*([^\s,;}\[]+)",
        ),
        r"\1=[REDACTED]",
    ),
)


def redact_text_for_bundle(text: str) -> str:
    """Apply log-level redaction patterns to free-form text.

    Used by the support-bundle recipes to scrub container logs and
    gcloud output before zipping them into the archive. Each pattern
    in :data:`_LOG_REDACTION_PATTERNS` is applied in order — the
    output of one substitution is the input of the next.

    Returns the redacted text. Idempotent: running the function twice
    on the same input produces the same output (the redaction
    sentinels themselves don't match any pattern).

    Performance: log files are typically a few hundred KB; this runs
    in milliseconds. We don't optimise for the multi-GB case because
    container log windows are bounded by the recipe's
    ``--tail=N`` argument.
    """
    for pattern, replacement in _LOG_REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_setting_value(name: str, value: object) -> object:
    """Return ``REDACTED_SENTINEL`` or the original value, per redaction rules.

    Three checks, in order of cheapness:

    1. Name-based: if the setting name indicates it's a credential,
       redact regardless of value type.
    2. Value-shape: if the value's *shape* matches a known secret
       pattern (PEM, JWT, embedded auth in URL), redact.
    3. Otherwise: pass through.

    Non-string values that pass both checks are returned as-is — we
    don't try to recurse into dicts/lists here because the caller
    (the management command) already iterates settings flatly and
    decides what to do with structured values.
    """
    if is_sensitive_setting(name):
        return REDACTED_SENTINEL
    if looks_like_secret_value(value if isinstance(value, str) else ""):
        return REDACTED_SENTINEL
    return value


# ─────────────────────────────────────────────────────────────────────
# Pydantic schemas — the JSON contract
# ─────────────────────────────────────────────────────────────────────


class VersionInfo(BaseModel):
    """Versions captured at bundle time.

    Useful for support to know what the deployment is running before
    digging into specific symptoms. ``validibot_version`` matches the
    same value the backup manifest records, so support staff can
    cross-reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    validibot_version: str = Field(
        description="``settings.VALIDIBOT_VERSION`` or the package's __version__.",
    )
    python_version: str = Field(
        description="Python interpreter version (X.Y.Z).",
    )
    postgres_server_version: str = Field(
        description="``SHOW server_version`` from the live database connection.",
    )
    target: str = Field(
        description="Deployment target (``self_hosted``, ``gcp``, etc.).",
    )
    stage: str | None = Field(
        default=None,
        description="GCP stage (``dev``/``staging``/``prod``); None for self-hosted.",
    )


class MigrationState(BaseModel):
    """Per-app migration head — same shape as backup manifest's compatibility.

    Lets support cross-reference a failing operation against the
    deployment's exact migration state without asking "did you run
    migrate?" three times.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    head: dict[str, str] = Field(
        description=(
            "Map of Django ``app_label`` to the most recent migration name "
            "applied. Empty dict is legal for fresh deployments."
        ),
    )


class RedactedSetting(BaseModel):
    """One Django setting with its value or the redaction sentinel.

    The list of these in a snapshot is what gives support staff
    enough to diagnose configuration problems. Values that pass both
    name-based and value-shape redaction checks are recorded
    verbatim; everything else is replaced with ``[REDACTED]``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: object = Field(
        description=(
            "Either the actual value (for non-sensitive settings) or "
            "``[REDACTED]`` (for sensitive). Type is preserved for "
            "non-redacted values so support tooling can distinguish "
            '``DEBUG=False`` from ``DEBUG="False"``.'
        ),
    )
    redacted: bool = Field(
        description=(
            "True iff the value was replaced with ``[REDACTED]``. "
            "Surfaces the redaction status without making support "
            "staff parse the value."
        ),
    )


class ValidatorBackendSummary(BaseModel):
    """One validator backend known to this Validibot deployment.

    Captured from the ``Validator`` model. We record name + slug +
    digest pinning policy because those are the things support
    typically needs to ask about; we do NOT include image digests
    here because they're more naturally captured by ``docker image
    ls`` host-side.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str
    validation_type: str
    is_system: bool
    image: str | None = Field(
        default=None,
        description="Image reference (tag or digest), or None for built-in validators.",
    )


class OutboundCallStatus(BaseModel):
    """Whether each known outbound-call channel is enabled.

    Mirrors the ADR section 10 list. Support tickets often start
    with "is telemetry on?" — this captures the answer without
    needing follow-up.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sentry_enabled: bool
    posthog_enabled: bool
    email_configured: bool
    runtime_license_check_enabled: bool


class SupportBundleAppSnapshot(BaseModel):
    """The Django-side portion of a support bundle.

    Produced by the ``collect_support_bundle`` management command,
    serialized as JSON, and embedded in the zip the recipe assembles.
    The recipe ALSO captures host-side artefacts (Compose state,
    logs, disk usage); this schema describes only what Django saw.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["validibot.support-bundle.v1"] = Field(
        default=SUPPORT_BUNDLE_SCHEMA_VERSION,
        description="Pinned schema version. Consumers reject unknown values.",
    )
    captured_at: str = Field(
        description="ISO 8601 UTC timestamp when the snapshot was produced.",
    )
    versions: VersionInfo
    migrations: MigrationState
    settings: list[RedactedSetting] = Field(
        description=(
            "Every Django setting we considered stable enough to surface, "
            "with sensitive values replaced by the redaction sentinel. "
            "Operators inspecting this list can verify what's redacted "
            "and what isn't."
        ),
    )
    outbound_calls: OutboundCallStatus
    validators: list[ValidatorBackendSummary] = Field(
        default_factory=list,
        description=(
            "Validator backends known to the Django deployment (from the "
            "``Validator`` model). Empty for fresh deployments. Host-side "
            "validator IMAGES are captured separately by the recipe."
        ),
    )
    doctor: dict | None = Field(
        default=None,
        description=(
            "The most recent doctor JSON output (``validibot.doctor.v1`` "
            "schema). Embedded as-is so support tooling that already "
            "understands the doctor schema can reuse its parsers."
        ),
    )


__all__ = [
    "REDACTED_SENTINEL",
    "SENSITIVE_NAME_EXCEPTIONS",
    "SENSITIVE_NAME_FRAGMENTS",
    "SUPPORT_BUNDLE_SCHEMA_VERSION",
    "MigrationState",
    "OutboundCallStatus",
    "RedactedSetting",
    "SupportBundleAppSnapshot",
    "ValidatorBackendSummary",
    "VersionInfo",
    "is_sensitive_setting",
    "looks_like_secret_value",
    "redact_setting_value",
    "redact_text_for_bundle",
]
