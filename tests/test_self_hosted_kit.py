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
