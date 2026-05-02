"""Tests for ``EvidenceBundleBuilder`` and the bundle download view.

ADR-2026-04-27 Phase 4 Session C/3: the bundle is the artefact a
verifier reads to confirm "this credential is signed by an
expected issuer and refers to the manifest bytes I have." These
tests pin the bundle's contract:

1. **Always-present members**: ``manifest.json`` (bytes match
   storage) and ``README.txt`` (human-readable).
2. **Conditional ``manifest.sig``**: included when validibot-pro
   is installed AND the run has an ``IssuedCredential``; omitted
   otherwise.
3. **Determinism**: building the same run twice produces
   identical bytes (so a future "bundle hash" story is possible).
4. **Failure modes**: no artifact / FAILED artifact -> the
   builder raises ``BundleNotAvailableError`` and the view
   produces a 404.

The pro-side path is exercised via a minimal monkey-patched
``apps.is_installed`` + a fake ``IssuedCredential`` model so we
don't need the full pro install in this test suite. The
integration path (real pro installed) is covered elsewhere.
"""

from __future__ import annotations

import io
import tarfile
from datetime import UTC
from datetime import datetime
from http import HTTPStatus

import pytest
from django.test import TestCase
from django.urls import reverse

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.evidence import stamp_evidence_manifest
from validibot.validations.services.evidence_bundle import BundleNotAvailableError
from validibot.validations.services.evidence_bundle import EvidenceBundleBuilder
from validibot.validations.tests.factories import ValidationRunFactory


def _setup_run_with_owner_access():
    """Build (user, run) where user is owner of run's org."""
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.OWNER)
    user.memberships.get(org=org).set_roles({RoleCode.OWNER})
    user.set_current_org(org)

    submission = SubmissionFactory(org=org, user=user, project__org=org)
    run = ValidationRunFactory(
        org=org,
        user=user,
        submission=submission,
        workflow=submission.workflow,
        project=submission.project,
        status=ValidationRunStatus.SUCCEEDED,
        ended_at=datetime(2026, 5, 2, tzinfo=UTC),
    )
    return user, run


def _read_tar_member(bundle_bytes: bytes, name: str) -> bytes | None:
    """Extract a single member's bytes from a gzipped-tar bundle."""
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        try:
            member = tar.getmember(name)
        except KeyError:
            return None
        f = tar.extractfile(member)
        if f is None:
            return None
        return f.read()


def _list_tar_members(bundle_bytes: bytes) -> list[str]:
    """Return the sorted member names in the tarball."""
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        return sorted(tar.getnames())


# ──────────────────────────────────────────────────────────────────────
# EvidenceBundleBuilder — service-level contract
# ──────────────────────────────────────────────────────────────────────


