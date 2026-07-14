"""Issue and verify hashed Validibot API keys.

Plaintext API keys are bearer credentials. This module is the only place
that creates or validates them so the rest of the application can avoid
handling storage details or accidentally re-displaying a saved secret.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from validibot.users.models import User
from validibot.users.models import ValidibotAPIKey

KEY_PREFIX = "vbk"
FORMAT_VERSION = 1
PUBLIC_ID_BYTES = 16
SECRET_BYTES = 32
MAX_API_KEY_LENGTH = 160
DEFAULT_LABEL = "Personal API key"

API_KEY_PATTERN = re.compile(
    r"^vbk_(?P<format_version>\d+)_(?P<public_id>[a-f0-9]{32})_"
    r"(?P<secret>[a-f0-9]{64})$",
)


@dataclass(frozen=True)
class ParsedAPIKey:
    """Parsed parts of a submitted API key."""

    format_version: int
    public_id: str
    secret: str


@dataclass(frozen=True)
class IssuedAPIKey:
    """Newly issued API key plus its one-time plaintext value."""

    api_key: ValidibotAPIKey
    full_key: str


def parse_api_key(raw_key: str) -> ParsedAPIKey | None:
    """Parse a Validibot API key without verifying its digest."""

    candidate = (raw_key or "").strip()
    if not candidate or len(candidate) > MAX_API_KEY_LENGTH:
        return None

    match = API_KEY_PATTERN.match(candidate)
    if match is None:
        return None

    try:
        format_version = int(match.group("format_version"))
    except ValueError:
        return None

    if format_version != FORMAT_VERSION:
        return None

    return ParsedAPIKey(
        format_version=format_version,
        public_id=match.group("public_id"),
        secret=match.group("secret"),
    )


def get_active_api_key(user: User) -> ValidibotAPIKey | None:
    """Return the user's newest currently usable Validibot API key."""

    now = timezone.now()
    return (
        ValidibotAPIKey.objects.filter(user=user, revoked_at__isnull=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .order_by("-created")
        .first()
    )


def issue_api_key(
    *,
    user: User,
    label: str = DEFAULT_LABEL,
    rotated_from: ValidibotAPIKey | None = None,
) -> IssuedAPIKey:
    """Create a new hashed API key and return its one-time plaintext value."""

    public_id = secrets.token_hex(PUBLIC_ID_BYTES)
    secret = secrets.token_hex(SECRET_BYTES)
    digest_version = _current_digest_version()
    secret_digest = _digest_secret(
        format_version=FORMAT_VERSION,
        public_id=public_id,
        secret=secret,
        digest_version=digest_version,
    )
    expires_at = _default_expiry()

    api_key = ValidibotAPIKey.objects.create(
        user=user,
        public_id=public_id,
        label=label,
        format_version=FORMAT_VERSION,
        digest_version=digest_version,
        secret_digest=secret_digest,
        expires_at=expires_at,
        rotated_from=rotated_from,
    )
    return IssuedAPIKey(
        api_key=api_key,
        full_key=f"{KEY_PREFIX}_{FORMAT_VERSION}_{public_id}_{secret}",
    )


def rotate_user_api_key(
    *,
    user: User,
    label: str = DEFAULT_LABEL,
) -> IssuedAPIKey:
    """Revoke the user's active personal keys and issue a replacement."""

    now = timezone.now()
    with transaction.atomic():
        active_keys = ValidibotAPIKey.objects.select_for_update().filter(
            user=user,
            revoked_at__isnull=True,
        )
        previous = active_keys.order_by("-created").first()
        active_keys.update(revoked_at=now, modified=now)
        return issue_api_key(user=user, label=label, rotated_from=previous)


def verify_api_key(raw_key: str) -> ValidibotAPIKey | None:
    """Return the matching usable API key, or ``None`` on any failure."""

    parsed = parse_api_key(raw_key)
    if parsed is None:
        return None

    api_key = (
        ValidibotAPIKey.objects.select_related("user")
        .filter(
            public_id=parsed.public_id,
            format_version=parsed.format_version,
        )
        .first()
    )
    if api_key is None:
        _compare_miss(parsed)
        return None

    submitted_digest = _digest_secret(
        format_version=parsed.format_version,
        public_id=parsed.public_id,
        secret=parsed.secret,
        digest_version=api_key.digest_version,
    )
    if not hmac.compare_digest(submitted_digest, api_key.secret_digest):
        return None

    if not api_key.is_usable:
        return None

    _touch_last_used(api_key)
    return api_key


def _digest_secret(
    *,
    format_version: int,
    public_id: str,
    secret: str,
    digest_version: int,
) -> str:
    """Return the storage digest for an API-key secret."""

    message = "\0".join(
        [
            "validibot-api-key",
            f"format={format_version}",
            f"digest={digest_version}",
            f"public={public_id}",
            f"secret={secret}",
        ],
    ).encode()
    return hmac.new(_digest_key(), message, hashlib.sha256).hexdigest()


def _digest_key() -> bytes:
    """Return the HMAC key used for API-key digests."""

    raw_key = getattr(settings, "API_KEY_DIGEST_KEY", "") or settings.SECRET_KEY
    if not raw_key:
        raise ImproperlyConfigured("API_KEY_DIGEST_KEY or SECRET_KEY is required.")
    if isinstance(raw_key, bytes):
        return raw_key
    return str(raw_key).encode()


def _current_digest_version() -> int:
    """Return the configured digest-key version."""

    version = int(getattr(settings, "API_KEY_DIGEST_VERSION", 1))
    if version < 1:
        raise ImproperlyConfigured("API_KEY_DIGEST_VERSION must be positive.")
    return version


def _default_expiry():
    """Return the default expiry timestamp for newly issued API keys."""

    days = int(getattr(settings, "API_KEY_DEFAULT_EXPIRY_DAYS", 365))
    if days <= 0:
        return None
    return timezone.now() + timedelta(days=days)


def _touch_last_used(api_key: ValidibotAPIKey) -> None:
    """Update ``last_used_at`` at most once per configured interval."""

    interval_seconds = int(
        getattr(settings, "API_KEY_LAST_USED_UPDATE_INTERVAL_SECONDS", 3600),
    )
    now = timezone.now()
    if (
        interval_seconds > 0
        and api_key.last_used_at is not None
        and api_key.last_used_at > now - timedelta(seconds=interval_seconds)
    ):
        return

    ValidibotAPIKey.objects.filter(pk=api_key.pk).update(
        last_used_at=now,
        modified=now,
    )
    api_key.last_used_at = now


def _compare_miss(parsed: ParsedAPIKey) -> None:
    """Spend comparable HMAC/compare work when the public id misses."""

    submitted_digest = _digest_secret(
        format_version=parsed.format_version,
        public_id=parsed.public_id,
        secret=parsed.secret,
        digest_version=_current_digest_version(),
    )
    hmac.compare_digest(submitted_digest, "0" * 64)
