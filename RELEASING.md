# Releasing Validibot

This document describes how Validibot maintainers cut a release and how
operators verify a release after cloning. Trust ADR Phase 5 Session D
(2026-05-02) shipped signed-tag enforcement plus an SBOM artifact on
each release — this file is the operator-facing doc that explains both.

## TL;DR

```bash
# Maintainer (release):
git tag -s v1.2.3 -m "Release notes go here"
git push origin v1.2.3
# CI workflow .github/workflows/release.yml then verifies the tag
# signature and publishes the GitHub release with an SBOM attached.

# Operator (verify after clone):
git fetch --tags
git verify-tag v1.2.3   # exits 0 only when the tag is signed by an allowed key
git checkout v1.2.3
```

## Why we sign release tags

Validibot is distributed via `git clone`, not as a PyPI wheel. That
means we can't lean on PyPI's OIDC build attestation to prove
provenance — operators receive the source directly from GitHub and
need a way to confirm the tagged commit is what a maintainer
intended to ship.

Signed git tags are the answer:

- `git tag -s` (or its newer alias `git tag --sign`) attaches a GPG
  or SSH signature to the tag's metadata.
- `git verify-tag` confirms the signature was made by a key the
  repo recognises as a maintainer's.
- The signature covers the commit hash. An attacker who pushes a
  malicious tag to `main` can't forge a signature, and rewriting the
  tag locally and force-pushing breaks signature continuity.

Combined with branch protection (which gates commits) and the
`.github/workflows/release.yml` workflow (which gates tag-driven
release publishing), every public release artifact links back to a
maintainer-signed tag.

## How the maintainer signs

### One-time setup: configure git to sign tags

You can sign with either GPG or SSH. SSH is simpler if you already
use an SSH key for git.

**SSH-signed tags (recommended):**

```bash
# Tell git to sign with SSH and use your existing SSH public key.
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global gpg.format ssh
git config --global tag.gpgsign true   # auto-sign all tags

# Add your public key to the repo's allowed_signers file so CI can
# verify it. (You only do this once per repo.)
echo "$(git config user.email) namespaces=\"git\" $(cat ~/.ssh/id_ed25519.pub)" \
  >> .allowed_signers
git add .allowed_signers
git commit -m "Add my SSH key to allowed_signers"
```

**GPG-signed tags:**

```bash
# Generate a GPG key if you don't have one (skip if you do):
gpg --full-generate-key
# Get the long-form key ID:
gpg --list-secret-keys --keyid-format=long
# Tell git to sign with that key:
git config --global user.signingkey YOUR_KEY_ID
git config --global tag.gpgsign true
```

The GPG public key needs to be uploaded to GitHub
(<https://github.com/settings/gpg/new>) so the GitHub UI shows the
"Verified" badge and CI can fetch it.

### Cutting a release

1. Update `CHANGELOG.md` under `[Unreleased]` and bump the version
   string in `pyproject.toml`.
2. Commit and push the changelog/version bump on a PR. After merge:
   ```bash
   git checkout main
   git pull
   ```
3. Sign and push the tag:
   ```bash
   git tag -s vX.Y.Z -m "vX.Y.Z release notes"
   git push origin vX.Y.Z
   ```
4. CI runs `.github/workflows/release.yml`:
   - Verifies `git verify-tag vX.Y.Z` succeeds.
   - Generates `validibot.cdx.json` / `validibot.cdx.xml` SBOMs.
   - Creates the GitHub release with the SBOMs attached.

If the workflow fails at "Verify tag is signed", the tag wasn't
signed correctly. Re-create it with `git tag -d vX.Y.Z`, fix your
git config, and re-tag-and-push.

## How an operator verifies a release

After cloning the repo, an operator can confirm any tag was made
by a known maintainer:

```bash
git fetch --tags
git verify-tag v1.2.3
```

`git verify-tag` exits with code 0 when:

- For SSH-signed tags: the signature was made by a key listed in
  `.allowed_signers` at the repo root (and `gpg.ssh.allowedSignersFile`
  is configured).
- For GPG-signed tags: the signature was made by a key in the local
  GPG keyring marked trusted.

To configure local verification once:

```bash
# Tell git where the allowed_signers file lives (for SSH verification):
git config gpg.ssh.allowedSignersFile "$(pwd)/.allowed_signers"
git config gpg.format ssh
```

After that, every `git verify-tag` and `git pull --verify-signatures`
checks against the `.allowed_signers` file checked into the repo.

## What's in a release

Every signed-tag release publishes:

1. **The git tag itself** — verifiable signature linking commit
   hash to maintainer key.
2. **A GitHub release page** auto-generated from `--generate-notes`
   (commit messages between this tag and the previous).
3. **SBOM artifacts** — `validibot.cdx.json` (CycloneDX JSON) and
   `validibot.cdx.xml` (CycloneDX XML) covering every Python
   dependency in the resolved environment. Operators can audit
   dependency chains without re-running `uv lock`.

Future releases may also ship a Scorecard score (already published
under `.github/workflows/scorecard.yml`); see the README badge for
the current score.

## Related docs

- `validibot-validator-backends/RELEASING.md` — same recipe applied
  to the validator backend Docker images, with OIDC attestation on
  the image digest in addition to the SBOM.
- `validibot-shared/CHANGELOG.md` — the wheel-publishing flow uses
  PyPI trusted publishing (OIDC) rather than git tag signatures.
- `validibot-project/docs/adr/2026-04-27-trust-boundary-hardening-and-evidence-first-validation.md`,
  Phase 5 Session D — the architectural decision behind this
  release process.
