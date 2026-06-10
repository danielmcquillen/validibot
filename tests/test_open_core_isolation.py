"""Enforce the open-core import boundary between community and commercial code.

WHY THIS EXISTS
---------------
The community repo must stay runnable on its own — a self-hosted user who has
*not* purchased Pro (and certainly has no access to the proprietary Cloud
package) installs only ``validibot`` and must be able to import every module
and boot the app. The commercial packages are activated by *adding them to
``INSTALLED_APPS``* (see ``config/settings/local_pro.py``), not by being
imported from community code.

Because our day-to-day test venv has ``validibot_pro`` / ``validibot_cloud``
installed (so ``just test pro`` / ``just test cloud`` can run), a stray import
of a commercial package from community source would NOT fail locally — it would
only blow up in a real community install. This test is the safety net that
makes that regression fail here instead.

THE TWO RULES IT ENFORCES
-------------------------
1. **Pro may only be imported conditionally.** Community code is allowed to
   integrate with ``validibot_pro`` (e.g. surfacing issued credentials) — but
   only via imports that do NOT run unconditionally at import time: inside a
   function, a ``try/except``, or a conditional ``if`` (the sanctioned
   module-level form is ``if "validibot_pro" in settings.INSTALLED_APPS:`` —
   see ``config/urls_web.py`` — plus ``if TYPE_CHECKING:`` for type-only
   imports). A *bare* module-level ``from validibot_pro import …`` would raise
   ``ModuleNotFoundError`` at import time on a community-only install, so that
   shape is forbidden.
2. **Cloud must never be referenced at all.** The one-way rule (see
   ``validibot-cloud/CLAUDE.md``): cloud may import community, never the
   reverse. So *any* ``validibot_cloud`` import in community source — lazy or
   not — is a violation.

If this test fails, move the offending import inside the function that uses it
(for Pro) or remove the community→cloud dependency entirely (for Cloud).
"""

from __future__ import annotations

import ast
from pathlib import Path

# Repo root is the parent of this ``tests/`` directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Community source trees to scan. These are the parts a community-only install
# ships and imports; tests and migrations are excluded below.
_SCAN_ROOTS = (
    _REPO_ROOT / "validibot",
    _REPO_ROOT / "config",
    _REPO_ROOT / "mcp" / "src",
)

_PRO_PREFIX = "validibot_pro"
_CLOUD_PREFIX = "validibot_cloud"


def _is_excluded(path: Path) -> bool:
    """Skip tests, migrations, caches — they may legitimately import commercial code."""
    parts = set(path.parts)
    if parts & {"tests", "migrations", "__pycache__"}:
        return True
    return path.name.startswith("test_") or path.name == "conftest.py"


def _iter_community_py_files():
    """Yield every community ``.py`` source file in scope."""
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if not _is_excluded(path):
                yield path


def _references(module: str | None, prefix: str) -> bool:
    """Return True when an imported module name is (or is under) ``prefix``."""
    if not module:
        return False
    return module == prefix or module.startswith(f"{prefix}.")


def _is_deferred(node: ast.AST) -> bool:
    """Return True when *node*'s import does not run *unconditionally* at import.

    An import is safe for a community-only install when it sits inside any of:

    * a **function** body — only runs when that code path executes;
    * a ``try`` block — guarded against the package being absent;
    * an ``if`` block — conditional. The sanctioned module-level pattern is
      ``if "validibot_pro" in settings.INSTALLED_APPS:`` (see
      ``config/urls_web.py``), and ``if TYPE_CHECKING:`` for type-only imports.

    Only a *bare, unconditional* module-level import would always execute (and
    therefore crash) when Pro is absent, so that is the only shape we forbid.
    """
    cur = getattr(node, "parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Try, ast.If)):
            return True
        cur = getattr(cur, "parent", None)
    return False


def _imported_modules(node: ast.Import | ast.ImportFrom):
    """Yield the dotted module name(s) an import node brings in."""
    if isinstance(node, ast.ImportFrom):
        # Ignore relative imports (level > 0) — they can't reach a top-level
        # commercial package.
        if node.level == 0:
            yield node.module
    else:
        for alias in node.names:
            yield alias.name


def _commercial_import_violations() -> list[str]:
    """Collect every community import that breaks the open-core boundary."""
    violations: list[str] = []
    for path in _iter_community_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        # Attach parent pointers so ``_is_deferred`` can climb the tree.
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent  # type: ignore[attr-defined]

        rel = path.relative_to(_REPO_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module in _imported_modules(node):
                if _references(module, _CLOUD_PREFIX):
                    violations.append(
                        f"{rel}:{node.lineno}: community must never import "
                        f"{module!r} (one-way rule: cloud→community only)",
                    )
                elif _references(module, _PRO_PREFIX) and not _is_deferred(node):
                    violations.append(
                        f"{rel}:{node.lineno}: top-level import of {module!r}; "
                        "move it inside the function that uses it (Pro imports "
                        "must be deferred so community installs without Pro)",
                    )
    return violations


def test_community_never_imports_commercial_packages_at_module_level():
    """Community source must not hard-depend on Pro/Cloud at import time.

    This pins the open-core guarantee: a community-only install (no
    ``validibot_pro`` / ``validibot_cloud`` on the path) can import every module
    and boot. Deferred Pro imports are allowed; cloud imports are not. A failure
    here lists the exact ``file:line`` to fix — and would otherwise have slipped
    through because our dev venv has the commercial packages installed.
    """
    violations = _commercial_import_violations()
    detail = "\n  ".join(violations)
    assert not violations, f"Open-core boundary violations:\n  {detail}"