class EvidenceBundleBuilderTests(TestCase):
    """The builder produces a deterministic gzipped-tar with the right members."""

    def test_bundle_contains_manifest_json_matching_artifact_bytes(self):
        """``manifest.json`` in the tarball matches the stored manifest's bytes."""
        _, run = _setup_run_with_owner_access()
        artifact = stamp_evidence_manifest(run)
        assert artifact is not None  # sanity check before reading file

        bundle = EvidenceBundleBuilder.build(run)
        manifest_bytes = _read_tar_member(bundle, "manifest.json")
        assert manifest_bytes is not None

        # Compare to what the artifact's FileField currently holds.
        artifact.refresh_from_db()
        artifact.manifest_path.open("rb")
        try:
            stored = artifact.manifest_path.read()
        finally:
            artifact.manifest_path.close()
        assert manifest_bytes == stored

    def test_bundle_always_contains_readme(self):
        """``README.txt`` is always present in the bundle."""
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        bundle = EvidenceBundleBuilder.build(run)
        readme = _read_tar_member(bundle, "README.txt")
        assert readme is not None
        # Spot-check a couple of structural anchors.
        readme_text = readme.decode("utf-8")
        assert "Validibot Evidence Bundle" in readme_text
        assert "manifest.json" in readme_text

    def test_bundle_omits_manifest_sig_in_community_only_deployment(self):
        """Without validibot_pro installed, no ``manifest.sig`` member."""
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        # The test suite typically doesn't have validibot_pro
        # installed; assert directly. (If it does, this test
        # documents what the community-only path looks like — the
        # _other_ pro-installed test below covers the pro path.)
        bundle = EvidenceBundleBuilder.build(run)
        members = _list_tar_members(bundle)
        assert "manifest.sig" not in members
        # Check the README mentions the absence so operators
        # opening the tarball understand why.
        readme = _read_tar_member(bundle, "README.txt").decode("utf-8")
        assert "manifest.sig is not present" in readme

    def test_bundle_is_deterministic(self):
        """Building twice produces byte-identical output.

        Member metadata is normalised (mode 0o644, mtime 0, no
        uid/gid) so a future "bundle hash" story can rely on the
        same bytes coming out.
        """
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        bundle1 = EvidenceBundleBuilder.build(run)
        bundle2 = EvidenceBundleBuilder.build(run)
        assert bundle1 == bundle2

    def test_bundle_member_metadata_is_normalised(self):
        """Members have stable mode, mtime, uid, gid — no leak of dev env."""
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        # Named expected values rather than literals so ruff's
        # "magic value in comparison" rule reads them as policy
        # rather than typos.
        expected_mode = 0o644

        bundle = EvidenceBundleBuilder.build(run)
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
            for member in tar.getmembers():
                assert member.mode == expected_mode
                assert member.mtime == 0
                assert member.uid == 0
                assert member.gid == 0
                assert member.uname == ""
                assert member.gname == ""

    def test_builder_raises_for_run_without_artifact(self):
        """No ``RunEvidenceArtifact`` -> ``BundleNotAvailableError``."""
        _, run = _setup_run_with_owner_access()
        # No stamp call.

        with pytest.raises(BundleNotAvailableError):
            EvidenceBundleBuilder.build(run)

    def test_builder_raises_for_failed_artifact(self):
        """A FAILED artifact -> ``BundleNotAvailableError``.

        FAILED means manifest bytes don't exist in storage; there's
        nothing to put in the tarball.
        """
        from validibot_shared.evidence import SCHEMA_VERSION

        from validibot.validations.models import RunEvidenceArtifact
        from validibot.validations.models import RunEvidenceArtifactAvailability

        _, run = _setup_run_with_owner_access()
        RunEvidenceArtifact.objects.create(
            run=run,
            schema_version=SCHEMA_VERSION,
            availability=RunEvidenceArtifactAvailability.FAILED,
            generation_error="storage IOError: simulated",
        )

        with pytest.raises(BundleNotAvailableError):
            EvidenceBundleBuilder.build(run)


# ──────────────────────────────────────────────────────────────────────
# EvidenceBundleDownloadView — view-level contract
# ──────────────────────────────────────────────────────────────────────


class EvidenceBundleDownloadViewTests(TestCase):
    """View-level happy path + permission + 404 coverage."""

    def test_authenticated_owner_can_download_bundle(self):
        """Owner gets a valid gzipped-tar back as application/gzip."""
        user, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.OK
        assert response["Content-Type"] == "application/gzip"
        # Body parses as a valid gzipped tarball.
        members = _list_tar_members(response.content)
        assert "manifest.json" in members
        assert "README.txt" in members

    def test_response_carries_hash_and_schema_version_headers(self):
        """Mirrors the manifest endpoint's CLI-friendly headers."""
        user, run = _setup_run_with_owner_access()
        artifact = stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        assert response["X-Validibot-Manifest-Sha256"] == artifact.manifest_hash
        assert "X-Validibot-Schema-Version" in response

    def test_response_filename_is_evidence_run_id(self):
        """Disposition filename includes the run id for operator orientation."""
        user, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        assert "attachment" in response["Content-Disposition"]
        assert f"evidence-{run.id}.tar.gz" in response["Content-Disposition"]

    def test_no_cache_header_set(self):
        """Re-downloads after re-stamp surface fresh bytes."""
        user, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        assert "no-store" in response["Cache-Control"]

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated requests bounce off LoginRequiredMixin."""
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        # 302 redirect to login.
        assert response.status_code in (
            HTTPStatus.MOVED_PERMANENTLY,
            HTTPStatus.FOUND,
        )

    def test_user_from_other_org_gets_404(self):
        """Cross-org access is filtered at the queryset; result is 404, not 403."""
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        other_org = OrganizationFactory()
        other_user = UserFactory(orgs=[other_org])
        grant_role(other_user, other_org, RoleCode.OWNER)
        other_user.memberships.get(org=other_org).set_roles({RoleCode.OWNER})
        other_user.set_current_org(other_org)

        self.client.force_login(other_user)
        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_run_without_artifact_returns_404(self):
        """A run that hasn't been stamped yet -> 404."""
        user, run = _setup_run_with_owner_access()

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_failed_artifact_returns_404(self):
        """A FAILED artifact -> 404 (no bytes to bundle)."""
        from validibot_shared.evidence import SCHEMA_VERSION

        from validibot.validations.models import RunEvidenceArtifact
        from validibot.validations.models import RunEvidenceArtifactAvailability

        user, run = _setup_run_with_owner_access()
        RunEvidenceArtifact.objects.create(
            run=run,
            schema_version=SCHEMA_VERSION,
            availability=RunEvidenceArtifactAvailability.FAILED,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_bundle_download", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.NOT_FOUND


# ──────────────────────────────────────────────────────────────────────
# UI: bundle action on run detail page
# ──────────────────────────────────────────────────────────────────────


class EvidenceCardBundleActionTests(TestCase):
    """The run-detail page exposes a bundle download button alongside manifest."""

    def test_detail_page_includes_bundle_download_link(self):
        """The evidence card carries both manifest + bundle URLs."""
        user, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.OK
        bundle_url = reverse(
            "validations:evidence_bundle_download",
            args=[run.id],
        )
        assert bundle_url.encode() in response.content

    def test_detail_page_omits_bundle_link_when_no_artifact(self):
        """Without an artifact, neither the manifest nor bundle button renders."""
        user, run = _setup_run_with_owner_access()

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", args=[run.id]),
        )
        bundle_url = reverse(
            "validations:evidence_bundle_download",
            args=[run.id],
        )
        assert bundle_url.encode() not in response.content


