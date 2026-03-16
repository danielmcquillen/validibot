"""
Cache-based rate limiting for Django views.

Provides a simple ``@rate_limit`` decorator that uses Django's cache
framework to throttle requests by IP address. This avoids adding an
external dependency (like django-ratelimit) while providing effective
protection against automated abuse.

Usage::

    from validibot.core.ratelimit import rate_limit

    class MyView(View):
        @method_decorator(rate_limit("10/m"))
        def post(self, request):
            ...

Rate format: ``"<count>/<period>"`` where period is one of:
    - ``s`` — seconds
    - ``m`` — minutes
    - ``h`` — hours

The decorator extracts the client IP from the request (respecting
X-Forwarded-For when behind a reverse proxy) and returns a 429
response when the rate is exceeded.

Limitations:
    - Uses Django's default cache (LocMemCache unless configured).
      With LocMemCache, rate limits are per-process — each Cloud Run
      instance enforces independently. This is acceptable for most
      abuse scenarios (attackers typically hit the same instance via
      keep-alive connections). For stricter enforcement, configure
      a shared cache backend (Redis/Memcached).
    - No user-level scoping — only IP-based. Use allauth's built-in
      rate limits for user/key-scoped throttling.
"""

from __future__ import annotations

import functools
import logging
import re
from http import HTTPStatus

from django.core.cache import cache
from django.http import HttpRequest
from django.http import HttpResponse

logger = logging.getLogger(__name__)

# Maps period suffix to seconds
_PERIOD_MAP = {
    "s": 1,
    "m": 60,
    "h": 3600,
}

_RATE_PATTERN = re.compile(r"^(\d+)/([smh])$")


def _parse_rate(rate_str: str) -> tuple[int, int]:
    """Parse a rate string like '10/m' into (max_requests, window_seconds)."""
    match = _RATE_PATTERN.match(rate_str)
    if not match:
        msg = (
            f"Invalid rate format: '{rate_str}'. "
            "Expected format: '<count>/<period>' where period is s, m, or h."
        )
        raise ValueError(msg)
    count = int(match.group(1))
    period = _PERIOD_MAP[match.group(2)]
    return count, period


def _get_client_ip(request: HttpRequest) -> str:
    """Extract client IP, respecting X-Forwarded-For behind proxies.

    Cloud Run sets X-Forwarded-For automatically. We take the first
    (leftmost) IP, which is the original client. Subsequent IPs are
    added by each proxy in the chain.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def rate_limit(rate_str: str, key_prefix: str = "rl"):
    """Decorator that rate-limits a view by client IP address.

    Args:
        rate_str: Rate in ``"count/period"`` format (e.g., ``"10/m"``).
        key_prefix: Cache key prefix. Use a unique prefix per endpoint
            to prevent rate limit counters from colliding across
            different views.

    Returns:
        429 Too Many Requests when the rate is exceeded, with a
        Retry-After header indicating when the client can retry.
    """
    max_requests, window_seconds = _parse_rate(rate_str)

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request_or_self, *args, **kwargs):
            # Support both function views and class-based view methods.
            # For CBV methods, the first arg is `self` and the request
            # is the second arg.
            if isinstance(request_or_self, HttpRequest):
                request = request_or_self
            else:
                request = args[0] if args else None

            if request is None:
                return view_func(request_or_self, *args, **kwargs)

            ip = _get_client_ip(request)
            cache_key = f"{key_prefix}:{view_func.__qualname__}:{ip}"

            # Increment the counter for this IP in this window.
            # cache.get_or_set + cache.incr is atomic in most backends.
            current = cache.get(cache_key)
            if current is None:
                cache.set(cache_key, 1, window_seconds)
                current = 1
            else:
                try:
                    current = cache.incr(cache_key)
                except ValueError:
                    # Key expired between get and incr — reset
                    cache.set(cache_key, 1, window_seconds)
                    current = 1

            if current > max_requests:
                logger.warning(
                    "Rate limit exceeded: %s from %s (%d/%d in %ds window)",
                    view_func.__qualname__,
                    ip,
                    current,
                    max_requests,
                    window_seconds,
                )
                response = HttpResponse(
                    "Too many requests. Please try again later.",
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                    content_type="text/plain",
                )
                response["Retry-After"] = str(window_seconds)
                return response

            return view_func(request_or_self, *args, **kwargs)

        return wrapper

    return decorator
