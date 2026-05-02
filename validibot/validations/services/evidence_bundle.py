"""``EvidenceBundleBuilder`` — package a run's evidence into a downloadable tarball.

ADR-2026-04-27 Phase 4 Session C/3: an operator-facing artefact
that bundles everything an external verifier needs into a single
``.tar.gz`` download:

- ``manifest.json`` — the canonical-JSON manifest (Session A's
  output, copied verbatim from the run's ``RunEvidenceArtifact``).
- ``manifest.sig`` — the compact-JWS signed credential (Session
  C/2's output) when ``validibot-pro`` is installed AND the run
  has an ``IssuedCredential``. Carries the
  ``credentialSubject.validationRun.manifestHash`` claim that
  binds the credential to the manifest's exact bytes.
- ``README.txt`` — human-readable orientation: what's in the
  bundle, how to verify, where to find the corresponding workflow.

What's intentionally NOT in the bundle (this session)
=====================================================

The ADR's eventual end-state lists optional ``input/`` and
``output/`` subdirectories carrying raw bytes (per retention
policy). Those are deferred:

- Session C's acceptance criteria only require the bundle to
  round-trip cleanly through verify; raw input/output bytes
  aren't part of the verify story.
- Including raw bytes adds a retention-policy decision per
  workflow (which input bytes survived ``DO_NOT_STORE``? which
  output retention tier permits inclusion?). That deserves its
  own focused design.
- For now, the manifest's payload digests (input/output SHA-256s)
  are the bundle's evidence of payload identity — the bytes
  themselves can come later without changing the bundle's
  fundamental shape.

Pro-awareness
=============

The signature (``manifest.sig``) is conditionally included via
``apps.is_installed("validibot_pro")``, mirroring the pattern in
:func:`validibot.validations.credential_utils.get_signed_credential_display_context`.
A community-only deployment produces a bundle with just
``manifest.json`` and ``README.txt``; a pro-enabled deployment
adds ``manifest.sig`` automatically. No feature flag, no separate
code path — the same builder produces both shapes.

A note on naming: ``manifest.sig`` vs ``credential.jwt``
========================================================

The same JWS bytes ship under two different filenames depending on
how a user downloads them:

- **``credential.jwt``** — served by the standalone "Download
  Credential" button on the run detail page (the Signed Credential
  card). This is the W3C VC ecosystem's de facto extension for
  compact-JWS credentials and matches the JWT-tooling convention
  most third-party verifiers expect.

- **``manifest.sig``** — written into the evidence bundle tarball
  next to ``manifest.json``. The ``.sig`` extension follows the
  long-standing sidecar-signature convention used by signed
  release artefacts (``package.tar.gz`` + ``package.tar.gz.sig``,
  ``release.zip`` + ``release.zip.asc``). Inside the bundle, this
  filename communicates "I am the signature *over the file next to
  me*" more clearly than ``credential.jwt`` would.

Both files are byte-identical — both pull from
``IssuedCredential.credential_jws`` and both verify against the
same JWKS. The W3C VC 2.0 spec defines IANA media types
(``application/vc+jwt``, etc.) but deliberately does **not**
mandate a file extension; implementers choose what serves their
context. We chose context-appropriate names for each surface
rather than forcing one name to win in both places.

If you ever rename one, rename neither — pick a single filename
that reads well in both contexts (no obvious candidate exists),
and update the README in this module's ``_build_readme`` plus the
download view in ``validations/views/evidence.py``. Cross-reference
in the README so users aren't surprised by either form.
"""

from __future__ import annotations

import gzip
import io
import logging
import tarfile
import textwrap
from typing import TYPE_CHECKING

from django.apps import apps

from validibot.validations.models import RunEvidenceArtifactAvailability

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


class BundleNotAvailableError(Exception):
    """Raised when a bundle can't be built — usually no GENERATED artifact.

    Distinct from a generic ``Exception`` so the view can map it to
    a clean 404 without swallowing genuine bugs.
    """