# ──────────────────────────────────────────────────────────────────────
# Pro-aware ``manifest.sig`` inclusion
# ──────────────────────────────────────────────────────────────────────


class BundleProAwarenessTests(TestCase):
    """``manifest.sig`` is included only when pro is installed + credential exists.

    The validibot-shared test suite doesn't install validibot_pro,
    so we exercise the pro-aware path via two indirections:

    1. ``override_settings(INSTALLED_APPS=...)`` to make
       ``apps.is_installed("validibot_pro")`` return True.
    2. A monkey-patched stand-in for the pro
       ``IssuedCredential.objects.filter(...).order_by(...).first()``
       chain.

    This pins the *contract* (when both gates pass, the JWS bytes
    land in the tarball as ``manifest.sig``) without needing pro
    installed.
    """

    def test_no_signature_when_pro_not_installed(self):
        """Default test settings have no pro -> no manifest.sig."""
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        bundle = EvidenceBundleBuilder.build(run)
        assert "manifest.sig" not in _list_tar_members(bundle)

    def test_signature_included_when_pro_installed_and_credential_exists(self):
        """Both gates pass -> JWS bytes flow into the tarball as manifest.sig.

        We can't use ``override_settings(INSTALLED_APPS=[..., 'validibot_pro'])``
        because Django actually tries to import the app config when
        the apps registry rebuilds, and ``validibot_pro`` isn't
        installed in this test venv. Instead, we patch the two
        seams the builder consults:

        1. ``django.apps.apps.is_installed`` — the boolean gate.
        2. ``sys.modules['validibot_pro.credentials.models']`` —
           the lazy import target. Pre-populating it lets Python's
           import machinery skip the filesystem lookup.

        Together those simulate "pro is installed AND a credential
        exists" without needing the full pro install.
        """
        from unittest import mock

        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        class _FakeCredential:
            credential_jws = "eyJhbGciOiJFUzI1NiJ9.eyJtYW5pZmVzdEhhc2giOiJhYmMifQ.SIG"

        # Build a queryset-shaped mock so the builder's
        # ``filter(...).order_by(...).first()`` chain returns our
        # fake credential.
        fake_qs = mock.MagicMock()
        fake_qs.order_by.return_value.first.return_value = _FakeCredential()
        fake_model = mock.MagicMock()
        fake_model.objects.filter.return_value = fake_qs

        # Stub the module so the lazy import succeeds without pro
        # being actually installed.
        import sys

        fake_module = mock.MagicMock()
        fake_module.IssuedCredential = fake_model

        with (
            mock.patch.dict(
                sys.modules,
                {"validibot_pro.credentials.models": fake_module},
            ),
            mock.patch(
                "validibot.validations.services.evidence_bundle.apps.is_installed",
                return_value=True,
            ),
        ):
            bundle = EvidenceBundleBuilder.build(run)

        members = _list_tar_members(bundle)
        assert "manifest.sig" in members
        sig_bytes = _read_tar_member(bundle, "manifest.sig")
        assert sig_bytes == _FakeCredential.credential_jws.encode("ascii")
