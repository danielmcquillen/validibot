"""Per-request audit context, carried via ``contextvars``.

``AuditContextMiddleware`` sets a context dict at the start of every
request; signal handlers (login, password change, token create/revoke)
read it to know *who* took the action and over which request.

Why ``contextvars`` rather than ``threading.local``:

Django increasingly runs under ASGI where a single request can ``await``
and resume on a different thread. A ``threading.local`` would silently
lose the audit context at the first ``await`` point, producing audit
entries with ``actor=None`` even though the request had an authenticated
user. ``contextvars.ContextVar`` follows the async task instead of the
thread, so the context survives ``await`` boundaries. It also degrades
gracefully in sync-only code paths (Celery workers, management
commands), where it simply returns the default "no-context" value.

The helpers here return *defaults* — never raise — so code that reads
context outside a request (a Celery task, a manage.py shell) gets a
well-formed empty context rather than an exception. Audit entries
written from such contexts end up with ``actor=None``, which is the
correct outcome.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from validibot.audit.services import ActorSpec


@dataclass(frozen=True)
class AuditRequestContext:
    """Everything the audit capture layer needs from the active request.

    Frozen because the middleware is the only writer; signal handlers
    only read it. If a handler needs a mutation (e.g. to override the
    actor because the signal has better information), it should build
    a new ``ActorSpec`` inline rather than editing shared state.
    """

    actor: ActorSpec
    request_id: str

    @classmethod
    def empty(cls) -> AuditRequestContext:
        """Return the fallback context for out-of-request code paths.

        An empty ``ActorSpec`` resolves to an actor row with no user /
        email / IP — which is the correct representation for
        system-originated events (Celery tasks, management commands,
        startup hooks). ``request_id`` stays blank rather than being a
        synthetic UUID so it's obvious the entry did not originate from
        an HTTP request.
        """

        return cls(actor=ActorSpec(), request_id="")


# The single ContextVar backing the whole audit-context surface.
# ``default`` is a sentinel rather than ``None`` so the accessors can
# return a consistent ``AuditRequestContext`` instance every time
# without allocating a fresh empty one on each read.
_DEFAULT_CONTEXT = AuditRequestContext.empty()
_CURRENT_CONTEXT: ContextVar[AuditRequestContext] = ContextVar(
    "validibot_audit_request_context",
    default=_DEFAULT_CONTEXT,
)


def get_current_context() -> AuditRequestContext:
    """Return the active audit context or the empty fallback.

    Called by signal handlers and any ad-hoc code that wants to
    attribute an audit write to the current request. Outside a request
    (Celery, management commands) this returns
    ``AuditRequestContext.empty()``.
    """

    return _CURRENT_CONTEXT.get()


def get_current_actor_spec() -> ActorSpec:
    """Return the actor half of the current context.

    Shortcut for the most common access pattern — signal handlers
    usually only care about the actor, not the request id.
    """

    return get_current_context().actor


def get_current_request_id() -> str:
    """Return the request id, or empty string if called out-of-request."""

    return get_current_context().request_id


def set_current_context(context: AuditRequestContext) -> Any:
    """Install a new context and return the token for resetting it.

    Middleware calls this on the way in and passes the returned token to
    :func:`reset_current_context` on the way out. Returning the token
    rather than a plain boolean keeps the ``contextvars`` API honest —
    the token identifies *which* set-call to undo, which matters when
    middlewares are nested or the reset path runs in a different
    handler than the set path (async views).
    """

    return _CURRENT_CONTEXT.set(context)


def reset_current_context(token: Any) -> None:
    """Restore the audit context to whatever it was before ``token``.

    Always safe to call with a valid token — if the token came from the
    very first set, this restores the default empty context.
    """

    _CURRENT_CONTEXT.reset(token)


def new_request_id() -> str:
    """Build a fresh opaque request id.

    UUID4, stringified, prefixed with ``req_`` so grep across logs
    can quickly distinguish request ids from other UUIDs. Exported
    because test code occasionally needs to construct a deterministic
    request id for assertions.
    """

    return f"req_{uuid.uuid4().hex}"
