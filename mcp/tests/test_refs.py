"""Tests for the opaque MCP reference codec.

The ref system encodes routing information (org, workflow slug, run ID,
wallet address) into opaque base64url handles so that MCP agents never
need to know or manage internal identifiers directly.

These tests verify:

* **Round-trip integrity** -- build then parse returns the original data.
* **Prefix enforcement** -- refs without the correct prefix are rejected.
* **Missing fields** -- partially populated payloads produce clear errors.
* **Malformed payloads** -- invalid base64, non-JSON, non-dict payloads
  all raise ValueError with a descriptive message.
* **Deterministic encoding** -- the same inputs always produce the same
  ref string (important for caching and idempotency checks).
"""

from __future__ import annotations

import pytest

from validibot_mcp.refs import (
    RUN_REF_MEMBER_KIND,
    RUN_REF_X402_KIND,
    build_member_run_ref,
    build_workflow_ref,
    build_x402_run_ref,
    parse_run_ref,
    parse_workflow_ref,
)

# ── Workflow ref round-trips ──────────────────────────────────────────


class TestWorkflowRefRoundTrip:
    """Build a workflow ref and parse it back to verify the codec."""

    def test_round_trip(self):
        """build_workflow_ref → parse_workflow_ref should return the
        original org and workflow slugs."""
        ref = build_workflow_ref(org_slug="acme-corp", workflow_slug="energy-check")
        org, wf = parse_workflow_ref(ref)
        assert org == "acme-corp"
        assert wf == "energy-check"

    def test_starts_with_prefix(self):
        """Workflow refs must start with 'wf_' for identification."""
        ref = build_workflow_ref(org_slug="test", workflow_slug="check")
        assert ref.startswith("wf_")

    def test_deterministic(self):
        """The same inputs must always produce the same ref string.

        This is important because refs may be stored or compared for
        equality. Non-deterministic encoding would break caching.
        """
        ref1 = build_workflow_ref(org_slug="acme", workflow_slug="check")
        ref2 = build_workflow_ref(org_slug="acme", workflow_slug="check")
        assert ref1 == ref2

    def test_different_inputs_produce_different_refs(self):
        """Two different workflows must produce different refs."""
        ref1 = build_workflow_ref(org_slug="acme", workflow_slug="check-a")
        ref2 = build_workflow_ref(org_slug="acme", workflow_slug="check-b")
        assert ref1 != ref2


# ── Workflow ref error handling ───────────────────────────────────────


class TestWorkflowRefErrors:
    """Verify that malformed workflow refs produce clear errors."""

    def test_missing_prefix_raises(self):
        """A ref without the 'wf_' prefix must be rejected."""
        with pytest.raises(ValueError, match="must start with"):
            parse_workflow_ref("not-a-valid-ref")

    def test_invalid_base64_raises(self):
        """Garbage base64 after the prefix must raise ValueError."""
        with pytest.raises(ValueError, match="not valid base64url"):
            parse_workflow_ref("wf_!!!invalid!!!")

    def test_missing_org_raises(self):
        """A ref that decodes but has no org field must be rejected."""
        import base64
        import json

        payload = json.dumps({"workflow": "check"}).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(ValueError, match="missing org"):
            parse_workflow_ref(f"wf_{encoded}")

    def test_missing_workflow_raises(self):
        """A ref that decodes but has no workflow field must be rejected."""
        import base64
        import json

        payload = json.dumps({"org": "acme"}).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(ValueError, match="missing org or workflow"):
            parse_workflow_ref(f"wf_{encoded}")

    def test_empty_string_raises(self):
        """An empty string must be rejected."""
        with pytest.raises(ValueError, match="must start with"):
            parse_workflow_ref("")


# ── Member run ref round-trips ────────────────────────────────────────


class TestMemberRunRefRoundTrip:
    """Build a member run ref and parse it back."""

    def test_round_trip(self):
        """build_member_run_ref → parse_run_ref should return the
        original org, run_id, and member auth kind."""
        ref = build_member_run_ref(
            org_slug="acme",
            run_id="550e8400-e29b-41d4-a716-446655440000",
        )
        resolved = parse_run_ref(ref)
        assert resolved.auth_kind == RUN_REF_MEMBER_KIND
        assert resolved.org_slug == "acme"
        assert resolved.run_id == "550e8400-e29b-41d4-a716-446655440000"
        assert resolved.wallet_address is None

    def test_starts_with_prefix(self):
        """Run refs must start with 'run_'."""
        ref = build_member_run_ref(org_slug="acme", run_id="abc-123")
        assert ref.startswith("run_")


# ── x402 run ref round-trips ─────────────────────────────────────────


class TestX402RunRefRoundTrip:
    """Build an x402 run ref and parse it back."""

    def test_round_trip(self):
        """build_x402_run_ref → parse_run_ref should return the
        original run_id, wallet, and x402 auth kind."""
        ref = build_x402_run_ref(
            run_id="550e8400-e29b-41d4-a716-446655440000",
            wallet_address="0x742d35Cc6634C0532925a3b844Bc96e7d3d6b6a5",
        )
        resolved = parse_run_ref(ref)
        assert resolved.auth_kind == RUN_REF_X402_KIND
        assert resolved.run_id == "550e8400-e29b-41d4-a716-446655440000"
        assert resolved.wallet_address == "0x742d35Cc6634C0532925a3b844Bc96e7d3d6b6a5"
        assert resolved.org_slug is None


# ── Run ref error handling ────────────────────────────────────────────


class TestRunRefErrors:
    """Verify that malformed run refs produce clear errors."""

    def test_missing_prefix_raises(self):
        """A ref without the 'run_' prefix must be rejected."""
        with pytest.raises(ValueError, match="must start with"):
            parse_run_ref("bad_prefix_abc")

    def test_invalid_base64_raises(self):
        """Garbage base64 after the prefix must raise ValueError."""
        with pytest.raises(ValueError, match="not valid base64url"):
            parse_run_ref("run_!!!invalid!!!")

    def test_missing_kind_raises(self):
        """A ref without a 'kind' field must be rejected."""
        import base64
        import json

        payload = json.dumps({"run_id": "abc"}).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(ValueError, match="missing kind"):
            parse_run_ref(f"run_{encoded}")

    def test_missing_run_id_raises(self):
        """A ref without a 'run_id' field must be rejected."""
        import base64
        import json

        payload = json.dumps({"kind": "member"}).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(ValueError, match="missing kind or run_id"):
            parse_run_ref(f"run_{encoded}")

    def test_member_ref_missing_org_raises(self):
        """A member run ref without an 'org' field must be rejected."""
        import base64
        import json

        payload = json.dumps({"kind": "member", "run_id": "abc"}).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(ValueError, match="missing org"):
            parse_run_ref(f"run_{encoded}")

    def test_x402_ref_missing_wallet_raises(self):
        """An x402 run ref without a 'wallet' field must be rejected."""
        import base64
        import json

        payload = json.dumps({"kind": "x402", "run_id": "abc"}).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(ValueError, match="missing wallet"):
            parse_run_ref(f"run_{encoded}")

    def test_unsupported_kind_raises(self):
        """An unknown auth kind must be rejected with a clear message."""
        import base64
        import json

        payload = json.dumps(
            {"kind": "unknown_kind", "run_id": "abc"},
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        with pytest.raises(ValueError, match="Unsupported"):
            parse_run_ref(f"run_{encoded}")

    def test_empty_string_raises(self):
        """An empty string must be rejected."""
        with pytest.raises(ValueError, match="must start with"):
            parse_run_ref("")
