"""Tests for ``EvidenceManifestDownloadView``.

ADR-2026-04-27 Phase 4 Session C/1: the operator-facing endpoint
that downloads a run's canonical-JSON evidence manifest. These
tests pin the contract:

1. **Permission gating piggybacks on run access**. If the user can
   see the run-detail view, they can download the manifest. No
   separate permission needed (the manifest is just a structured
   view of the run that the user could already read).
2. **404 when the run has no GENERATED artifact**. Both the
   "never stamped" and "FAILED stamp" cases produce 404 — the
   absence is part of the run's expected lifecycle.
3. **Successful download streams ``manifest.json`` with the
   manifest's bytes**, ``Content-Type: application/json``, and
   ``Cache-Control: no-store`` so re-stamps surface fresh bytes.
4. **Hash header allows CLI verification without parsing the
   body**. ``X-Validibot-Manifest-Sha256`` mirrors what's on the
   ``RunEvidenceArtifact`` row.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from http import HTTPStatus

from django.test import TestCase
from django.urls import reverse
from validibot_shared.evidence import SCHEMA_VERSION

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import RunEvidenceArtifact
from validibot.validations.models import RunEvidenceArtifactAvailability
from validibot.validations.services.evidence import stamp_evidence_manifest
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


class EvidenceManifestDownloadViewTests(TestCase):
    """Happy-path + permission + 404 coverage."""

    def test_authenticated_owner_can_download_manifest(self):
        """Run owner gets the manifest bytes back as application/json."""
        user, run = _setup_run_with_owner_access()
        # Stamp the manifest the way the run-completion path would.
        artifact = stamp_evidence_manifest(run)
        assert artifact is not None  # sanity check before the test body

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )

        assert response.status_code == HTTPStatus.OK
        assert response["Content-Type"] == "application/json"
        # FileResponse drains the file when streaming; accumulate bytes.
        body = b"".join(response.streaming_content)
        # The stored bytes match what we just downloaded.
        artifact.refresh_from_db()
        artifact.manifest_path.open("rb")
        try:
            stored = artifact.manifest_path.read()
        finally:
            artifact.manifest_path.close()
        assert body == stored

    def test_response_carries_hash_header(self):
        """X-Validibot-Manifest-Sha256 mirrors the artifact row's hash.

        Lets CLI consumers verify the body's hash without re-parsing
        the JSON.
        """
        user, run = _setup_run_with_owner_access()
        artifact = stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )
        assert response["X-Validibot-Manifest-Sha256"] == artifact.manifest_hash
        assert response["X-Validibot-Schema-Version"] == SCHEMA_VERSION

    def test_response_is_attachment_named_manifest_json(self):
        """Disposition header forces a file download named manifest.json."""
        user, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )
        assert "attachment" in response["Content-Disposition"]
        assert "manifest.json" in response["Content-Disposition"]

    def test_response_is_no_cache(self):
        """Re-export after re-stamp must surface the latest bytes."""
        user, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )
        assert "no-store" in response["Cache-Control"]

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated requests bounce off the LoginRequiredMixin."""
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        # No login.
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )
        # The mixin redirects (302) to the login URL.
        assert response.status_code in (302, 301)

    def test_user_from_other_org_gets_404(self):
        """Cross-org access is filtered at the queryset; result is 404, not 403.

        Returning 404 (rather than 403) is the existing convention
        on the run-detail surface — it doesn't leak the existence
        of runs the user can't see.
        """
        _, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        # Different org's user with no overlap.
        other_org = OrganizationFactory()
        other_user = UserFactory(orgs=[other_org])
        grant_role(other_user, other_org, RoleCode.OWNER)
        other_user.memberships.get(org=other_org).set_roles({RoleCode.OWNER})
        other_user.set_current_org(other_org)

        self.client.force_login(other_user)
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_run_without_artifact_returns_404(self):
        """A run that hasn't been stamped yet -> 404.

        Pre-Phase-4 runs and in-flight runs both fall through this
        branch. The auditor's MANIFEST_MISSING finding is where the
        operator sees this aggregated across runs.
        """
        user, run = _setup_run_with_owner_access()
        # Don't stamp.

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_failed_artifact_returns_404(self):
        """A FAILED artifact is not downloadable — manifest bytes don't exist."""
        user, run = _setup_run_with_owner_access()
        # Simulate the path where stamping failed.
        RunEvidenceArtifact.objects.create(
            run=run,
            schema_version=SCHEMA_VERSION,
            availability=RunEvidenceArtifactAvailability.FAILED,
            generation_error="storage IOError: disk full",
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:evidence_manifest_download", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.NOT_FOUND


class EvidenceCardOnRunDetailTests(TestCase):
    """The run-detail page surfaces the evidence card when an artifact exists."""

    def test_detail_page_shows_evidence_block_when_artifact_present(self):
        """Page rendering: the partial appears with the download URL."""
        user, run = _setup_run_with_owner_access()
        stamp_evidence_manifest(run)

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.OK
        # The download URL appears in the page (the partial is rendered).
        download_url = reverse(
            "validations:evidence_manifest_download",
            args=[run.id],
        )
        assert download_url.encode() in response.content

    def test_detail_page_omits_evidence_block_when_no_artifact(self):
        """Without an artifact, the partial renders nothing."""
        user, run = _setup_run_with_owner_access()
        # Don't stamp.

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.OK
        download_url = reverse(
            "validations:evidence_manifest_download",
            args=[run.id],
        )
        assert download_url.encode() not in response.content

    def test_detail_page_omits_evidence_block_when_artifact_failed(self):
        """A FAILED artifact also produces no card (no usable bytes)."""
        user, run = _setup_run_with_owner_access()
        RunEvidenceArtifact.objects.create(
            run=run,
            schema_version=SCHEMA_VERSION,
            availability=RunEvidenceArtifactAvailability.FAILED,
        )

        self.client.force_login(user)
        response = self.client.get(
            reverse("validations:validation_detail", args=[run.id]),
        )
        assert response.status_code == HTTPStatus.OK
        download_url = reverse(
            "validations:evidence_manifest_download",
            args=[run.id],
        )
        assert download_url.encode() not in response.content
