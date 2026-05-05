# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Validibot, please report it responsibly by emailing **security@mcquilleninteractive.com**. Do not open a public GitHub issue.

Please include a description of the vulnerability and steps to reproduce it.

## What to Expect

Validibot is maintained by a small team (me). I'll do my best to acknowledge reports and work toward a fix, but I can't guarantee specific response times. Critical issues will be prioritized.

If you'd like to be credited in the release notes when a fix ships, let me know in your report.

## Scope

Security reports are welcome for:

- The Validibot Django web application (this repository)
- The REST API
- Authentication and authorization
- Cryptographic operations (credential signing, JWKS)

For vulnerabilities in third-party dependencies, please report those to the upstream project directly.

## Verifying Validibot releases

Validibot ships as source you `git clone` from GitHub — there's no PyPI wheel or Docker image with a third-party registry attesting "this came from the Validibot maintainers." We provide that attestation ourselves by signing each release tag with a maintainer key. The release CI in `.github/workflows/release.yml` runs the same check before publishing release artifacts, so a successful local verification matches what the pipeline did.

To verify and check out a release:

```bash
# 1. Get the source. If you already have a clone, run
#    `git fetch --tags` instead of cloning again.
git clone https://github.com/danielmcquillen/validibot.git
cd validibot

# 2. One-time setup: tell git to check SSH signatures against
#    .allowed_signers, which ships in this repo. Without this,
#    git falls back to GPG and verification will fail.
git config gpg.ssh.allowedSignersFile "$(pwd)/.allowed_signers"
git config gpg.format ssh

# 3. Verify a release tag. Exits 0 only when the signature matches
#    a maintainer key in .allowed_signers. Non-zero means stop.
git verify-tag v0.4.0

# 4. Check out the verified tag.
git checkout v0.4.0
```

Find the latest release tag at <https://github.com/danielmcquillen/validibot/releases/latest> and substitute it for `v0.4.0` above. After step 2, every future `git verify-tag` and `git pull --verify-signatures` in this clone uses the repo's `.allowed_signers` automatically — use `git config --global` if you work across multiple Validibot clones.

Validibot signs tags with SSH keys, so verification succeeds when the signature matches a maintainer's public key listed in `.allowed_signers`. We don't currently publish a GPG key, so the GPG verification path applies only if a future maintainer chooses to.

### If verification fails

A non-zero exit from `git verify-tag` means **do not install**. Likely causes:

- **Skipped step 2.** Run the `git config` lines, then retry.
- **Wrong repo.** Confirm the clone URL is `https://github.com/danielmcquillen/validibot.git` — not a fork or mirror that may lack `.allowed_signers`.
- **Genuinely bad tag.** If the URL and config are right and a freshly-cloned copy still fails, don't install — open an issue at <https://github.com/danielmcquillen/validibot/issues>.

### What's in a release

Each signed-tag release publishes the verified git tag, a [GitHub release page](https://github.com/danielmcquillen/validibot/releases) with auto-generated notes, and CycloneDX SBOM artifacts (`validibot.cdx.json`, `validibot.cdx.xml`) attached to the release page as downloadable assets. The SBOMs list every Python dependency in the resolved environment, so you can audit the dependency chain without running `uv lock` yourself.

### Verifying upstream packages (optional)

Validibot pulls in two sister projects as dependencies — `uv sync` and the validator runner handle them automatically — but each has its own provenance trail if you want defense in depth:

- **[`validibot-shared`](https://pypi.org/project/validibot-shared/)** — Pydantic models published to PyPI with build attestations from PyPI's trusted-publishing flow. Most operators rely on `uv`'s hash-locked install; if you want more, the package page shows the GitHub workflow run that built each release.
- **[`validibot-validator-backends`](https://github.com/danielmcquillen/validibot-validator-backends)** — EnergyPlus / FMU container images on GitHub Container Registry, signed with sigstore attestations. If you run advanced (containerised) validators, verify each image with `gh attestation verify oci://ghcr.io/...@sha256:...`. See that repo's `RELEASING.md` for the full recipe. If you only run the built-in validators (Basic, JSON Schema, XML Schema, AI), you don't need these images at all.

## Security Best Practices for Deployment

When deploying Validibot in production:

1. **Always use HTTPS** in production
2. **Set a strong `DJANGO_SECRET_KEY`** - generate one with:
   ```bash
   python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
   ```
3. **Restrict `DJANGO_ALLOWED_HOSTS`** to your specific domain(s)
4. **Use environment variables** or a secrets manager for all credentials
5. **Never commit `.envs/` files** to version control (they are gitignored by default)
6. **Keep dependencies updated** - run `uv lock --upgrade` regularly

## Disclaimer

This software is provided "as is", without warranty of any kind. See the [LICENSE](LICENSE) file for full terms. Security fixes are provided on a best-effort basis.
