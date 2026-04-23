"""Opaque MCP reference helpers used by the tool layer.

The MCP UX should not require users to manually manage organization slugs,
wallet addresses, or raw run IDs. These helpers keep the user-facing contract
stable while still letting the MCP server route each request to the existing
backend endpoints.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

WORKFLOW_REF_PREFIX = "wf_"
RUN_REF_PREFIX = "run_"
RUN_REF_MEMBER_KIND = "member"
RUN_REF_X402_KIND = "x402"

# TODO: Move this ref codec into validibot-shared so the FastMCP package and
# the Django helper layer stop maintaining parallel base64url/JSON helpers.


@dataclass(frozen=True)
class ResolvedRunRef:
    """Describe the backend routing data encoded inside an MCP ``run_ref``."""

    auth_kind: str
    run_id: str
    org_slug: str | None = None
    wallet_address: str | None = None


def build_workflow_ref(*, org_slug: str, workflow_slug: str) -> str:
    """Return the stable workflow reference for a workflow family."""

    payload = {
        "org": org_slug,
        "workflow": workflow_slug,
    }
    encoded = _encode_payload(payload)
    return f"{WORKFLOW_REF_PREFIX}{encoded}"


def parse_workflow_ref(workflow_ref: str) -> tuple[str, str]:
    """Decode a workflow reference back to ``(org_slug, workflow_slug)``."""

    if not workflow_ref.startswith(WORKFLOW_REF_PREFIX):
        msg = "workflow_ref must start with 'wf_'."
        raise ValueError(msg)
    payload = _decode_payload(workflow_ref.removeprefix(WORKFLOW_REF_PREFIX))
    org_slug = str(payload.get("org", "")).strip()
    workflow_slug = str(payload.get("workflow", "")).strip()
    if not org_slug or not workflow_slug:
        msg = "workflow_ref is missing org or workflow information."
        raise ValueError(msg)
    return org_slug, workflow_slug


def build_member_run_ref(*, org_slug: str, run_id: str) -> str:
    """Return a run reference for an authenticated member-access run."""

    payload = {
        "kind": RUN_REF_MEMBER_KIND,
        "org": org_slug,
        "run_id": run_id,
    }
    return f"{RUN_REF_PREFIX}{_encode_payload(payload)}"


def build_x402_run_ref(*, run_id: str, wallet_address: str) -> str:
    """Return a run reference for an anonymous x402-backed run."""

    payload = {
        "kind": RUN_REF_X402_KIND,
        "run_id": run_id,
        "wallet": wallet_address,
    }
    return f"{RUN_REF_PREFIX}{_encode_payload(payload)}"


def parse_run_ref(run_ref: str) -> ResolvedRunRef:
    """Decode a ``run_ref`` into the backend routing data it represents."""

    if not run_ref.startswith(RUN_REF_PREFIX):
        msg = "run_ref must start with 'run_'."
        raise ValueError(msg)

    payload = _decode_payload(run_ref.removeprefix(RUN_REF_PREFIX))
    auth_kind = str(payload.get("kind", "")).strip()
    run_id = str(payload.get("run_id", "")).strip()
    if not auth_kind or not run_id:
        msg = "run_ref is missing kind or run_id."
        raise ValueError(msg)

    if auth_kind == RUN_REF_MEMBER_KIND:
        org_slug = str(payload.get("org", "")).strip()
        if not org_slug:
            msg = "Member run_ref is missing org."
            raise ValueError(msg)
        return ResolvedRunRef(
            auth_kind=auth_kind,
            run_id=run_id,
            org_slug=org_slug,
        )

    if auth_kind == RUN_REF_X402_KIND:
        wallet_address = str(payload.get("wallet", "")).strip()
        if not wallet_address:
            msg = "x402 run_ref is missing wallet."
            raise ValueError(msg)
        return ResolvedRunRef(
            auth_kind=auth_kind,
            run_id=run_id,
            wallet_address=wallet_address,
        )

    msg = f"Unsupported run_ref kind '{auth_kind}'."
    raise ValueError(msg)


def _encode_payload(payload: dict[str, str]) -> str:
    """Encode a small JSON payload as URL-safe base64 without padding."""

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_payload(encoded_payload: str) -> dict[str, object]:
    """Decode a URL-safe base64 JSON payload used in MCP references."""

    try:
        padding = "=" * (-len(encoded_payload) % 4)
        raw = base64.urlsafe_b64decode(f"{encoded_payload}{padding}".encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        msg = "Reference payload is not valid base64url JSON."
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "Reference payload must decode to a JSON object."
        raise TypeError(msg)
    return payload
