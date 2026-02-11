"""
Idempotency key support for DRF views.

This module provides a mixin that implements the Stripe-style idempotency key
pattern for API endpoints. Clients can send an Idempotency-Key header with a
unique identifier, and the server will return cached responses for duplicate
requests instead of processing them again.

Usage:
    class MyViewSet(IdempotencyMixin, viewsets.ModelViewSet):
        idempotent_actions = ["create", "start_validation"]

See ADR-2025-11-27 for design rationale and implementation details.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from functools import wraps
from http import HTTPStatus
from typing import Any

from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response

from validibot.core.models import IDEMPOTENCY_KEY_TTL_HOURS
from validibot.core.models import IdempotencyKey
from validibot.core.models import IdempotencyKeyStatus

logger = logging.getLogger(__name__)


# Header name (Django normalizes to HTTP_IDEMPOTENCY_KEY)
IDEMPOTENCY_HEADER = "HTTP_IDEMPOTENCY_KEY"
MAX_KEY_LENGTH = 255


class IdempotencyError:
    """Error codes for idempotency-related failures."""

    KEY_TOO_LONG = "idempotency_key_too_long"
    KEY_REUSED = "idempotency_key_reused"
    KEY_IN_PROGRESS = "idempotency_key_in_progress"


def compute_request_hash(request) -> str:
    """
    Compute a SHA256 hash of the request body for fingerprinting.

    This is used to detect when a client reuses an idempotency key
    with a different request payload (which is an error).
    """
    body = request.body or b""
    return hashlib.sha256(body).hexdigest()


def get_client_ip(request) -> str | None:
    """Extract client IP from request for debugging."""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def idempotent(func: Callable) -> Callable:
    """
    Decorator that adds idempotency key handling to a DRF view action.

    This decorator wraps a view action to:
    1. Check for an Idempotency-Key header
    2. If found, check if we've seen this key before
    3. If seen and completed, return the cached response
    4. If seen and in progress, return 409 Conflict
    5. If new, process the request and cache the response

    Usage:
        @action(detail=True, methods=["post"])
        @idempotent
        def start_validation(self, request, pk=None):
            ...
    """

    @wraps(func)
    def wrapper(self, request, *args, **kwargs):
        # Extract idempotency key from header
        idempotency_key = request.META.get(IDEMPOTENCY_HEADER)

        # If no key provided, process normally (no idempotency)
        if not idempotency_key:
            return func(self, request, *args, **kwargs)

        # Validate key length
        if len(idempotency_key) > MAX_KEY_LENGTH:
            return Response(
                {
                    "detail": (
                        f"Idempotency key exceeds maximum length "
                        f"of {MAX_KEY_LENGTH} characters."
                    ),
                    "code": IdempotencyError.KEY_TOO_LONG,
                },
                status=HTTPStatus.BAD_REQUEST,
            )

        # Determine organization and endpoint
        org = _get_org_from_request(self, request)
        if org is None:
            # Can't enforce idempotency without org scope
            return func(self, request, *args, **kwargs)

        endpoint = _get_endpoint_name(self)
        request_hash = compute_request_hash(request)

        # Try to find existing key or create a new one
        result = _process_idempotency_key(
            org=org,
            key=idempotency_key,
            endpoint=endpoint,
            request_hash=request_hash,
            request=request,
        )

        if result["action"] == "replay":
            # Return cached response with replay indicator
            response = Response(
                result["key_record"].response_body,
                status=result["key_record"].response_status,
            )
            response["Idempotent-Replayed"] = "true"
            response["Original-Request-Id"] = str(result["key_record"].id)
            return response

        if result["action"] == "conflict":
            return Response(
                {
                    "detail": (
                        "A request with this idempotency key "
                        "is currently being processed."
                    ),
                    "code": IdempotencyError.KEY_IN_PROGRESS,
                },
                status=HTTPStatus.CONFLICT,
            )

        if result["action"] == "hash_mismatch":
            return Response(
                {
                    "detail": (
                        "Idempotency key has already been used "
                        "with a different request body."
                    ),
                    "code": IdempotencyError.KEY_REUSED,
                },
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        # Process without idempotency (edge case from race condition)
        if result["action"] == "process_without_idempotency":
            return func(self, request, *args, **kwargs)

        # Process new request
        key_record = result["key_record"]
        try:
            response = func(self, request, *args, **kwargs)
        except Exception:
            # On error, try to delete the key record so client can retry.
            # If we're in a broken transaction, the delete will fail too,
            # but that's okay - the key record will be unusable anyway.
            try:
                key_record.delete()
            except Exception:
                logger.debug("Failed to delete key record after error")
            raise
        else:
            # Cache the response
            _complete_idempotency_key(
                key_record=key_record,
                response=response,
                validation_run=_extract_validation_run(response),
            )

            return response

    return wrapper


def _get_org_from_request(view, request):
    """Extract organization from request or view context."""
    # For workflow actions, we can get org from the workflow object
    if hasattr(view, "get_object"):
        try:
            obj = view.get_object()
            if hasattr(obj, "org"):
                return obj.org
        except Exception:
            logger.debug("Failed to get org from view object")

    # Fall back to user's current org
    user = request.user
    if hasattr(user, "current_org"):
        return user.current_org

    return None


def _get_endpoint_name(view) -> str:
    """Generate endpoint identifier from view class and action."""
    class_name = view.__class__.__name__
    action = getattr(view, "action", "unknown")
    return f"{class_name}.{action}"


def _process_idempotency_key(
    org,
    key: str,
    endpoint: str,
    request_hash: str,
    request,
) -> dict[str, Any]:
    """
    Process an idempotency key, returning action to take.

    Returns dict with:
    - action: "replay" | "conflict" | "hash_mismatch" | "process"
    - key_record: The IdempotencyKey instance (if applicable)
    """
    now = timezone.now()

    # First, try to find an existing non-expired key
    existing = IdempotencyKey.objects.filter(
        org=org,
        key=key,
        endpoint=endpoint,
        expires_at__gt=now,
    ).first()

    if existing:
        # Check if request hash matches
        if existing.request_hash != request_hash:
            return {"action": "hash_mismatch", "key_record": existing}

        # Check if still processing
        if existing.status == IdempotencyKeyStatus.PROCESSING:
            return {"action": "conflict", "key_record": existing}

        # Completed - return cached response
        return {"action": "replay", "key_record": existing}

    # Delete any expired keys with same (org, key, endpoint) before creating new one
    IdempotencyKey.objects.filter(
        org=org,
        key=key,
        endpoint=endpoint,
        expires_at__lte=now,
    ).delete()

    # Create a new key
    try:
        with transaction.atomic():
            key_record = IdempotencyKey.objects.create(
                org=org,
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                status=IdempotencyKeyStatus.PROCESSING,
                expires_at=now + timezone.timedelta(hours=IDEMPOTENCY_KEY_TTL_HOURS),
                request_ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:500],
            )
            return {"action": "process", "key_record": key_record}
    except IntegrityError:
        # Race condition - another request created the key first
        # Re-fetch and handle appropriately (only non-expired keys)
        existing = IdempotencyKey.objects.filter(
            org=org,
            key=key,
            endpoint=endpoint,
            expires_at__gt=now,
        ).first()

        if existing:
            if existing.request_hash != request_hash:
                return {"action": "hash_mismatch", "key_record": existing}
            if existing.status == IdempotencyKeyStatus.PROCESSING:
                return {"action": "conflict", "key_record": existing}
            return {"action": "replay", "key_record": existing}

        # Key doesn't exist or is expired - something unusual happened
        # Process without idempotency
        return {"action": "process_without_idempotency", "key_record": None}


def _serialize_response_data(data: Any) -> Any:
    """
    Convert response data to JSON-serializable format.

    Handles UUIDs, Django lazy strings, dates, and other types
    that aren't directly JSON-serializable.
    """
    import uuid as uuid_module

    from django.utils.functional import Promise

    if isinstance(data, dict):
        return {k: _serialize_response_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_serialize_response_data(item) for item in data]
    if isinstance(data, uuid_module.UUID):
        return str(data)
    if isinstance(data, Promise):
        # Django lazy translation strings
        return str(data)
    if hasattr(data, "isoformat"):
        return data.isoformat()
    return data


def _complete_idempotency_key(
    key_record: IdempotencyKey,
    response: Response,
    validation_run=None,
):
    """Update idempotency key with completed response."""
    key_record.status = IdempotencyKeyStatus.COMPLETED
    key_record.response_status = response.status_code

    # Serialize response data for storage, handling UUIDs and other types
    try:
        key_record.response_body = _serialize_response_data(response.data)
    except Exception:
        # Fallback: try rendering to JSON and parsing back
        try:
            key_record.response_body = json.loads(response.rendered_content)
        except Exception:
            key_record.response_body = {"_serialization_error": True}

    if validation_run:
        key_record.validation_run = validation_run

    key_record.save()


def _extract_validation_run(response: Response):
    """Extract ValidationRun from response if present."""
    # Check if response data contains a run ID
    if hasattr(response, "data") and isinstance(response.data, dict):
        run_id = response.data.get("id") or response.data.get("run_id")
        if run_id:
            from validibot.validations.models import ValidationRun

            try:
                return ValidationRun.objects.get(pk=run_id)
            except (ValidationRun.DoesNotExist, ValueError):
                pass
    return None


class IdempotencyMixin:
    """
    Mixin for DRF views that provides idempotency key support.

    This mixin is provided for views that want to customize idempotency
    behavior. For most cases, use the @idempotent decorator directly.

    Usage:
        class MyViewSet(IdempotencyMixin, viewsets.ModelViewSet):
            idempotent_actions = ["create", "start_validation"]
    """

    idempotent_actions: list[str] = []
    idempotency_key_header = IDEMPOTENCY_HEADER
    idempotency_ttl_hours = IDEMPOTENCY_KEY_TTL_HOURS

    def get_idempotency_key(self, request) -> str | None:
        """Extract idempotency key from request headers."""
        return request.META.get(self.idempotency_key_header)

    def get_idempotency_endpoint(self) -> str:
        """Generate endpoint identifier for this view action."""
        return _get_endpoint_name(self)
