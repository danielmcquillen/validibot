"""Tests for operator ordering guarantees in GCP deployment recipes.

These tests protect local release-readiness gates from drifting behind
production confirmation or runtime mutations. A predictable operator command
must reject a dirty build before it touches the isolated production stage.
"""

from pathlib import Path

from django.conf import settings


def test_maintenance_deploy_checks_and_builds_before_runtime_mutation():
    """Dirty trees and build failures must be discovered before maintenance work."""
    recipe = (Path(settings.BASE_DIR) / "just" / "gcp" / "mod.just").read_text()
    start = recipe.index("deploy-maintenance stage:")
    end = recipe.index("# Run a Django management command", start)
    deploy = recipe[start:end]

    preflight = deploy.index("just gcp _build-preflight")
    confirmation = deploy.index("WARNING: This will replace")
    build = deploy.index("just gcp build")
    push = deploy.index("just gcp push")
    maintenance = deploy.index("GCP_YES=1 just gcp mode-maintenance")

    assert preflight < confirmation < build < push < maintenance


def test_direct_build_uses_the_same_clean_tree_preflight():
    """Calling the build recipe directly must retain the provenance guard."""
    recipe = (Path(settings.BASE_DIR) / "just" / "gcp" / "mod.just").read_text()

    assert "build: _require-gcp-config _build-preflight" in recipe
    assert "if [ ${#DIRTY_REPOS[@]} -gt 0 ]; then" in recipe
