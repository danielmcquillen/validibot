# Verifying a Validibot release

This document is for **operators** verifying a Validibot release
after cloning. The maintainer's release recipe (signing keys,
release workflow internals, etc.) is internal documentation; this
file covers what you need to know as a downstream consumer of
Validibot.

## Why we sign release tags

Validibot is distributed as source via `git clone`, not as a PyPI
wheel. That means we can't lean on PyPI's OIDC build attestation
to prove provenance — operators receive the source directly from
GitHub and need a way to confirm the tagged commit is what a
maintainer intended to ship.

Signed git tags solve this:

- `git tag -s` attaches a GPG or SSH signature to the tag's
  metadata.
- `git verify-tag` confirms the signature was made by a key the
  repo recognises as a maintainer's.
- The signature covers the commit hash. An attacker who pushes a
  malicious tag to `main` can't forge a signature, and rewriting
  the tag locally and force-pushing breaks signature continuity.

The CI workflow at `.github/workflows/release.yml` enforces this:
unsigned tags are refused at the verify step before any release
metadata is published.

## Verifying a release after clone

```bash
git clone https://github.com/danielmcquillen/validibot.git
cd validibot
git fetch --tags

# Confirm the tag was signed by a maintainer key in this repo's
# allowed_signers file:
git verify-tag v0.4.0   # exits 0 only when signed by an allowed key

# Check out the verified tag:
git checkout v0.4.0
```

`git verify-tag` exits with code 0 when:

- For SSH-signed tags: the signature was made by a key listed in
  `.allowed_signers` at the repo root.
- For GPG-signed tags: the signature was made by a key in your
  local GPG keyring marked trusted.

To configure local verification once:

```bash
# Use the repo's allowed_signers for SSH verification:
git config gpg.ssh.allowedSignersFile "$(pwd)/.allowed_signers"
git config gpg.format ssh
```

After that, every `git verify-tag` and `git pull
--verify-signatures` checks against the `.allowed_signers` file
checked into the repo. (You can do `git config --global` if you
work with multiple Validibot repos.)

## What's in a release

Each signed-tag release publishes:

1. **The git tag itself** — verifiable signature linking the
   commit hash to a maintainer key.
2. **A GitHub release page** at
   <https://github.com/danielmcquillen/validibot/releases/tag/vX.Y.Z>
   with auto-generated release notes.
3. **CycloneDX SBOM artifacts** — `validibot.cdx.json` and
   `validibot.cdx.xml` covering every Python dependency in the
   resolved environment. Operators can audit dependency chains
   without re-running `uv lock`.

## Related repositories

Validibot is composed of multiple repositories that work together;
each has its own release flow:

- **`validibot-shared`** — Pydantic models published as a PyPI
  wheel with PyPI OIDC build attestation. Verify via `pip install
  validibot-shared==X.Y.Z` and the PyPI provenance UI.
- **`validibot-validator-backends`** — EnergyPlus / FMU container
  images on GitHub Container Registry. Verify via
  `gh attestation verify oci://ghcr.io/...@sha256:...`. See that
  repo's `RELEASING.md` for the recipe.

## ADR reference

The full architectural rationale for this release model lives in
ADR-2026-04-27 §Phase 5 Session D in the `validibot-project`
repository. The maintainer release recipe lives at
`validibot-project/docs/operations/releasing/validibot.md`.
