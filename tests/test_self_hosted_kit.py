"""Tests for Phase 0 of the self-hosted deployment kit.

This module verifies the *shape* of the self-hosted deployment kit:
that the directory rename landed cleanly, that stub helper scripts
respond to ``--help`` with usage text, that the Compose file parses,
that the env templates exist with the eight ADR-mandated comment
groups, and that operator-facing ``just`` recipes have cross-target
parity between ``self-hosted`` and ``gcp`` modules.

These are *kit-shape* tests, not behaviour tests. The actual recipe
implementations land in later ADR phases (Phase 1 doctor, Phase 3
backup/restore, etc.). What this suite locks in today:

1. The Phase 0 rename (``docker-compose`` → ``self-hosted``) is
   complete and consistent across justfile, env example directories,
   the ``DEPLOYMENT_TARGET`` enum, and the production Compose file.
2. The ``deploy/self-hosted/`` kit directory exists with the four
   stub helper scripts the ADR mandates, each emitting ``--help``
   output that matches its expected interface.
3. The ``just/self-hosted/`` and ``just/gcp/`` modules expose matching
   operator recipes (``doctor``, ``smoke-test``, ``backup``,
   ``restore``, ``collect-support-bundle``, ``validators``) so
   cross-target parity is enforced by construction. The one
   intentional asymmetry — self-hosted is single-stage per VM, GCP
   is multi-stage — is captured via the recipe's argument shape.
4. Every stub helper script handles ``--help`` cleanly (exit 0,
   prints usage referencing the script name).

If a future PR breaks any of these invariants — for instance, by
accidentally re-introducing the historical ``docker-compose``
terminology, or by adding a self-hosted recipe without a paired GCP
recipe — these tests fail with a clear pointer to the relevant
ADR-2026-04-27 section.

Tests run independently of Django settings since the kit is a
file-and-process-level artifact, not a database concern. We use
``SimpleTestCase`` to avoid Django's database setup overhead.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml
from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[1]
KIT_ROOT = REPO_ROOT / "deploy" / "self-hosted"
ENVS_EXAMPLE_ROOT = REPO_ROOT / ".envs.example" / ".production" / ".self-hosted"
DOCS_ROOT = REPO_ROOT / "docs" / "operations" / "self-hosting"

# The two host-prep helper scripts the kit ships. Only these two
# remain as scripts because they have to run *before* ``just`` is
# installed on a fresh VM. Everything else (check-dns,
# build-pro-image, doctor, backup, etc.) is a ``just`` recipe — see
# ADR-2026-04-27 section 4 ("Just modules are the unified driver").
STUB_SCRIPTS = [
    "bootstrap-host",
    "bootstrap-digitalocean",
]

# Operator-facing recipes that must exist in BOTH ``just/self-hosted/``
# AND ``just/gcp/`` for cross-target parity. See ADR section 1
# (operator capability matrix).
#
# ``errors-since`` was added during Phase 1 Session 1 — incident-
# response log scanning is a capability both targets need (self-hosted
# greps Compose logs, GCP greps Cloud Run logs).
PARITY_RECIPES = [
    "doctor",
    "smoke-test",
    "backup",
    "restore",
    "collect-support-bundle",
    "validators",
    "errors-since",
    # ``cleanup`` was added in Phase 5 — both substrates accumulate
    # artefacts that aren't part of any working set (stopped
    # containers + dangling images on self-hosted; old Cloud Run
    # Job executions + expired GCS backups on GCP), and operators
    # shouldn't have to know substrate-specific prune commands.
    "cleanup",
]

# Self-hosted-only recipes that don't need a GCP equivalent. ``check-dns``
# and ``build-pro-image`` only make sense on a customer VM (DNS is
# managed at the project level on GCP; Pro images are built locally
# from a wheel only in self-hosted deployments).
SELF_HOSTED_ONLY_RECIPES = [
    "check-dns",
    "build-pro-image",
]

# The eight comment groups the ADR mandates inside
# .envs.example/.production/.self-hosted/.django. See ADR section 2
# (Phase 0 task 2).
ENV_GROUPS = [
    "1. REQUIRED",
    "2. URLs / SECURITY",
    "3. DATABASE / CACHE",
    "4. STORAGE",
    "5. EMAIL",
    "6. VALIDATORS",
    "7. PRO / SIGNING",
    "8. OPTIONAL TELEMETRY",
]


class KitDirectoryShapeTests(SimpleTestCase):
    """Verify the deploy/self-hosted/ directory structure exists.

    This is the operator-facing artifact directory. If a future PR
    deletes it accidentally or moves these files around, operators
    get a confusing kit and the DigitalOcean tutorial breaks.
    """

    def test_kit_root_exists(self):
        """``deploy/self-hosted/`` must exist as the kit entry point.

        ADR section 5 makes this directory the canonical operator-
        facing artifact location.
        """
        assert KIT_ROOT.is_dir(), (
            f"Kit directory missing: {KIT_ROOT}. See ADR-2026-04-27 section 5."
        )

    def test_kit_readme_exists(self):
        """A README must exist so operators have one place to start.

        ADR acceptance criterion: "There is one place to start."
        """
        assert (KIT_ROOT / "README.md").is_file(), (
            "deploy/self-hosted/README.md missing. "
            "Operators need one entry point — see ADR acceptance criteria."
        )

    def test_caddyfile_exists(self):
        """The Caddyfile is the bundled-reverse-proxy artifact.

        Off-by-default but its file must exist so the ``caddy`` Compose
        profile (in docker-compose.production.yml) has something to
        mount. See ADR section 21.
        """
        assert (KIT_ROOT / "caddy" / "Caddyfile").is_file(), (
            "deploy/self-hosted/caddy/Caddyfile missing. See ADR-2026-04-27 section 21."
        )

    def test_overview_doc_exists(self):
        """The operator overview is the single doc entry point in Phase 0.

        ADR acceptance criterion: "A new user can see the intended
        deployment shape without reading source."
        """
        assert (DOCS_ROOT / "overview.md").is_file(), (
            "docs/operations/self-hosting/overview.md missing. "
            "Operators need a doc entry point — see ADR Phase 0 task 7."
        )

    def test_digitalocean_provider_doc_exists(self):
        """The DigitalOcean tutorial is Phase 0's outline + Phase 1's full doc.

        ADR acceptance criterion: "The DigitalOcean path is visible
        as the first supported provider quickstart."
        """
        do_doc = DOCS_ROOT / "providers" / "digitalocean.md"
        assert do_doc.is_file(), f"{do_doc} missing. See ADR-2026-04-27 section 15."


class StubScriptHelpTests(SimpleTestCase):
    """Verify each Phase 0 stub helper responds to --help cleanly.

    The four scripts are stubs — their actual behaviour lands in
    later phases. But they must:

    1. Respond to ``--help`` with exit code 0 (so docs and operator
       walkthroughs that say "run ``foo --help`` to see usage" don't
       fail mysteriously).
    2. Print usage text that mentions the script's own name (so an
       operator who runs ``--help`` knows what they're looking at).
    3. Run without `bash` prefix once cloned — i.e. the file mode
       in the git index includes the executable bit.

    We invoke via ``bash`` here only to remain robust against this
    test running in a sandbox where the filesystem permissions might
    not match the git index mode. Operators using a clone will see
    the executable bit applied.
    """

    def _run_help(self, script_name: str) -> subprocess.CompletedProcess[str]:
        """Run ``bash <kit>/scripts/<name> --help`` and return the result."""
        script_path = KIT_ROOT / "scripts" / script_name
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(script_path), "--help"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )

    def test_bootstrap_host_help(self):
        """``bootstrap-host --help`` succeeds and mentions the script.

        ADR Phase 0 task 6: stub helpers ship with --help output even
        when implementation lands later.
        """
        result = self._run_help("bootstrap-host")
        self.assertEqual(
            result.returncode,
            0,
            f"bootstrap-host --help failed: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertIn("bootstrap-host", result.stdout)

    def test_bootstrap_digitalocean_help(self):
        """``bootstrap-digitalocean --help`` succeeds and mentions the script.

        DigitalOcean is Phase 0's named first provider (ADR section 15).
        """
        result = self._run_help("bootstrap-digitalocean")
        self.assertEqual(result.returncode, 0)
        self.assertIn("bootstrap-digitalocean", result.stdout)

    def test_old_check_dns_script_gone(self):
        """``check-dns`` is now a ``just`` recipe, not a script.

        Migrated in Phase 0 to live under ``just/self-hosted/``
        because it only runs after ``just`` is installed (no
        chicken-and-egg). The script file must not exist — leaving it
        behind would diverge from the recipe and confuse operators.
        """
        old_script = KIT_ROOT / "scripts" / "check-dns"
        assert not old_script.exists(), (
            f"{old_script} still exists. It was moved into "
            f"`just self-hosted check-dns`. Delete the old script."
        )

    def test_old_build_pro_image_script_gone(self):
        """``build-pro-image`` is now a ``just`` recipe, not a script.

        Same reason as ``check-dns`` — it runs after ``just`` is
        available, so it doesn't need to be a standalone script.
        """
        old_script = KIT_ROOT / "scripts" / "build-pro-image"
        assert not old_script.exists(), (
            f"{old_script} still exists. It was moved into "
            f"`just self-hosted build-pro-image`. Delete the old script."
        )

    def test_all_stubs_have_executable_mode_in_git(self):
        """Stub scripts must have the executable bit set in the git index.

        Without this, a fresh clone gives operators
        "Permission denied" when they try to run the scripts directly.
        ``git update-index --chmod=+x`` is what writes the executable
        mode into the index — see ADR Phase 0 task 4.
        """
        result = subprocess.run(
            [
                "/usr/bin/env",
                "git",
                "ls-files",
                "--stage",
                "deploy/self-hosted/scripts/",
            ],
            capture_output=True,
            check=True,
            cwd=str(REPO_ROOT),
            text=True,
        )
        # Each line looks like: ``100755 <hash> 0\tdeploy/self-hosted/scripts/<name>``
        # 100755 = executable file in git's mode encoding.
        for stub in STUB_SCRIPTS:
            line = next(
                (
                    line
                    for line in result.stdout.splitlines()
                    if line.endswith(f"deploy/self-hosted/scripts/{stub}")
                ),
                None,
            )
            assert line is not None, f"Stub {stub} not in git index."
            mode = line.split()[0]
            self.assertEqual(
                mode,
                "100755",
                f"Stub {stub} has git mode {mode}, expected 100755 (executable). "
                f"Run: git update-index --chmod=+x deploy/self-hosted/scripts/{stub}",
            )


class EnvTemplateShapeTests(SimpleTestCase):
    """Verify the renamed self-hosted env templates have the right shape.

    ADR Phase 0 task 1 renames .envs.example/.production/.docker-compose/
    to .envs.example/.production/.self-hosted/. Phase 0 task 2
    restructures .django so its comment headers match the eight
    ADR-mandated groups.
    """

    def test_renamed_env_directory_exists(self):
        """The .self-hosted/ env example directory must exist.

        If this test fails, the rename in ADR Phase 0 task 1 either
        didn't happen or got reverted.
        """
        assert ENVS_EXAMPLE_ROOT.is_dir(), (
            f"{ENVS_EXAMPLE_ROOT} missing. "
            f"Did the docker-compose → self-hosted rename get reverted?"
        )

    def test_old_env_directory_gone(self):
        """The historical .docker-compose/ env directory must be gone.

        Two parallel directories would let the rename slowly drift.
        Phase 0 hard-renames; no deprecation alias.
        """
        old_dir = REPO_ROOT / ".envs.example" / ".production" / ".docker-compose"
        assert not old_dir.exists(), (
            f"{old_dir} still exists after rename. "
            f"Phase 0 hard-renames — see ADR open question 10 resolution."
        )

    def test_django_template_has_self_hosted_target(self):
        """The .django template must set DEPLOYMENT_TARGET=self_hosted.

        Phase 0 also renamed the env-var value from docker_compose to
        self_hosted (matching the audience-named module rename).
        """
        django_env = (ENVS_EXAMPLE_ROOT / ".django").read_text(encoding="utf-8")
        self.assertIn("DEPLOYMENT_TARGET=self_hosted", django_env)
        self.assertNotIn("DEPLOYMENT_TARGET=docker_compose", django_env)

    def test_django_template_has_eight_adr_groups(self):
        """The .django template must include all eight comment groups.

        ADR Phase 0 task 2 mandates: required; URLs/security;
        database/cache; storage; email; validators; Pro/signing;
        optional telemetry. Each group must appear as a numbered
        comment header so operators can navigate the file.
        """
        django_env = (ENVS_EXAMPLE_ROOT / ".django").read_text(encoding="utf-8")
        for group in ENV_GROUPS:
            self.assertIn(
                group,
                django_env,
                f"Missing ADR group header '{group}' in .django template. "
                f"See ADR section 2 task 2.",
            )

    def test_all_four_env_files_exist(self):
        """The four env files (.django, .postgres, .build, .mcp) must all exist.

        Each plays a distinct role in the self-hosted Compose stack:
        .django for Django runtime config, .postgres for DB credentials,
        .build for commercial-package + recipe knobs, .mcp for the
        opt-in FastMCP container.
        """
        for filename in (".django", ".postgres", ".build", ".mcp"):
            path = ENVS_EXAMPLE_ROOT / filename
            assert path.is_file(), (
                f"{path} missing — Phase 0 expected all four env files."
            )


class ComposeFileShapeTests(SimpleTestCase):
    """Verify docker-compose.production.yml parses and includes Caddy.

    The Compose file is the substrate for ``just self-hosted``
    recipes. If it stops parsing, every operator command fails.
    Caddy is the opt-in reverse proxy added in Phase 0 (ADR
    section 21).
    """

    def test_compose_file_parses(self):
        """The production Compose file must be valid YAML.

        Invalid YAML breaks every operator recipe with cryptic
        Compose errors. PyYAML is what Compose itself uses
        internally for parsing.
        """
        compose_path = REPO_ROOT / "docker-compose.production.yml"
        assert compose_path.is_file(), (
            f"{compose_path} missing — should be at the repo root."
        )
        # safe_load: don't allow the file to instantiate Python
        # objects via YAML tags; we're just checking structure.
        compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        assert isinstance(compose_data, dict)
        assert "services" in compose_data

    def test_compose_has_caddy_profile(self):
        """The Compose file must declare the ``caddy`` opt-in profile.

        ADR section 21: Caddy is off by default, opt-in via
        COMPOSE_PROFILES=caddy. The profile attribute on the caddy
        service is what makes the opt-in work.
        """
        compose_path = REPO_ROOT / "docker-compose.production.yml"
        compose_data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        services = compose_data.get("services", {})
        assert "caddy" in services, (
            "Caddy service missing from docker-compose.production.yml. "
            "See ADR-2026-04-27 section 21."
        )
        caddy = services["caddy"]
        # Two assertions split per ruff PT018: each failure mode points
        # at its own root cause (missing profiles vs. wrong profile value).
        assert "profiles" in caddy, (
            "Caddy service must declare a profiles list. "
            "See ADR section 21 — Caddy is off by default."
        )
        assert "caddy" in caddy["profiles"], (
            "Caddy service profiles list must include 'caddy'. "
            "See ADR section 21 — Caddy is off by default."
        )

    def test_compose_uses_self_hosted_env_paths(self):
        """Compose env_file paths must point at the renamed directory.

        Stale ``.docker-compose/`` paths would break every recipe at
        startup with "env file not found". The rename is mechanical
        but easy to miss in a large file.
        """
        compose_path = REPO_ROOT / "docker-compose.production.yml"
        text = compose_path.read_text(encoding="utf-8")
        # Old path must NOT appear as an env_file value
        assert ".envs/.production/.docker-compose/" not in text, (
            "docker-compose.production.yml still references the old "
            ".docker-compose/ env path. See Phase 0 rename."
        )
        # New path must appear
        assert ".envs/.production/.self-hosted/" in text


class JustRecipeParityTests(SimpleTestCase):
    """Verify just/self-hosted/ and just/gcp/ have parity for operator recipes.

    ADR section 1 (operator capability matrix) says each operator
    capability must exist in BOTH the ``self-hosted`` and ``gcp``
    just modules with the same recipe name. Today these are mostly
    Phase 0 stubs, but the recipe surface must already match so that
    Phase 1+ implementations don't accidentally diverge.

    The one intentional asymmetry is stage handling: self-hosted
    recipes take no stage argument, GCP recipes take a stage. The
    parity is at the recipe-name level, not the argument shape.
    """

    SELF_HOSTED_MOD = REPO_ROOT / "just" / "self-hosted" / "mod.just"
    GCP_MOD = REPO_ROOT / "just" / "gcp" / "mod.just"

    def _has_recipe(self, mod_path: Path, name: str) -> bool:
        """Return True if the just module file declares a recipe with this name.

        We look for ``<name>:`` or ``<name> <args>:`` at start of line,
        which is Just's recipe declaration syntax. Comments and
        recipe bodies are ignored because they're indented.
        """
        text = mod_path.read_text(encoding="utf-8")
        # Match a recipe header at the start of a line. Allow
        # parameters (with optional defaults). Recipe bodies are
        # always indented in Just, so a recipe header is the only
        # line that starts with ``<name>`` followed by optional args
        # and a colon.
        pattern = re.compile(
            rf"^{re.escape(name)}(?:\s+[^:]*)?:",
            re.MULTILINE,
        )
        return bool(pattern.search(text))

    def test_self_hosted_module_exists(self):
        """The renamed self-hosted just module must exist.

        Phase 0 task 1 renamed just/docker-compose/ to
        just/self-hosted/. If this test fails, the rename either
        didn't happen or the file was deleted.
        """
        assert self.SELF_HOSTED_MOD.is_file(), (
            f"{self.SELF_HOSTED_MOD} missing — Phase 0 rename problem?"
        )

    def test_old_docker_compose_module_gone(self):
        """The historical just/docker-compose/ directory must be gone.

        Phase 0 hard-renames (no deprecation alias). Two parallel
        modules would let the rename drift.
        """
        old_mod = REPO_ROOT / "just" / "docker-compose" / "mod.just"
        assert not old_mod.exists(), (
            f"{old_mod} still exists after rename. "
            f"See ADR open question 10 resolution (hard rename)."
        )

    def test_self_hosted_recipes_present(self):
        """All Phase 0 operator recipes must exist in just/self-hosted/.

        Implementation lands in later phases, but the recipes must
        appear today so ``just self-hosted --list`` shows the full
        operator surface.
        """
        for recipe in PARITY_RECIPES:
            assert self._has_recipe(self.SELF_HOSTED_MOD, recipe), (
                f"Recipe '{recipe}' missing from just/self-hosted/mod.just. "
                f"See ADR section 1 (operator capability matrix)."
            )

    def test_gcp_parity_recipes_present(self):
        """All Phase 0 operator recipes must exist in just/gcp/ too.

        Cross-target parity is the way we know the operator
        experience is actually consistent — see ADR section 1 and
        the parity principle in the Goals section.
        """
        for recipe in PARITY_RECIPES:
            assert self._has_recipe(self.GCP_MOD, recipe), (
                f"Recipe '{recipe}' missing from just/gcp/mod.just. "
                f"Self-hosted has it; cross-target parity requires GCP "
                f"to have it too. See ADR section 1."
            )

    def test_self_hosted_only_recipes_present(self):
        """``check-dns`` and ``build-pro-image`` exist only in just/self-hosted/.

        These are self-hosted-specific operations: DNS verification
        before TLS issuance, and locally-building a Pro image from a
        wheel. Neither has a GCP equivalent (GCP DNS is managed via
        Cloud DNS at the project level; Pro is licensed via the
        package index for self-hosted only).
        """
        for recipe in SELF_HOSTED_ONLY_RECIPES:
            assert self._has_recipe(self.SELF_HOSTED_MOD, recipe), (
                f"Recipe '{recipe}' missing from just/self-hosted/mod.just. "
                f"It was moved here from deploy/self-hosted/scripts/. "
                f"See ADR section 4 — operations that don't need to "
                f"run before just is installed should be just recipes."
            )


class GcpOperatorRecipeInvariantTests(SimpleTestCase):
    """Pin trust-critical GCP recipe wiring that cannot run in unit tests.

    These checks are intentionally static. The recipes call ``gcloud`` and need
    live project state, but the most damaging regressions are naming and flag
    drift that we can catch by inspecting the just module.
    """

    GCP_MOD = REPO_ROOT / "just" / "gcp" / "mod.just"

    def _gcp_mod_text(self) -> str:
        """Read the GCP just module once per assertion."""
        return self.GCP_MOD.read_text(encoding="utf-8")

    def _block_between(self, start_marker: str, end_marker: str) -> str:
        """Return a justfile slice bounded by two stable markers."""
        text = self._gcp_mod_text()
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        return text[start:end]

    def test_deployed_image_resolver_uses_web_service_names(self):
        """Doctor / backup / restore must inspect the deployed web service.

        A resolver pointed at ``validibot`` instead of ``validibot-web`` makes
        every higher-level operator command either fail or inspect a legacy
        service that is not serving traffic.
        """
        block = self._block_between(
            "_resolve-deployed-image stage:",
            "# _run-doctor-job",
        )

        assert 'SERVICE="${APP_NAME}-web"' in block
        assert 'SERVICE="${APP_NAME}-web-{{stage}}"' in block
        assert 'SERVICE="${APP_NAME}"' not in block
        assert 'SERVICE="${APP_NAME}-{{stage}}"' not in block

    def test_errors_since_filters_deployed_web_and_restore_jobs(self):
        """Incident scanning must include real web service and restore verifier."""
        block = self._block_between(
            'errors-since stage duration="1h":',
            "# End-to-end smoke test",
        )

        assert 'WEB_SERVICE="${APP_NAME}-web"' in block
        assert 'WEB_SERVICE="${APP_NAME}-web-{{stage}}"' in block
        assert "verify-backup" in block

    def test_operator_job_updates_converge_identity_and_database_binding(self):
        """Re-running an operator recipe must converge old Cloud Run Jobs.

        The create path has always set service account and Cloud SQL binding.
        The update path has to set them too, otherwise an old job keeps stale
        runtime identity even after the recipe appears to succeed.
        """
        blocks = [
            self._block_between(
                "_run-doctor-job stage image args:",
                "# _run-backup",
            ),
            self._block_between(
                "_run-backup stage image:",
                "# _run-restore",
            ),
            self._block_between(
                "_run-restore stage path image:",
                "# Run setup_validibot to initialize site",
            ),
        ]

        for block in blocks:
            position = block.index("gcloud run jobs update")
            update_block = block[position : block.index("--quiet >/dev/null", position)]
            assert "--service-account" in update_block
            assert "--set-cloudsql-instances" in update_block

    def test_restore_verifies_manifest_db_artifact_before_import(self):
        """Restore must compare manifest size and sha256 before Cloud SQL import."""
        block = self._block_between(
            "_run-restore stage path image:",
            "# Run setup_validibot to initialize site",
        )

        assert "Pre-flight 4/4: verifying DB dump integrity" in block
        assert ".data.db_dump.path" in block
        assert ".data.db_dump.checksum_sha256" in block
        assert ".data.db_dump.size_bytes" in block
        assert 'gcloud sql import sql "${DB_INSTANCE}" "${DB_OBJECT}"' in block
        assert '"${BACKUP_URI}/db.sql.gz"' not in block

    def test_gcp_django_image_stamps_release_version(self):
        """GCP app images must carry the release tag used by backup manifests.

        Two acceptable forms here:

        - ``"{{validibot_version}}"`` — the original literal-constant
          form (still valid for daily deploys);
        - ``"${VALIDIBOT_VERSION:-{{validibot_version}}}"`` — the
          override-aware form added with Phase 4 upgrade pinning. The
          shell expansion picks an explicit env override (set by the
          upgrade recipe) when present, and falls back to the
          pyproject-derived constant otherwise.

        We accept either spelling so the test stays meaningful as the
        version-stamping policy evolves; what matters is that the
        recipe DOES stamp ``VALIDIBOT_VERSION`` and that the worker /
        web env carry it through to the running services.
        """
        build_block = self._block_between(
            "build: _require-gcp-config",
            "# Push Docker image",
        )
        deploy_block = self._block_between(
            "_deploy-web stage:",
            "# Internal: deploy worker service",
        )

        override_form = "${VALIDIBOT_VERSION:-{{validibot_version}}}"
        accepted_build_forms = (
            '--build-arg VALIDIBOT_VERSION="{{validibot_version}}"',
            f'--build-arg VALIDIBOT_VERSION="{override_form}"',
        )
        accepted_deploy_forms = (
            "VALIDIBOT_VERSION={{validibot_version}}",
            f'VALIDIBOT_VERSION="{override_form}"',
        )

        assert any(form in build_block for form in accepted_build_forms), (
            "GCP build recipe must stamp VALIDIBOT_VERSION via "
            "--build-arg (literal or override-aware form)."
        )
        assert any(form in deploy_block for form in accepted_deploy_forms), (
            "GCP _deploy-web must propagate VALIDIBOT_VERSION via "
            "--set-env-vars (literal or override-aware form)."
        )
        assert "VALIDIBOT_VERSION={{git_sha}}" not in deploy_block

    def test_validator_deploy_uses_backend_image_metadata_not_app_version(self):
        """Validator backend images expose OCI metadata, not VALIDATOR_VERSION env.

        The backend version comes from the Dockerfile's
        ``ARG VALIDATOR_BACKEND_VERSION`` default; the recipe only
        passes the build-arg when an operator overrides via env var
        (``${VALIDATOR_BACKEND_VERSION:+--build-arg ...}``). We assert
        that override path is wired AND that the recipe doesn't shell
        out to the deleted ``resolve-backend-image-version.py`` helper.
        """
        block = self._block_between(
            "validator-build name:",
            "validators-deploy-all stage:",
        )

        # The deleted resolver script must not be called from this recipe.
        assert "resolve-backend-image-version.py" not in block
        # OCI revision + slug are still per-build args (only the version
        # comes from the Dockerfile default).
        assert "VALIDATOR_BACKEND_REVISION" in block
        # Conditional override pattern: only passes --build-arg when
        # the env var is set, otherwise the Dockerfile default wins.
        assert "${VALIDATOR_BACKEND_VERSION:+--build-arg" in block
        # Validator-backend SHA is distinct from validibot's git SHA.
        assert "{{validator_backend_git_sha}}" in block
        assert "{{git_sha}}" not in block
        # The legacy app-version env var must not appear.
        assert "VALIDATOR_VERSION=" not in block


class NoStaleDockerComposeReferencesTests(SimpleTestCase):
    """Verify the operator surface no longer mentions the old name.

    ADR acceptance criterion 13: "The terminology rename is
    consistent: no remaining references to 'Docker Compose
    production' in operator-facing surfaces (just, .envs.example/,
    DEPLOYMENT_TARGET value, docs)."

    We don't ban "docker-compose" as a string everywhere — Compose
    is the underlying technology, and ``docker-compose.production.yml``
    is the file's actual name. We ban the old *operator-facing*
    spellings: the just module path, the env directory, and the
    enum value.
    """

    def test_no_just_docker_compose_invocation_in_docs(self):
        """Operator docs must not say ``just docker-compose <cmd>``.

        That recipe path doesn't exist anymore (renamed to
        ``just self-hosted <cmd>`` per Phase 0). Stale docs send
        operators down dead ends.
        """
        forbidden = "just docker-compose "
        for path in DOCS_ROOT.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            assert forbidden not in text, (
                f"{path} still references '{forbidden}'. "
                f"Replace with 'just self-hosted '."
            )

    def test_no_old_env_path_in_compose_or_just(self):
        """Compose file and just modules must not reference the old env path.

        Stale ``.envs/.production/.docker-compose/`` paths break
        env_file loading at startup.
        """
        forbidden = ".envs/.production/.docker-compose/"
        files_to_check = [
            REPO_ROOT / "docker-compose.production.yml",
            REPO_ROOT / "just" / "self-hosted" / "mod.just",
            ENVS_EXAMPLE_ROOT / ".django",
            ENVS_EXAMPLE_ROOT / ".postgres",
            ENVS_EXAMPLE_ROOT / ".build",
            ENVS_EXAMPLE_ROOT / ".mcp",
        ]
        for path in files_to_check:
            text = path.read_text(encoding="utf-8")
            assert forbidden not in text, (
                f"{path} still references '{forbidden}'. "
                f"Replace with '.envs/.production/.self-hosted/'."
            )

    def test_deployment_target_enum_renamed(self):
        """The DeploymentTarget enum must use SELF_HOSTED, not DOCKER_COMPOSE.

        Phase 0 renames the production-Compose enum member. The
        local-dev member (``LOCAL_DOCKER_COMPOSE``) keeps its name
        because it's the developer-dev audience, not the
        customer-self-hosted audience. The asymmetry is intentional
        and documented in the enum docstring.
        """
        # Import locally so the test class doesn't fail to load if
        # Django settings aren't configured for a non-Django run.
        from validibot.core.constants import DeploymentTarget

        assert hasattr(DeploymentTarget, "SELF_HOSTED")
        assert DeploymentTarget.SELF_HOSTED.value == "self_hosted"
        assert not hasattr(DeploymentTarget, "DOCKER_COMPOSE"), (
            "DeploymentTarget.DOCKER_COMPOSE must be renamed to SELF_HOSTED. "
            "See ADR-2026-04-27 section 2."
        )
        # local_docker_compose stays — developer-dev audience.
        assert hasattr(DeploymentTarget, "LOCAL_DOCKER_COMPOSE")


class BackupRestoreRecipeShapeTests(SimpleTestCase):
    """Verify the Phase 3 backup/restore recipes are real implementations.

    Phase 3 of ADR-2026-04-27 replaces the Phase 0 stubs with a
    manifested backup/restore pair that shares the
    ``validibot.backup.v1`` schema with GCP backups. These tests pin
    the operator-facing contract:

    1. The recipes are no longer ``_phase0-stub`` calls — they
       reference the real management commands.
    2. The backup recipe targets the manifest writer with
       ``--target self_hosted`` (matching the GCP recipe's
       ``--target gcp``).
    3. The restore recipe runs the four pre-flight gates that
       ADR section 8 mandates: manifest exists, doctor --strict,
       verify_backup_compatibility, and DB integrity.
    4. The restore recipe writes the ``.last-restore-test`` marker
       that doctor's ``VB411`` consumes.
    5. The Postgres + tar artifacts use the zstd compression named
       in ADR section 8 (``plain-sql-zstd``).
    6. The django runtime image installs ``zstd`` so ``tar --zstd``
       works inside the web container.

    These checks are static (regex / substring) — actually running
    backup against a live Compose stack is a manual / integration
    concern, not a unit test. The static checks catch the shape
    regressions that would otherwise slip through.
    """

    SELF_HOSTED_MOD = REPO_ROOT / "just" / "self-hosted" / "mod.just"
    DJANGO_DOCKERFILE = REPO_ROOT / "compose" / "production" / "django" / "Dockerfile"

    def _self_hosted_text(self) -> str:
        return self.SELF_HOSTED_MOD.read_text(encoding="utf-8")

    def test_backup_recipe_no_longer_stub(self):
        """``backup`` must not delegate to ``_phase0-stub`` anymore.

        The Phase 0 stub printed "not yet implemented" and exited 64;
        Phase 3 wires the real orchestration.
        """
        text = self._self_hosted_text()
        # Find the public ``backup:`` recipe (no args) and capture its body.
        # Recipes end at the next blank line followed by a non-indented line.
        match = re.search(
            r"^backup:\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None, (
            "Public ``backup:`` recipe missing from just/self-hosted/mod.just."
        )
        body = match.group(1)
        assert "_phase0-stub" not in body, (
            "``backup`` is still a Phase 0 stub. Phase 3 of ADR-2026-04-27 "
            "replaces it with the manifested orchestration in _run-backup."
        )
        assert "_run-backup" in body, (
            "``backup`` should delegate to ``_run-backup`` private recipe."
        )

    def test_restore_recipe_no_longer_stub(self):
        """``restore`` must not delegate to ``_phase0-stub`` anymore."""
        text = self._self_hosted_text()
        match = re.search(
            r"^restore path=\"\":\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None, (
            'Public ``restore path="":`` recipe missing from just/self-hosted/mod.just.'
        )
        body = match.group(1)
        assert "_phase0-stub" not in body, (
            "``restore`` is still a Phase 0 stub. Phase 3 wires the real "
            "orchestration through _run-restore."
        )
        assert "_run-restore" in body

    def test_backup_orchestration_writes_manifest_with_self_hosted_target(self):
        """Backup must invoke write_backup_manifest with the self-hosted target.

        Cross-target restore tooling reads ``manifest.json`` and
        branches on ``target`` to know whether to ``gcloud sql
        import`` (gcp) or ``psql`` (self_hosted). The ``--target``
        flag in the writer call is the contract.
        """
        text = self._self_hosted_text()
        # The block between _run-backup: and the next public recipe header.
        match = re.search(
            r"^_run-backup:\n((?:    .*\n|    \n|\n)*?)^# ",
            text,
            re.MULTILINE,
        )
        assert match is not None, "_run-backup helper missing."
        body = match.group(1)
        assert "write_backup_manifest" in body
        assert "--target self_hosted" in body
        assert "validibot.backup.v1" not in body, (
            "Don't hard-code the schema version in the recipe — let the "
            "manifest writer's BackupManifest default supply it."
        )

    def test_backup_orchestration_uses_zstd_compression(self):
        """ADR section 8 mandates zstd compression for both DB dump and data archive."""
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-backup:\n((?:    .*\n|    \n|\n)*?)^# ",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert "zstd" in body, "DB dump must be compressed with zstd (ADR section 8)."
        assert "tar --zstd" in body, "Data archive must be tar --zstd (ADR section 8)."

    def test_restore_orchestration_runs_four_preflight_gates(self):
        """The restore recipe must run the four pre-flight gates GCP runs.

        ADR section 8 + AC #15-#16: destructive operations refuse on
        unhealthy deployments, incompatible backups, or tampered
        artifacts. The four gates report progress so an operator
        watching the recipe sees what it's checking and why.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-restore path:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "_run-restore helper missing."
        body = match.group(1)
        # The four numbered gates are operator-visible signals.
        assert "Pre-flight 1/4" in body
        assert "Pre-flight 2/4" in body
        assert "Pre-flight 3/4" in body
        assert "Pre-flight 4/4" in body
        # Specific tools per gate.
        assert "doctor --strict" in body, (
            "Pre-flight gate 2 must invoke doctor --strict (AC #15)."
        )
        assert "verify_backup_compatibility" in body, (
            "Pre-flight gate 3 must invoke verify_backup_compatibility (AC #16)."
        )
        assert "sha256sum" in body, (
            "Pre-flight gate 4 must verify DB sha256 against the manifest."
        )

    def test_restore_orchestration_writes_last_restore_test_marker(self):
        """Doctor's VB411 expects ``.last-restore-test`` to be touched on restore.

        Without this write, every install warns on VB411 forever
        — the marker is the signal that "this deployment has done
        a restore drill and it worked." See doctor's _check_restore_test.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-restore path:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert ".last-restore-test" in body, (
            "Restore must touch ``.last-restore-test`` so doctor's VB411 "
            "can compute restore-test staleness. See "
            "validibot/core/management/commands/check_validibot.py."
        )

    def test_restore_orchestration_demands_operator_confirmation(self):
        """Restore is destructive — the recipe must prompt before proceeding.

        The GCP recipe asks the operator to type the stage name; the
        self-hosted equivalent asks for the short hostname. Either way,
        an accidental ``just self-hosted restore <typo>`` typed by
        mistake stops at the prompt rather than nuking data.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-restore path:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert "DESTRUCTIVE OPERATION" in body
        assert "read -r CONFIRM" in body or "read -r CONFIRM" in body

    def test_django_runtime_image_installs_zstd(self):
        """The web container needs zstd so ``tar --zstd`` works.

        ``tar --zstd`` shells out to the zstd binary; without it the
        backup's data-archive step fails immediately. Adding zstd to
        the Dockerfile is small (~600KB) and prevents a class of
        silent backup failures.
        """
        text = self.DJANGO_DOCKERFILE.read_text(encoding="utf-8")
        # Look for zstd in any apt-get install RUN block in the runtime
        # stage. We don't pin the exact line number because that drifts.
        runtime_section = text.split("FROM python:3.13-slim-bookworm", 1)[-1]
        assert "zstd" in runtime_section, (
            "compose/production/django/Dockerfile runtime stage must "
            "install zstd. Without it ``tar --zstd`` inside the web "
            "container fails, which breaks ``just self-hosted backup``."
        )

    def test_self_hosted_builds_stamp_validibot_version(self):
        """Compose builds must bake the release tag into the runtime image.

        Backup manifests are written from inside the running web
        container. If the image is built without ``VALIDIBOT_VERSION``,
        restore compatibility falls back to package metadata and loses
        the explicit release tag the operator built.
        """
        just_text = self._self_hosted_text()
        compose_text = (REPO_ROOT / "docker-compose.production.yml").read_text(
            encoding="utf-8"
        )

        version_export = (
            'export VALIDIBOT_VERSION="${VALIDIBOT_VERSION:-{{validibot_version}}}"'
        )
        assert version_export in just_text
        assert "VALIDIBOT_VERSION: ${VALIDIBOT_VERSION:-}" in compose_text
        assert "VALIDIBOT_REVISION: ${VALIDIBOT_REVISION:-}" in compose_text

    def test_self_hosted_validator_build_labels_backend_images(self):
        """Locally built validator images carry OCI metadata via Dockerfile defaults.

        The wrapper version is baked into the Dockerfile's
        ``ARG VALIDATOR_BACKEND_VERSION`` default; the recipe only
        passes the build-arg when an operator overrides via env var.
        We assert the override path is wired AND that the recipe
        doesn't reach for the deleted resolver script.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^validator-build name:\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        # The deleted resolver script must not be referenced.
        assert "resolve-backend-image-version.py" not in body
        # OCI revision + slug args still per build.
        assert "VALIDATOR_BACKEND_REVISION" in body
        # Conditional override only passes --build-arg when the env
        # var is set; otherwise Dockerfile default wins.
        assert "${VALIDATOR_BACKEND_VERSION:+--build-arg" in body
        assert "{{validator_backend_git_sha}}" in body
        # Legacy app-version env var must not appear.
        assert "VALIDATOR_VERSION=" not in body


class UpgradeRecipeShapeTests(SimpleTestCase):
    """Verify Phase 4 upgrade recipes are real implementations.

    Phase 4 of ADR-2026-04-27 introduces the manifested upgrade —
    version-pinned, gated by doctor --strict and a four-step
    pre-flight, mandatory backup unless --no-backup, post-flight
    doctor + smoke-test, and a ``validibot.upgrade.v1`` report. These
    tests pin the operator-facing contract on the recipe-shape level
    (running the actual upgrade requires a live Compose stack, which
    a unit test can't provide).

    Specifically:

    1. The legacy ``update`` recipe is deprecated and no longer pulls
       latest unconditionally — it points operators at ``upgrade``.
    2. ``upgrade`` is no longer a Phase 0 stub.
    3. The four pre-flight gates (doctor --strict, clean tree, target
       tag exists, version path validation) all appear.
    4. The recipe calls the manifested ``backup`` (Phase 3) — not
       ``backup-db`` (the legacy db-only dump).
    5. Post-flight ``doctor`` and ``smoke-test`` run.
    6. The report schema is ``validibot.upgrade.v1`` (matches the
       backup-manifest versioning convention).
    7. ``clean-all`` got the same destructive-confirmation pattern
       (typing the short hostname unlocks the wipe).
    8. GCP has a paired ``upgrade <stage> <version>`` recipe that
       reuses ``deploy-all`` as its build+migrate+deploy step.
    """

    SELF_HOSTED_MOD = REPO_ROOT / "just" / "self-hosted" / "mod.just"
    GCP_MOD = REPO_ROOT / "just" / "gcp" / "mod.just"

    def _self_hosted_text(self) -> str:
        return self.SELF_HOSTED_MOD.read_text(encoding="utf-8")

    def _gcp_text(self) -> str:
        return self.GCP_MOD.read_text(encoding="utf-8")

    def test_update_is_deprecated(self):
        """``update`` no longer runs the old pull-latest flow."""
        text = self._self_hosted_text()
        match = re.search(
            r"^update:\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "update recipe missing"
        body = match.group(1)
        assert "deprecated" in body.lower()
        assert "upgrade --to" in body
        assert "git pull" not in body
        assert "backup-db" not in body

    def test_upgrade_recipe_no_longer_stub(self):
        """``upgrade`` must not delegate to ``_phase0-stub`` anymore."""
        text = self._self_hosted_text()
        match = re.search(
            r"^upgrade \*args:\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "upgrade recipe missing"
        body = match.group(1)
        assert "_phase0-stub" not in body
        assert "_run-upgrade" in body

    def test_upgrade_runs_four_preflight_gates(self):
        """All four pre-flight gates must be present in _run-upgrade.

        ADR section 9 + AC #15-#16: destructive operations gate on
        doctor --strict, clean working tree, target tag existence,
        and cross-major-version refusal. The four gates report as
        Pre-flight 1/4 through 4/4 so an operator following along
        sees exactly what's checked.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-upgrade args:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "_run-upgrade helper missing"
        body = match.group(1)
        assert "Pre-flight 1/4" in body
        assert "Pre-flight 2/4" in body
        assert "Pre-flight 3/4" in body
        assert "Pre-flight 4/4" in body
        assert "doctor --strict" in body
        assert "git diff --quiet HEAD" in body
        assert "CURRENT_MAJOR" in body
        assert "TARGET_MAJOR" in body

    def test_upgrade_uses_manifested_backup(self):
        """Pre-upgrade backup must call the Phase 3 manifested backup."""
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-upgrade args:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert "just self-hosted backup" in body
        assert "backup-db" not in body, (
            "Pre-upgrade backup must use the manifested ``backup``, "
            "not the legacy ``backup-db``."
        )

    def test_upgrade_runs_postflight_doctor_and_smoke_test(self):
        """Post-flight: doctor + smoke-test, both reused from existing recipes."""
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-upgrade args:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert "just self-hosted doctor" in body
        assert "just self-hosted smoke-test" in body

    def test_upgrade_writes_versioned_report_schema(self):
        """Upgrade report uses ``validibot.upgrade.v1``.

        Same versioning convention as backup manifests and doctor
        output. Schema is part of the operator-facing contract.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-upgrade args:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert "validibot.upgrade.v1" in body
        assert "report.json" in body

    def test_clean_all_demands_hostname_confirmation(self):
        """``clean-all`` must require typing the short hostname."""
        text = self._self_hosted_text()
        match = re.search(
            r"^clean-all:\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "clean-all recipe missing"
        body = match.group(1)
        assert "DESTRUCTIVE OPERATION" in body
        assert "hostname" in body.lower()
        assert "read -r CONFIRM" in body

    def test_gcp_upgrade_recipe_exists(self):
        """``just gcp upgrade <stage> <version>`` must exist for parity."""
        text = self._gcp_text()
        match = re.search(
            r"^upgrade stage version \*flags:.*$",
            text,
            re.MULTILINE,
        )
        assert match is not None, "GCP upgrade recipe missing"

    def test_gcp_upgrade_reuses_deploy_all(self):
        """GCP upgrade must call the existing ``deploy-all`` recipe.

        Reusing ``deploy-all`` keeps the build + push + migrate +
        scheduler-setup logic in one place. The upgrade recipe is
        a wrapper that adds the gates around it.
        """
        text = self._gcp_text()
        match = re.search(
            r"^_run-upgrade stage version flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "GCP _run-upgrade helper missing"
        body = match.group(1)
        assert "just gcp deploy-all" in body
        assert "just gcp doctor" in body
        assert "just gcp smoke-test" in body
        assert "just gcp backup" in body

    # ── Version-override path (Option C from the second-review pass) ──
    #
    # Concept being tested: the *default* version-stamping path reads
    # pyproject.toml at justfile-load time and bakes the result into a
    # ``{{validibot_version}}`` constant. That constant is captured
    # BEFORE any recipe body runs, so by the time an upgrade recipe
    # has done ``git checkout vX.Y.Z`` the constant still reflects
    # the OLD pyproject. Without an override, the resulting build
    # would be tagged with the pre-upgrade version — exactly the
    # version-provenance failure mode the review flagged.
    #
    # The fix is a shell-level override: each upgrade recipe exports
    # ``VALIDIBOT_VERSION="${TARGET}"`` after checkout, and every
    # build/deploy line reads ``${VALIDIBOT_VERSION:-{{validibot_version}}}``
    # so the explicit operator-supplied target wins, falling back to
    # the pyproject default for daily deploys.
    #
    # These tests pin the override path on the recipe shape because
    # the upgrade flow can't run end-to-end in a unit test (it needs
    # docker compose / Cloud Run + a real backup target).

    def test_self_hosted_upgrade_pins_validibot_version_to_target(self):
        """Self-hosted upgrade must export VALIDIBOT_VERSION=<target>.

        The export happens between Step 2/7 (git checkout) and
        Step 3/7 (build), so the docker compose build picks up the
        operator-supplied tag rather than the pre-checkout
        ``{{validibot_version}}`` constant.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-upgrade args:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "_run-upgrade helper missing"
        body = match.group(1)

        export_line = 'export VALIDIBOT_VERSION="${TARGET}"'
        assert export_line in body, (
            "Self-hosted _run-upgrade must export VALIDIBOT_VERSION "
            "to the operator-supplied target so the build stamps the "
            "correct version. Without this, the parse-time constant "
            "{{validibot_version}} would still hold the OLD version "
            "(parsed before git checkout ran)."
        )

        checkout_idx = body.index('git checkout --quiet "${TARGET}"')
        export_idx = body.index(export_line)
        # Match the actual ``docker compose ... build`` command line
        # rather than the substring ``docker compose`` (which also
        # appears in explanatory comments). The justfile interpolation
        # ``{{compose_env_args}}`` only ever lives on real command
        # lines, never in prose, so it gives us a clean anchor.
        build_idx = body.index("docker compose {{compose_env_args}}")
        assert checkout_idx < export_idx < build_idx, (
            "VALIDIBOT_VERSION export must sit AFTER ``git checkout`` "
            "(pyproject is now at the new version) and BEFORE the "
            "``docker compose ... build`` (so the build picks it up)."
        )

    def test_self_hosted_upgrade_pins_validibot_revision_to_post_checkout_head(self):
        """Self-hosted upgrade must also export VALIDIBOT_REVISION post-checkout.

        Mirrors the VALIDIBOT_VERSION override but for the commit
        revision label. ``{{git_sha}}`` is captured at parse time
        from pre-checkout HEAD, so without this re-export the image
        would have the right release version label but the OLD
        commit revision label — what the second-pass review caught.

        We assert ``$(git rev-parse --short HEAD)`` rather than the
        target tag because tags can point at any commit (and tag
        objects have their own SHA distinct from the commit SHA).
        Re-running rev-parse at this point in the recipe guarantees
        the LABEL matches the actual checked-out commit.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-upgrade args:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "_run-upgrade helper missing"
        body = match.group(1)

        revision_export = 'export VALIDIBOT_REVISION="$(git rev-parse --short HEAD)"'
        assert revision_export in body, (
            "Self-hosted _run-upgrade must export VALIDIBOT_REVISION "
            "to the post-checkout HEAD sha so the image's commit "
            "revision label matches the checked-out commit. Without "
            "this, ``{{git_sha}}`` (captured at parse time before "
            "checkout) would still hold the OLD commit, leaving "
            "the image labels internally inconsistent."
        )

        checkout_idx = body.index('git checkout --quiet "${TARGET}"')
        revision_idx = body.index(revision_export)
        build_idx = body.index("docker compose {{compose_env_args}}")
        assert checkout_idx < revision_idx < build_idx, (
            "VALIDIBOT_REVISION export must sit AFTER ``git checkout`` "
            "(otherwise rev-parse returns the OLD HEAD) and BEFORE "
            "the build (so the build picks it up)."
        )

    def test_self_hosted_load_build_env_honours_validibot_revision_override(self):
        """``_load-build-env`` must echo the override-aware revision form.

        ``${VALIDIBOT_REVISION:-{{git_sha}}}`` is the form that lets
        the upgrade recipe's ``export VALIDIBOT_REVISION=$(git ...)``
        propagate through ``eval $(just self-hosted _load-build-env)``
        into the docker compose build. If this regressed to a literal
        ``{{git_sha}}``, the export above would be silently dropped.
        """
        text = self._self_hosted_text()
        override_form = 'export VALIDIBOT_REVISION="${VALIDIBOT_REVISION:-{{git_sha}}}"'
        assert override_form in text, (
            "Self-hosted _load-build-env must echo the override-aware "
            "form for VALIDIBOT_REVISION so the upgrade recipe's "
            "post-checkout export propagates through into the docker "
            "compose build."
        )

    def test_self_hosted_load_build_env_honours_validibot_version_override(self):
        """The compose env loader must respect a pre-set VALIDIBOT_VERSION.

        ``_load-build-env`` is the helper the build recipe ``eval``s
        to pull host env into compose. If it hard-coded the parse-time
        constant, the upgrade-recipe export above would be ignored.
        The ``${VAR:-default}`` shape lets the override flow through
        while keeping pyproject as the default for daily deploys.
        """
        text = self._self_hosted_text()
        # Match either inside _load-build-env or a ``just self-hosted``
        # echo line — the exact spelling has shifted across edits.
        override_form = (
            'export VALIDIBOT_VERSION="${VALIDIBOT_VERSION:-{{validibot_version}}}"'
        )
        assert override_form in text, (
            "Self-hosted _load-build-env must echo the override-aware "
            "form so the upgrade recipe's VALIDIBOT_VERSION export "
            "propagates through ``eval $(just self-hosted "
            "_load-build-env)`` into the docker compose build."
        )

    def test_gcp_upgrade_pins_validibot_version_to_target(self):
        """GCP upgrade must export VALIDIBOT_VERSION=<target> after checkout.

        Mirrors the self-hosted contract: between Step 2/4 (git
        checkout) and Step 3/4 (deploy-all), the recipe exports the
        operator-supplied target so the build / web / worker layers
        all stamp the right version.
        """
        text = self._gcp_text()
        match = re.search(
            r"^_run-upgrade stage version flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "GCP _run-upgrade helper missing"
        body = match.group(1)

        export_line = 'export VALIDIBOT_VERSION="${TARGET}"'
        assert export_line in body, (
            "GCP _run-upgrade must export VALIDIBOT_VERSION to the "
            "operator-supplied target so deploy-all (which calls "
            "build, _deploy-web, _deploy-worker) stamps the correct "
            "version. Without this, the parse-time "
            "{{validibot_version}} constant would still hold the "
            "OLD version."
        )

        checkout_idx = body.index('git checkout --quiet "${TARGET}"')
        export_idx = body.index(export_line)
        deploy_idx = body.index("just gcp deploy-all")
        assert checkout_idx < export_idx < deploy_idx, (
            "VALIDIBOT_VERSION export must sit AFTER ``git checkout`` "
            "and BEFORE ``just gcp deploy-all`` so the deploy chain "
            "(build → web → worker) sees the operator-supplied target."
        )

    def test_gcp_upgrade_pins_validibot_revision_to_post_checkout_head(self):
        """GCP upgrade must also export VALIDIBOT_REVISION post-checkout.

        Same failure mode as self-hosted: ``{{git_sha}}`` is captured
        at parse time, so an upgrade build without this re-export
        would carry the right release version label but the OLD
        commit revision label.

        Note: this test asserts the post-checkout export only. The
        exported value feeds both the in-image LABEL and the Cloud
        Run image TAG, since both now thread the same
        ``${VALIDIBOT_REVISION:-{{git_sha}}}`` override form. See
        ``test_gcp_image_tag_uses_revision_override`` further down
        for the image-tag pinning.
        """
        text = self._gcp_text()
        match = re.search(
            r"^_run-upgrade stage version flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "GCP _run-upgrade helper missing"
        body = match.group(1)

        revision_export = 'export VALIDIBOT_REVISION="$(git rev-parse --short HEAD)"'
        assert revision_export in body, (
            "GCP _run-upgrade must export VALIDIBOT_REVISION to the "
            "post-checkout HEAD sha so the in-image LABEL matches "
            "the checked-out commit. Without this, ``{{git_sha}}`` "
            "captured at parse time would still hold the OLD HEAD."
        )

        checkout_idx = body.index('git checkout --quiet "${TARGET}"')
        revision_idx = body.index(revision_export)
        deploy_idx = body.index("just gcp deploy-all")
        assert checkout_idx < revision_idx < deploy_idx, (
            "VALIDIBOT_REVISION export must sit AFTER ``git checkout`` "
            "and BEFORE ``just gcp deploy-all`` so the build picks "
            "up the post-checkout sha."
        )

    def test_gcp_build_recipe_honours_validibot_revision_override(self):
        """GCP build must use ${VALIDIBOT_REVISION:-default} for the LABEL.

        If the build recipe hard-codes ``{{git_sha}}`` in the
        ``--build-arg VALIDIBOT_REVISION`` line, the export from
        ``_run-upgrade`` would be silently dropped and the upgrade
        build would land with the OLD commit revision label.
        """
        text = self._gcp_text()
        override_form = (
            '--build-arg VALIDIBOT_REVISION="${VALIDIBOT_REVISION:-{{git_sha}}}"'
        )
        assert override_form in text, (
            "GCP build recipe must use the override-aware form "
            f"``{override_form}`` so the upgrade recipe's "
            "post-checkout VALIDIBOT_REVISION export propagates into "
            "the image LABEL. The literal ``{{git_sha}}`` form would "
            "silently drop the override."
        )

    def test_gcp_build_and_deploy_honour_validibot_version_override(self):
        """GCP build + deploy lines must use ${VALIDIBOT_VERSION:-default}.

        If the build/deploy lines hard-coded ``{{validibot_version}}``,
        the export from ``_run-upgrade`` would be silently ignored
        and the upgrade would produce a mis-stamped image — the
        provenance bug Option C exists to prevent.
        """
        text = self._gcp_text()
        override_form = "${VALIDIBOT_VERSION:-{{validibot_version}}}"

        # The override-aware form must appear in three places: the
        # build recipe (image label), _deploy-web (runtime env), and
        # _deploy-worker (runtime env). A separate count check makes
        # the failure mode clear if any one of the three drifts back
        # to the literal ``{{validibot_version}}`` form.
        expected_override_sites = 3  # build, _deploy-web, _deploy-worker
        occurrences = text.count(override_form)
        assert occurrences >= expected_override_sites, (
            f"Expected the override-aware form "
            f"``{override_form}`` to appear in at least "
            f"{expected_override_sites} spots (build, _deploy-web, "
            f"_deploy-worker). Found {occurrences}. Whichever spot "
            f"is missing will silently drop back to the parse-time "
            f"{{validibot_version}} constant, mis-stamping upgrade "
            f"builds."
        )

    # ── Image-tag threading (third-pass review P1 fix) ────────────────
    #
    # Concept being tested: the registry image TAG (not just the
    # in-image LABEL) must move with the actual code. Daily deploys
    # use the parse-time ``{{git_sha}}``; upgrades use the post-
    # checkout sha via the same ``${VALIDIBOT_REVISION:-{{git_sha}}}``
    # override mechanism we already use for the LABEL.
    #
    # Why this matters: container tags are mutable pointers. If
    # ``image:AAAAAA`` was previously deployed with one set of code,
    # and an upgrade re-pushes new code under the same tag string,
    # the registry silently re-points the tag — breaking rollback
    # ("redeploy image:AAAAAA" no longer returns the same code) and
    # confusing forensics ("what's actually at image:AAAAAA?").
    #
    # The fix threads ``IMAGE_TAG="${VALIDIBOT_REVISION:-{{git_sha}}}"``
    # through every recipe that references the app image:
    # ``build``, ``push``, ``_deploy-web``, ``_deploy-worker``,
    # ``_migrate``. Operator-job recipes (``_run-doctor-job``,
    # ``_run-backup``, ``_run-restore``) resolve the deployed image
    # at runtime via ``_resolve-deployed-image``, so they don't
    # need this override.

    def test_gcp_image_tag_uses_revision_override(self):
        """All app-image references must thread the IMAGE_TAG override.

        We assert by counting two distinct shapes:

        - ``IMAGE_TAG="${VALIDIBOT_REVISION:-{{git_sha}}}"`` — the
          local-variable form used by bash recipes (build,
          _deploy-web, _deploy-worker, _migrate).
        - ``${VALIDIBOT_REVISION:-{{git_sha}}}`` inlined directly —
          the form used by the non-bash ``push`` recipe (each line
          is its own shell command, so no shared local variable).

        Either form means the recipe will pick up the override; the
        regression we're guarding against is a bare ``{{git_sha}}``
        sneaking back in via copy-paste from an older recipe shape.
        """
        text = self._gcp_text()
        local_var_form = 'IMAGE_TAG="${VALIDIBOT_REVISION:-{{git_sha}}}"'
        inline_form = "${VALIDIBOT_REVISION:-{{git_sha}}}"

        # Local-variable form should appear in 4 bash recipes:
        # build, _deploy-web, _deploy-worker, _migrate.
        expected_bash_sites = 4
        local_var_count = text.count(local_var_form)
        assert local_var_count >= expected_bash_sites, (
            f"Expected ``IMAGE_TAG=...`` to appear in at least "
            f"{expected_bash_sites} bash recipes (build, _deploy-web, "
            f"_deploy-worker, _migrate). Found {local_var_count}. "
            f"Whichever recipe is missing will tag/deploy the image "
            f"with the parse-time {{git_sha}}, breaking upgrade "
            f"provenance."
        )

        # Inline form must appear at least 5 times (the ones inside
        # IMAGE_TAG="..." lines plus 2 additional in the ``push``
        # recipe — the echo and the docker push line).
        expected_total_sites = 6  # 4 bash + 2 push lines
        inline_count = text.count(inline_form)
        assert inline_count >= expected_total_sites, (
            f"Expected ``${inline_form}`` to appear in at least "
            f"{expected_total_sites} spots total (4 bash recipes + "
            f"the 2 lines in ``push``). Found {inline_count}. The "
            f"non-bash push recipe in particular needs the inlined "
            f"form because each line runs as its own shell command."
        )

    def test_gcp_app_image_tag_uses_imagetag_var_in_bash_recipes(self):
        """Bash recipes must reference the image as ``{{gcp_image}}:${IMAGE_TAG}``.

        The local variable is only useful if it's actually consumed.
        We grep for the post-fix usage shape and count the call sites:

        - ``-t {{gcp_image}}:${IMAGE_TAG}`` in build (one tag line;
          the ``:latest`` line stays unchanged because ``latest`` is
          a deliberately moving tag).
        - ``--image {{gcp_image}}:${IMAGE_TAG}`` in _deploy-web,
          _deploy-worker, and the migrate-job hand-off.
        - ``"{{gcp_image}}:${IMAGE_TAG}"`` (quoted) in _migrate's
          call to _run-migrate-job.

        Total: at least 4 sites must use the ``${IMAGE_TAG}`` form.
        """
        text = self._gcp_text()
        usage_form = "{{gcp_image}}:${IMAGE_TAG}"
        expected_consumer_sites = 4
        usage_count = text.count(usage_form)
        assert usage_count >= expected_consumer_sites, (
            f"Expected ``{usage_form}`` to appear in at least "
            f"{expected_consumer_sites} sites (build -t, "
            f"_deploy-web --image, _deploy-worker --image, "
            f"_migrate's _run-migrate-job hand-off). Found "
            f"{usage_count}. The IMAGE_TAG variable is set up at "
            f"the top of each bash recipe but only matters if the "
            f"actual image reference uses it."
        )

    def test_gcp_no_bare_git_sha_in_app_image_tag(self):
        """No remaining bare ``{{gcp_image}}:{{git_sha}}`` references.

        This is the regression-guard: the literal form was the
        broken-tag-overwrite shape that the third-pass review
        flagged. Any new recipe that needs to reference the app
        image must either use the IMAGE_TAG variable (bash recipes)
        or the inlined ``${VALIDIBOT_REVISION:-{{git_sha}}}`` form
        (non-bash recipes). The literal must not return.

        ``{{gcp_image}}:latest`` is fine — ``:latest`` is a moving
        "most recent build" tag by convention; the override only
        applies to per-commit tags.
        """
        text = self._gcp_text()
        broken_form = "{{gcp_image}}:{{git_sha}}"
        non_comment_lines = [
            line
            for line in text.splitlines()
            # Strip recipe-body indentation; comment lines start with
            # ``#`` after stripping. We deliberately allow comments
            # to mention the broken form (they explain the history).
            if not line.lstrip().startswith("#")
        ]
        offenders = [line for line in non_comment_lines if broken_form in line]
        assert not offenders, (
            f"Found {len(offenders)} non-comment line(s) still "
            f"using the broken ``{broken_form}`` form. These will "
            f"silently re-point registry tags during upgrade builds, "
            f"breaking rollback by tag:\n"
            + "\n".join(f"  - {line.strip()}" for line in offenders)
        )


class ValidatorsAndCleanupShapeTests(SimpleTestCase):
    """Verify Phase 5 validators + cleanup recipes are real implementations.

    Phase 5 of ADR-2026-04-27 introduces:

    1. ``just self-hosted validators`` — local Docker daemon
       inventory that surfaces the OCI version label
       (``org.opencontainers.image.version``) added by the
       VALIDATOR_VERSION-cleanup work earlier this session.
    2. ``just self-hosted cleanup`` and ``just gcp cleanup <stage>``
       — Discourse-launcher-style prune of artefacts that aren't
       part of any working set.

    These tests pin the contract on the recipe-shape level. Actually
    running cleanup against a live Docker daemon requires
    integration-level fixtures; we cover the orchestration shape
    here and rely on operator-level walkthroughs for end-to-end.
    """

    SELF_HOSTED_MOD = REPO_ROOT / "just" / "self-hosted" / "mod.just"
    GCP_MOD = REPO_ROOT / "just" / "gcp" / "mod.just"

    def _self_hosted_text(self) -> str:
        return self.SELF_HOSTED_MOD.read_text(encoding="utf-8")

    def _gcp_text(self) -> str:
        return self.GCP_MOD.read_text(encoding="utf-8")

    def test_self_hosted_validators_recipe_no_longer_stub(self):
        """``validators`` must not delegate to ``_phase0-stub`` anymore."""
        text = self._self_hosted_text()
        # Capture the full recipe body. Just recipe bodies have indented
        # lines; blank lines inside the body are bare ``\n`` (no
        # indentation). The body ends at the next recipe declaration
        # (a non-indented line that isn't blank) or end-of-file.
        match = re.search(
            r"^validators:\n((?:    .*\n|\n)*?)(?=^[A-Za-z_#\[]|\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "self-hosted validators recipe missing"
        body = match.group(1)
        assert "_phase0-stub" not in body
        # The recipe must query the local Docker daemon.
        assert "docker image ls" in body

    def test_self_hosted_validators_reads_oci_version_label(self):
        """The inventory must surface ``org.opencontainers.image.version``.

        That label is the operator-readable backend version (e.g.
        EnergyPlus 25.2.0). Showing only the digest would mean
        operators can't tell at a glance which version of EnergyPlus
        is installed.
        """
        text = self._self_hosted_text()
        # Capture the full recipe body. Just recipe bodies have indented
        # lines; blank lines inside the body are bare ``\n`` (no
        # indentation). The body ends at the next recipe declaration
        # (a non-indented line that isn't blank) or end-of-file.
        match = re.search(
            r"^validators:\n((?:    .*\n|\n)*?)(?=^[A-Za-z_#\[]|\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert "org.opencontainers.image.version" in body
        # Filter on the validator-backend repository convention so
        # other unrelated images don't appear in the listing.
        assert "validibot-validator-backend-*" in body

    def test_self_hosted_cleanup_recipe_no_longer_stub(self):
        """``cleanup`` must not delegate to ``_phase0-stub``."""
        text = self._self_hosted_text()
        match = re.search(
            r"^cleanup \*flags:\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "self-hosted cleanup recipe missing"
        body = match.group(1)
        assert "_phase0-stub" not in body
        assert "_run-cleanup" in body

    def test_self_hosted_cleanup_supports_dry_run_and_yes(self):
        """``cleanup`` must accept ``--dry-run`` and ``--yes`` flags.

        The operator UX contract: dry-run lists candidates without
        deletion (safe to run any time); --yes skips the
        confirmation prompt for cron-friendly automation.
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-cleanup flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "_run-cleanup helper missing"
        body = match.group(1)
        assert "--dry-run" in body
        assert "--yes" in body

    def test_self_hosted_cleanup_three_retention_scopes(self):
        """Cleanup walks three retention scopes per ADR Phase 5."""
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-cleanup flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        # Validator container + backup + upgrade-report retention,
        # each with a configurable env-var window.
        assert "VALIDATOR_RETAIN_HOURS" in body
        assert "BACKUP_RETAIN_DAYS" in body
        assert "UPGRADE_REPORT_RETAIN_DAYS" in body
        # Dangling images get a bonus pass — always safe to remove.
        assert "dangling" in body.lower()

    def test_self_hosted_cleanup_shows_candidates_before_deleting(self):
        """Cleanup must list candidates before any destructive action.

        Pattern from Discourse's launcher cleanup. Even without
        ``--dry-run`` the recipe shows what would be removed and
        prompts for confirmation. That's what makes it safe to run
        on a schedule (operators inspect the cron log to see what
        was cleaned, not "did anything bad happen?").
        """
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-cleanup flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        # Each scope prints "Scope N/3: ..." with the candidate
        # listing before any deletion happens; the confirmation
        # prompt comes after.
        assert "Scope 1/3" in body
        assert "Scope 2/3" in body
        assert "Scope 3/3" in body
        assert "read -r CONFIRM" in body

    def test_gcp_cleanup_recipe_exists(self):
        """``just gcp cleanup <stage>`` exists for cross-target parity."""
        text = self._gcp_text()
        match = re.search(
            r"^cleanup stage \*flags:.*$",
            text,
            re.MULTILINE,
        )
        assert match is not None, "GCP cleanup recipe missing"

    def test_gcp_cleanup_targets_executions_and_gcs_backups(self):
        """GCP cleanup prunes Cloud Run Job executions + expired GCS backups."""
        text = self._gcp_text()
        match = re.search(
            r"^_run-cleanup stage flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "GCP _run-cleanup helper missing"
        body = match.group(1)
        # Two scopes: Cloud Run Job executions and GCS backup objects.
        assert "Scope 1/2" in body
        assert "Scope 2/2" in body
        assert "gcloud run jobs executions" in body
        assert "gcloud storage" in body
        # Same retention env vars + dry-run UX as self-hosted.
        assert "EXECUTION_RETAIN_DAYS" in body
        assert "BACKUP_RETAIN_DAYS" in body
        assert "--dry-run" in body
        assert "--yes" in body

    def test_gcp_cleanup_is_stage_scoped(self):
        """``cleanup <stage>`` must only touch jobs belonging to that stage.

        Earlier the recipe listed every execution in the project /
        region and only filtered by age. That meant ``cleanup dev``
        could delete prod execution history — a real cross-stage
        blast-radius bug.

        The fix builds stage-specific job-name regexes and applies
        them in the jq filter. Prod includes ``^<app>-`` and excludes
        anything ending in ``-dev`` or ``-staging`` (since prod jobs
        are unsuffixed). Non-prod includes ``^<app>-.*-{stage}$``.
        """
        text = self._gcp_text()
        match = re.search(
            r"^_run-cleanup stage flags:\n((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        # Stage-pattern variables MUST be set, otherwise the filter
        # has nothing to constrain on.
        assert "STAGE_INCLUDE_RE" in body, (
            "GCP cleanup must build a stage-include regex; without it "
            "every execution in the project gets matched."
        )
        assert "STAGE_EXCLUDE_RE" in body, (
            "GCP cleanup needs the prod-exclude regex to reject "
            "dev/staging-suffixed jobs from the prod set (since prod "
            "jobs are unsuffixed)."
        )
        # The jq filter must reference both regexes.
        assert "test($include)" in body
        assert "test($exclude)" in body
        # The stage label must appear in the listing prefix so
        # operators see what stage they're cleaning.
        assert "for {{stage}} older than" in body


class VersionResolutionShapeTests(SimpleTestCase):
    """Verify ``validibot_version`` resolves from pyproject.toml.

    Version-stamping policy (deliberate, repeated across reviews):
    ──────────────────────────────────────────────────────────────
    Every default deploy path — daily ``deploy`` / ``bootstrap`` /
    ``deploy-all`` against any stage — reads the runtime version from
    pyproject.toml, not from "the latest git tag". This is the policy
    chosen for the project after evaluating both options. Reasons
    pyproject wins:

    1. pyproject.toml is the canonical source already shipped in
       every checkout — no extra script, no ``git fetch --tags``
       precondition. Behaviour is identical on a fresh CI clone, a
       shallow checkout, or a tag-less branch.
    2. Tags are operator metadata; they can drift behind the
       checked-out commit. A "latest tag" resolver would silently
       change behaviour the moment somebody cuts a tag — backup-
       manifest provenance gets harder to reason about under that.
    3. The "exact release tag" use case is handled by the upgrade
       recipes via env-var override (``VALIDIBOT_VERSION="${TARGET}"``
       after ``git checkout``); see ``UpgradeRecipeShapeTests``.

    History: this used to live in ``scripts/resolve-git-tag-version.sh``
    (latest tag with pyproject fallback). That layer was removed in
    Phase 5 because shelling out from a justfile creates a path-
    resolution headache, and the .sh fallback existed only because
    shallow git exports might lack tags — checkouts always have
    pyproject.toml.

    These tests pin that the simplified approach lands cleanly:

    - all three justfile modules (``common``, ``self-hosted``, ``gcp``)
      read directly from pyproject.toml, not from a script;
    - the script and its test file are gone;
    - the resolved version starts with ``v`` (matches OCI label and
      backup-manifest convention).
    """

    SELF_HOSTED_MOD = REPO_ROOT / "just" / "self-hosted" / "mod.just"
    GCP_MOD = REPO_ROOT / "just" / "gcp" / "mod.just"
    COMMON = REPO_ROOT / "just" / "common.just"

    def test_resolve_git_tag_version_script_is_gone(self):
        """The legacy script must not return — no stub, no symlink."""
        legacy = REPO_ROOT / "scripts" / "resolve-git-tag-version.sh"
        assert not legacy.exists(), (
            f"{legacy} should have been removed in Phase 5. The "
            "version is now read directly from pyproject.toml."
        )

    def test_no_module_calls_the_legacy_script(self):
        """No just module should reference the deleted script."""
        for mod in (self.COMMON, self.SELF_HOSTED_MOD, self.GCP_MOD):
            text = mod.read_text(encoding="utf-8")
            assert "resolve-git-tag-version.sh" not in text, (
                f"{mod} still references the deleted resolver script."
            )

    def test_modules_read_pyproject_directly(self):
        """The three modules each compute ``validibot_version`` from pyproject.toml.

        Using ``git rev-parse --show-toplevel`` rather than a relative
        path or ``{{justfile_directory()}}`` is intentional: just
        substitutes ``{{...}}`` in recipes but not in constant-
        assignment backticks, and a relative path is fragile across
        ``just`` invocation contexts (different cwd, module nesting).
        """
        for mod in (self.COMMON, self.SELF_HOSTED_MOD, self.GCP_MOD):
            text = mod.read_text(encoding="utf-8")
            # Each module declares its own validibot_version because
            # just modules have isolated scope — that's a known just
            # limitation, not a code-smell.
            match = re.search(
                r"^validibot_version := `(.*?)`",
                text,
                re.MULTILINE,
            )
            assert match is not None, f"{mod} missing validibot_version assignment"
            backtick = match.group(1)
            assert "pyproject.toml" in backtick, (
                f"{mod} validibot_version should read pyproject.toml, got: {backtick!r}"
            )
            assert "git rev-parse --show-toplevel" in backtick, (
                f"{mod} validibot_version should use ``git rev-parse "
                f"--show-toplevel`` to find the repo root from any cwd."
            )


class SupportBundleRecipeShapeTests(SimpleTestCase):
    """Verify Phase 6 ``collect-support-bundle`` recipes are real.

    Phase 6 of ADR-2026-04-27 adds operator-facing support bundle
    recipes for both substrates. These tests pin the contract on the
    recipe-shape level (the redaction primitives + management command
    are tested directly in
    ``validibot/core/tests/test_support_bundle.py`` and
    ``test_collect_support_bundle_command.py``).

    What we lock in here:

    1. The Phase 0 stubs are gone — real implementations replace them.
    2. The recipes invoke the ``collect_support_bundle`` management
       command (the canonical app-side snapshot path).
    3. The bundles are zipped (operator-friendly single artefact).
    4. The kit ships a ``support-bundle-README.txt`` that gets
       embedded in the zip — operators reviewing the bundle see a
       redaction explanation without consulting the website.
    5. Both substrates produce the same conceptual bundle shape
       (app-side JSON + host-side artefacts + README).
    """

    SELF_HOSTED_MOD = REPO_ROOT / "just" / "self-hosted" / "mod.just"
    GCP_MOD = REPO_ROOT / "just" / "gcp" / "mod.just"
    KIT_README = REPO_ROOT / "deploy" / "self-hosted" / "support-bundle-README.txt"

    def _self_hosted_text(self) -> str:
        return self.SELF_HOSTED_MOD.read_text(encoding="utf-8")

    def _gcp_text(self) -> str:
        return self.GCP_MOD.read_text(encoding="utf-8")

    def test_self_hosted_recipe_no_longer_stub(self):
        """``collect-support-bundle`` must not delegate to ``_phase0-stub``."""
        text = self._self_hosted_text()
        match = re.search(
            r"^collect-support-bundle \*flags:\n((?:    .*\n|    \n)*)",
            text,
            re.MULTILINE,
        )
        assert match is not None, "self-hosted collect-support-bundle missing"
        body = match.group(1)
        assert "_phase0-stub" not in body
        assert "_run-collect-support-bundle" in body

    def test_self_hosted_recipe_invokes_collect_support_bundle_command(self):
        """The recipe must call the management command for app-side data."""
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-collect-support-bundle flags:\n"
            r"((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        assert "manage.py collect_support_bundle" in body, (
            "Recipe must invoke the collect_support_bundle Django command"
        )
        # Output must be a zip — operators get a single artefact.
        assert "zip" in body.lower()

    def test_self_hosted_recipe_captures_host_artefacts(self):
        """The recipe must capture host-side data the Django command can't see."""
        text = self._self_hosted_text()
        match = re.search(
            r"^_run-collect-support-bundle flags:\n"
            r"((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert match is not None
        body = match.group(1)
        # Each host-side artefact comes from a distinct command —
        # the bundle's value comes from breadth, not just one
        # source.
        assert "docker compose" in body
        assert "logs --tail" in body or "logs --tail=" in body
        assert "df -h" in body
        # The validator inventory recipe (Phase 5) is reused.
        assert "just self-hosted validators" in body

    def test_kit_ships_support_bundle_readme(self):
        """The kit ships a README explaining bundle contents.

        The recipe ``cp``s this file into the workdir before
        zipping; without it operators reviewing a bundle have no
        offline reference for what's in there.
        """
        assert self.KIT_README.exists(), (
            f"{self.KIT_README} missing. Operators reviewing a "
            "bundle need an offline explanation of what's redacted."
        )
        readme = self.KIT_README.read_text(encoding="utf-8")
        # The file mentions the redaction story and the schema name.
        assert "REDACTED" in readme
        assert "validibot.support-bundle.v1" in readme

    def test_gcp_recipe_no_longer_stub(self):
        text = self._gcp_text()
        match = re.search(
            r"^collect-support-bundle stage \*flags:.*$",
            text,
            re.MULTILINE,
        )
        assert match is not None, "GCP collect-support-bundle missing"
        # GCP recipe stages directly into a Cloud Run Job (no separate
        # private helper needed because the stage parameter scopes it).
        block_match = re.search(
            r"^collect-support-bundle stage \*flags:.*\n"
            r"((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert block_match is not None
        body = block_match.group(1)
        assert "_phase0-stub" not in body

    def test_gcp_recipe_runs_command_via_cloud_run_job(self):
        """GCP captures app-side data by invoking the Django command in a Job.

        Mirrors the doctor / smoke-test / migrate Job patterns —
        same Cloud SQL + Secret Manager wiring so the Job sees the
        live deployment.
        """
        text = self._gcp_text()
        block_match = re.search(
            r"^collect-support-bundle stage \*flags:.*\n"
            r"((?:    .*\n|    \n|\n)*?)(?=^# |\Z)",
            text,
            re.MULTILINE,
        )
        assert block_match is not None
        body = block_match.group(1)
        assert "manage.py collect_support_bundle" in body
        assert "gcloud run jobs execute" in body
        # The recipe must also capture Cloud Run service state +
        # recent logs (the host-side equivalents on GCP).
        assert "gcloud run services describe" in body
        assert "gcloud logging read" in body
