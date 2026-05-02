"""Tests for validator backend trust-tier hardening.

ADR-2026-04-27 Phase 5 Session C — every ``Validator`` row carries a
``trust_tier`` column (TIER_1 / TIER_2). The runner reads it at
launch time and applies a tier-aware sandbox profile:

- Tier 1 (default for everything we ship today) keeps the Phase 1
  hardening profile: cap_drop=ALL, no-new-privileges, read-only
  rootfs, configurable network, default mem/CPU caps.
- Tier 2 (user-added or partner-authored backends, future) layers
  tighter overrides on top: forced ``network=none``, halved memory
  and CPU defaults, and optional gVisor / Kata runtime via
  ``VALIDATOR_TIER_2_CONTAINER_RUNTIME``.

This suite pins down four properties:

1. The model field defaults to TIER_1 for every new ``Validator``
   row — no existing deployment changes behavior on upgrade.
2. ``_apply_tier_2_hardening`` produces the correct overrides:
   network forced to none, mem/cpu caps tightened, runtime
   optionally injected.
3. The runner invokes the helper only when ``trust_tier=="TIER_2"``
   — Tier-1 launches use the unchanged Tier-1 dict.
4. The semantic-digest layer treats ``trust_tier`` as a SEMANTIC
   field — flipping the tier under the same (slug, version) is
   drift, not a silent no-op.

What's deliberately not covered here
====================================

- Real container launches under gVisor: requires gVisor installed
  on the test runner. The unit layer asserts the runner sets
  ``runtime="runsc"`` correctly; gVisor enforces what we set.
- Cloud Run trust-tier enforcement: Cloud Run tier-2 maps to GCP
  Binary Authorization + VPC config rather than a runtime overlay,
  so it's a Cloud Run-specific implementation, not a runner shim.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from validibot.validations.constants import ValidatorTrustTier
from validibot.validations.services.runners.docker import _apply_tier_2_hardening
from validibot.validations.services.validator_digest import SEMANTIC_FIELDS

pytestmark = pytest.mark.django_db


# ── Model field defaults ────────────────────────────────────────────────


class TestValidatorTrustTierFieldDefaults:
    """Every new ``Validator`` row defaults to TIER_1."""

    def test_default_is_tier_1(self):
        """A fresh validator from the factory carries ``TIER_1``.

        This is the structural guarantee Phase 1 deployments rely on:
        upgrading to the new column doesn't accidentally re-tier any
        first-party validator into the stricter Tier-2 profile.
        """
        from validibot.validations.tests.factories import ValidatorFactory

        validator = ValidatorFactory()
        assert validator.trust_tier == ValidatorTrustTier.TIER_1

    def test_tier_2_can_be_set_explicitly(self):
        """Operators (and the future registration flow) can opt into Tier-2.

        Once user-added backends ship, the registration UI sets
        TIER_2 explicitly. This test asserts the field accepts that
        value cleanly — no validation error, no silent reset to
        TIER_1.
        """
        from validibot.validations.tests.factories import ValidatorFactory

        validator = ValidatorFactory(trust_tier=ValidatorTrustTier.TIER_2)
        assert validator.trust_tier == ValidatorTrustTier.TIER_2


# ── _apply_tier_2_hardening helper ──────────────────────────────────────


class TestApplyTier2Hardening:
    """The helper produces the correct config overrides."""

    def _baseline_config(self) -> dict:
        """Build a Tier-1-shaped container_config to layer overrides on top.

        Mirrors what ``DockerValidatorRunner`` constructs at the
        Tier-1 stage: cap_drop, security_opt, mem/cpu caps, network
        config. The helper's job is to mutate this into the Tier-2
        shape.
        """
        return {
            "image": "registry/example/backend@sha256:abc",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "read_only": True,
            "user": "1000:1000",
            "mem_limit": "4g",
            "nano_cpus": int(2.0 * 1e9),
            "network": "validator-network",  # something explicit to clear
        }

    @override_settings(
        VALIDATOR_TIER_2_MEMORY_LIMIT="2g",
        VALIDATOR_TIER_2_CPU_LIMIT="1.0",
        VALIDATOR_TIER_2_CONTAINER_RUNTIME="",
    )
    def test_forces_network_none(self):
        """Tier-2 always uses ``network_mode=none``.

        Even when a Tier-1 deployment configures an explicit Docker
        network (e.g. ``validator-network`` for Compose stacks),
        Tier-2 strips that and forces ``network=none``. Partner
        code has no business reaching the network — the envelope
        contract delivers everything via shared storage.
        """
        config = self._baseline_config()
        hardened = _apply_tier_2_hardening(config)
        assert "network" not in hardened
        assert hardened["network_mode"] == "none"

    @override_settings(
        VALIDATOR_TIER_2_MEMORY_LIMIT="2g",
        VALIDATOR_TIER_2_CPU_LIMIT="1.0",
        VALIDATOR_TIER_2_CONTAINER_RUNTIME="",
    )
    def test_tightens_resource_caps(self):
        """Tier-2 halves the default mem/cpu budget.

        Defaults: 4g/2.0 → 2g/1.0. The numeric values come from
        settings so deployments with unusual cap profiles can tune
        them, but the *direction* is fixed: Tier-2 must not be
        looser than Tier-1.
        """
        config = self._baseline_config()
        hardened = _apply_tier_2_hardening(config)
        assert hardened["mem_limit"] == "2g"
        assert hardened["nano_cpus"] == int(1.0 * 1e9)

    @override_settings(
        VALIDATOR_TIER_2_MEMORY_LIMIT="2g",
        VALIDATOR_TIER_2_CPU_LIMIT="1.0",
        VALIDATOR_TIER_2_CONTAINER_RUNTIME="runsc",
    )
    def test_injects_runtime_when_configured(self):
        """``VALIDATOR_TIER_2_CONTAINER_RUNTIME=runsc`` → ``runtime=runsc``.

        Operators install gVisor (``runsc``) or Kata Containers and
        flip the setting to opt in. The runner doesn't try to detect
        runtime availability — that's a deployment concern flagged
        by the doctor command.
        """
        config = self._baseline_config()
        hardened = _apply_tier_2_hardening(config)
        assert hardened["runtime"] == "runsc"

    @override_settings(
        VALIDATOR_TIER_2_MEMORY_LIMIT="2g",
        VALIDATOR_TIER_2_CPU_LIMIT="1.0",
        VALIDATOR_TIER_2_CONTAINER_RUNTIME="",
    )
    def test_omits_runtime_when_unset(self):
        """Empty ``VALIDATOR_TIER_2_CONTAINER_RUNTIME`` leaves runtime alone.

        This is the right posture for deployments that haven't
        installed gVisor: the host's default runtime (typically
        ``runc``) is still substantially harder than Tier-1
        ``runc`` because of the network/cap restrictions Tier-2
        layers on. Breaking every Tier-2 launch on a missing
        runtime would be worse than running under standard runc.
        """
        config = self._baseline_config()
        hardened = _apply_tier_2_hardening(config)
        assert "runtime" not in hardened

    @override_settings(
        VALIDATOR_TIER_2_MEMORY_LIMIT="2g",
        VALIDATOR_TIER_2_CPU_LIMIT="1.0",
    )
    def test_returns_new_dict_does_not_mutate(self):
        """Helper produces a fresh dict so call-site reasoning stays clean.

        The runner builds the Tier-1 config, then layers Tier-2
        overrides on top. Mutating in place would make the call
        site harder to reason about — "what's in container_config
        right now?" should be answerable from the assignment, not
        a chain of mutations.
        """
        config = self._baseline_config()
        original_mem = config["mem_limit"]
        hardened = _apply_tier_2_hardening(config)
        # Original dict unchanged
        assert config["mem_limit"] == original_mem
        # Returned dict carries the overrides
        assert hardened["mem_limit"] != original_mem


# ── Semantic digest contract ────────────────────────────────────────────


class TestTrustTierIsSemanticField:
    """``trust_tier`` belongs to the semantic-digest field set.

    Flipping a validator's tier under the same (slug, version)
    silently changes the sandbox profile — that's exactly the kind
    of drift the semantic_digest is designed to catch. The drift
    detection in ``sync_validators`` will refuse such a change
    unless ``--allow-drift`` is passed.
    """

    def test_trust_tier_is_in_semantic_fields(self):
        """The frozenset includes ``trust_tier``."""
        assert "trust_tier" in SEMANTIC_FIELDS

    def test_changing_trust_tier_changes_semantic_digest(self):
        """Two configs differing only in tier produce different digests.

        This is the contract that gives ``sync_validators`` its
        teeth: tier flips can't sneak through without being noticed.
        """
        from validibot.validations.services.validator_digest import (
            compute_semantic_digest,
        )

        cfg_tier_1 = {
            "validation_type": "EXAMPLE",
            "trust_tier": "TIER_1",
            # Other semantic fields kept stable across both digests
            "validator_class": "validibot.example.Validator",
            "compute_tier": "LOW",
        }
        cfg_tier_2 = dict(cfg_tier_1, trust_tier="TIER_2")

        digest_1 = compute_semantic_digest(cfg_tier_1)
        digest_2 = compute_semantic_digest(cfg_tier_2)
        assert digest_1 != digest_2


# ── Runner integration: trust_tier flows through ───────────────────────


class TestDockerRunnerAppliesTier2:
    """The runner reads ``trust_tier`` and invokes the helper.

    We don't launch a real container here — that would require a
    Docker daemon, gVisor, etc. We assert that the runner's
    construction of ``container_config`` reflects the right tier
    profile based on the ``trust_tier`` argument.
    """

    @override_settings(
        VALIDATOR_TIER_2_MEMORY_LIMIT="2g",
        VALIDATOR_TIER_2_CPU_LIMIT="1.0",
        VALIDATOR_TIER_2_CONTAINER_RUNTIME="runsc",
        VALIDATOR_BACKEND_IMAGE_POLICY="tag",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False,
    )
    def test_tier_1_does_not_apply_tier_2_overrides(self):
        """Tier-1 launches keep the unchanged Tier-1 config.

        The ``runtime`` key is the canary: Tier-2 sets it (when the
        runtime setting is non-empty); Tier-1 leaves it absent.
        Asserting absence proves the tier-aware branch was skipped.
        """
        # Lazy unit-level test: capture container_config that the
        # runner would pass to docker by patching containers.run.
        from unittest.mock import MagicMock
        from unittest.mock import patch

        from validibot.validations.services.runners.docker import DockerValidatorRunner

        runner = DockerValidatorRunner(memory_limit="4g", cpu_limit="2.0")

        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            container = MagicMock()
            container.short_id = "test123"
            container.wait.return_value = {"StatusCode": 0}
            container.logs.return_value = b""
            container.image.attrs = {}
            container.attrs = {"Image": "sha256:" + "0" * 64}
            return container

        with patch("docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            mock_client.containers.run.side_effect = fake_run
            mock_client.ping.return_value = True
            mock_from_env.return_value = mock_client

            runner.run(
                container_image="example:tag",
                input_uri="file:///tmp/in.json",
                output_uri="file:///tmp/out.json",
                trust_tier=ValidatorTrustTier.TIER_1,
            )

        # Tier-1 doesn't inject the runtime key.
        assert "runtime" not in captured
        # Tier-1 keeps the configured 4g memory limit.
        assert captured["mem_limit"] == "4g"

    @override_settings(
        VALIDATOR_TIER_2_MEMORY_LIMIT="2g",
        VALIDATOR_TIER_2_CPU_LIMIT="1.0",
        VALIDATOR_TIER_2_CONTAINER_RUNTIME="runsc",
        VALIDATOR_BACKEND_IMAGE_POLICY="tag",
        COSIGN_VERIFY_VALIDATOR_BACKEND_IMAGES=False,
    )
    def test_tier_2_injects_overrides(self):
        """Tier-2 launches carry the tightened config.

        Asserts the three observable changes: mem cap halved,
        runtime injected, network forced to none.
        """
        from unittest.mock import MagicMock
        from unittest.mock import patch

        from validibot.validations.services.runners.docker import DockerValidatorRunner

        runner = DockerValidatorRunner(memory_limit="4g", cpu_limit="2.0")

        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            container = MagicMock()
            container.short_id = "test123"
            container.wait.return_value = {"StatusCode": 0}
            container.logs.return_value = b""
            container.image.attrs = {}
            container.attrs = {"Image": "sha256:" + "0" * 64}
            return container

        with patch("docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            mock_client.containers.run.side_effect = fake_run
            mock_client.ping.return_value = True
            mock_from_env.return_value = mock_client

            runner.run(
                container_image="example:tag",
                input_uri="file:///tmp/in.json",
                output_uri="file:///tmp/out.json",
                trust_tier=ValidatorTrustTier.TIER_2,
            )

        assert captured["mem_limit"] == "2g"
        assert captured["runtime"] == "runsc"
        assert captured["network_mode"] == "none"
        # ``network`` (explicit network attachment) must be absent
        # — Tier-2 enforces no-network regardless of the deployment
        # default.
        assert "network" not in captured