class EvidenceBundleBuilder:
    """Stateless builder for run-level evidence bundles.

    Static methods only. Mirrors the
    :class:`validibot.validations.services.evidence.EvidenceManifestBuilder`
    shape so future builders (e.g. an audit-log bundle) inherit the
    same convention.
    """

    @staticmethod
    def build(run: ValidationRun) -> bytes:
        """Return the gzipped-tar bytes for ``run``'s evidence bundle.

        Args:
            run: A completed validation run with a ``GENERATED``
                ``RunEvidenceArtifact``. Caller is expected to have
                permission-gated access to the run.

        Returns:
            gzipped-tar bytes (``application/gzip``, suitable for an
            attachment download).

        Raises:
            BundleNotAvailableError: If the run has no
                ``RunEvidenceArtifact``, the artifact is in
                ``FAILED`` / ``PURGED`` state, or its
                ``manifest_path`` is empty. The view layer maps this
                to a 404 (matching the convention used by the
                manifest endpoint).
        """
        artifact = EvidenceBundleBuilder._require_generated_artifact(run)
        manifest_bytes = EvidenceBundleBuilder._read_manifest_bytes(artifact)
        signature_bytes = EvidenceBundleBuilder._read_signature_bytes(run)
        readme_bytes = EvidenceBundleBuilder._build_readme(
            run=run,
            artifact=artifact,
            has_signature=signature_bytes is not None,
        )

        return EvidenceBundleBuilder._pack_tarball(
            manifest_bytes=manifest_bytes,
            signature_bytes=signature_bytes,
            readme_bytes=readme_bytes,
        )

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _require_generated_artifact(run):
        """Look up the run's evidence artifact and validate it's downloadable."""
        try:
            artifact = run.evidence_artifact
        except Exception as exc:
            msg = f"Run {run.id} has no evidence manifest yet."
            raise BundleNotAvailableError(msg) from exc

        if artifact.availability != RunEvidenceArtifactAvailability.GENERATED:
            msg = (
                f"Run {run.id} evidence artifact is in "
                f"{artifact.availability} state; bundle not buildable."
            )
            raise BundleNotAvailableError(msg)

        if not artifact.manifest_path:
            msg = f"Run {run.id} evidence artifact has no stored manifest bytes."
            raise BundleNotAvailableError(msg)

        return artifact

    @staticmethod
    def _read_manifest_bytes(artifact) -> bytes:
        """Read the manifest's canonical-JSON bytes from storage."""
        artifact.manifest_path.open("rb")
        try:
            return artifact.manifest_path.read()
        finally:
            artifact.manifest_path.close()

    @staticmethod
    def _read_signature_bytes(run) -> bytes | None:
        """Return the signed-credential JWS bytes when pro+credential exist.

        ``manifest.sig`` is the full compact-JWS credential. A verify
        flow re-parses the JWS, validates the signature, extracts the
        ``credentialSubject.validationRun.manifestHash`` claim, and
        confirms it matches a fresh hash of ``manifest.json``.
        Putting the full JWS in ``manifest.sig`` (rather than just
        the detached signature) is what makes that verify path work
        from the bundle alone.

        Returns:
            JWS bytes when pro is installed and the run has an
            ``IssuedCredential``. ``None`` for community-only
            deployments OR for runs that haven't been issued a
            credential.
        """
        if not apps.is_installed("validibot_pro"):
            return None

        # Local import to keep this module importable without pro.
        from validibot_pro.credentials.models import IssuedCredential

        credential = (
            IssuedCredential.objects.filter(workflow_run=run)
            .order_by("-created")
            .first()
        )
        if credential is None:
            return None

        # IssuedCredential.credential_jws holds the compact JWS
        # string directly — same field the existing
        # ``CredentialDownloadView`` returns to operators clicking
        # "download credential". A future pro release that renames
        # this field would break both the standalone download AND
        # the bundle inclusion; that's the right coupling, since
        # they're both serving the same artefact.
        compact = getattr(credential, "credential_jws", "")
        if not compact:
            return None
        # JWS is ASCII-safe by construction (base64url-encoded
        # segments separated by ``.``).
        return compact.encode("ascii")

    @staticmethod
    def _build_readme(*, run, artifact, has_signature: bool) -> bytes:
        """Produce a human-readable README.txt explaining the bundle.

        Operators (and external auditors) opening the tarball expect
        a 30-second orientation: what's here, how to verify it, what
        to do if any check fails.
        """
        workflow = run.workflow
        # Use the run's terminal-state timestamp rather than export
        # wall-clock time. The "when did this run happen?" answer is
        # what operators actually want, AND it makes the bundle's
        # bytes stable across re-exports of the same run (a
        # property a future bundle-hash story can rely on).
        run_completed_at = run.ended_at.isoformat() if run.ended_at else "<unknown>"
        signature_section = (
            textwrap.dedent(
                """\
                manifest.sig
                ============

                A W3C Verifiable Credential 2.0 in compact-JWS format
                (media type ``application/vc+jwt``). Contains a
                ``credentialSubject.validationRun.manifestHash`` claim
                binding the credential to ``manifest.json``'s bytes.

                Note on naming: this file is byte-for-byte identical
                to the ``credential.jwt`` you can download separately
                from the run detail page in Validibot. Two filenames,
                same bytes — the ``.sig`` extension reflects this
                file's role as a sidecar attestation about the
                ``manifest.json`` next to it; the ``.jwt`` extension
                reflects the standalone download's role as a
                self-describing credential. Either filename verifies
                identically against the issuer's JWKS.

                To verify:

                1. Parse manifest.sig as a JWT.
                2. Verify the JWS signature against the issuer's
                   public key (publicly available at the issuer's
                   .well-known/jwks.json).
                3. Recompute SHA-256 of manifest.json's bytes.
                4. Compare the recomputed hash to
                   credentialSubject.validationRun.manifestHash.
                5. If they match, the credential refers to the bytes
                   you have. If they differ, the manifest has been
                   tampered with after signing — discard.
                """,
            )
            if has_signature
            else (
                "manifest.sig is not present in this bundle.\n"
                "The originating deployment did not issue a signed "
                "credential for this run. The manifest's hash is\n"
                "still recoverable by re-fetching manifest.json from\n"
                "the original deployment and recomputing SHA-256.\n"
            )
        )

        body = textwrap.dedent(
            f"""\
            Validibot Evidence Bundle
            =========================

            Run completed at:   {run_completed_at}
            Run ID:             {run.id}
            Workflow:           {workflow.slug} (v{workflow.version})
            Manifest schema:    {artifact.schema_version}
            Manifest SHA-256:   {artifact.manifest_hash}

            What's in this bundle
            ---------------------

            manifest.json
                The canonical-JSON evidence manifest produced when
                run {run.id} completed. Contains the workflow's
                contract snapshot, per-step validator metadata
                (slug + version + semantic_digest), the input-schema
                contract, retention class, and SHA-256 hashes of the
                run's input and (where retention permits) output
                bytes.

                Re-hashing this file's bytes and comparing to the
                ``Manifest SHA-256`` value above tells you whether
                the file is intact.

            README.txt
                This file.

            {signature_section}
            What's NOT in this bundle
            -------------------------

            Raw input or output bytes are not currently included.
            The manifest's ``payload_digests`` carry the SHA-256
            hashes of the input and (where retention permits) output;
            those hashes are the cryptographic identity of the
            payload data without exposing the bytes themselves. A
            future bundle revision may include raw bytes for runs
            where the workflow's retention policy permits.

            More information
            ----------------

            Manifest schema spec: validibot-shared package,
            ``validibot_shared.evidence`` module.

            Trust model: see the Validibot documentation at
            https://docs.validibot.com/ for the complete
            evidence-and-credential trust story.
            """,
        )
        return body.encode("utf-8")

    @staticmethod
    def _pack_tarball(
        *,
        manifest_bytes: bytes,
        signature_bytes: bytes | None,
        readme_bytes: bytes,
    ) -> bytes:
        """Pack the bundle members into a gzipped tarball.

        Determinism is the property the tests pin: two runs of the
        same bundle must produce byte-for-byte identical archives
        (so a future "bundle hash" story is possible). That requires
        normalising both the tar's per-member metadata (mode, mtime,
        uid/gid — handled in :meth:`_add_member`) AND the gzip
        wrapper's metadata (the gzip header carries an mtime field
        that defaults to the current time).

        Implementation: build the uncompressed tar in memory, then
        gzip it with ``GzipFile(mtime=0)`` so the gzip header's
        timestamp is fixed.
        """
        # Step 1: build the raw tar bytes with normalised member metadata.
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            EvidenceBundleBuilder._add_member(tar, "manifest.json", manifest_bytes)
            if signature_bytes is not None:
                EvidenceBundleBuilder._add_member(
                    tar,
                    "manifest.sig",
                    signature_bytes,
                )
            EvidenceBundleBuilder._add_member(tar, "README.txt", readme_bytes)

        # Step 2: gzip with explicit mtime=0 so the wrapper header is
        # deterministic. Without this, GzipFile defaults to time.time()
        # and two builds milliseconds apart produce different bytes.
        gz_buf = io.BytesIO()
        with gzip.GzipFile(
            fileobj=gz_buf,
            mode="wb",
            mtime=0,
            compresslevel=6,
        ) as gz:
            gz.write(tar_buf.getvalue())
        return gz_buf.getvalue()

    @staticmethod
    def _add_member(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        """Add a single in-memory file to the tar with normalised metadata."""
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = 0o644
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        tar.addfile(info, io.BytesIO(data))


__all__ = ["BundleNotAvailableError", "EvidenceBundleBuilder"]
