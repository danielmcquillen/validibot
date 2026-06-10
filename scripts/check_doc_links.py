"""Check that relative links in the repo's markdown docs point at real files.

Why this exists: docs drift was found in June 2026's docs audit — links to a
``CHANGELOG.md`` that never existed and to docs-site pages that had been
renamed. External-link checking is deliberately out of scope (network calls
make CI flaky); this guard catches the high-confidence failure mode, which is
a *relative* link whose target file was renamed, moved, or never created.

Scope:
    - every ``*.md`` under ``docs/``
    - the repo-root markdown files (README, CONTRIBUTING, FAQ, SECURITY, ...)

What counts as a violation: an inline markdown link or image whose target is
a relative path (no URL scheme, not ``mailto:``, not a pure ``#anchor``, not
site-absolute ``/...``) that does not exist on disk after resolving against
the linking file's directory. Anchors and query strings are stripped before
the existence check; anchor validity is not verified.

Run it directly (no dependencies beyond the standard library):

    python scripts/check_doc_links.py

Exit code 0 = all relative links resolve; 1 = at least one is broken.
CI runs this from .github/workflows/docs.yml on any docs change.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Inline links/images: [text](target) and ![alt](target). The target group
# stops at the first unescaped ')' or whitespace+title — good enough for the
# link styles used in this repo's docs (no nested parens in paths).
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")

# Fenced code blocks must not be scanned: they routinely contain
# example-markdown and shell snippets full of bracket syntax.
FENCE_RE = re.compile(r"^(```|~~~)")

SKIP_DIR_NAMES = {
    "node_modules",
    "docs_build",
    "_archive",
    ".venv",
}


def iter_markdown_files() -> list[Path]:
    files = [p for p in REPO_ROOT.glob("*.md")]
    for path in (REPO_ROOT / "docs").rglob("*.md"):
        if not SKIP_DIR_NAMES.intersection(part for part in path.parts):
            files.append(path)
    return sorted(files)


def is_checkable(target: str) -> bool:
    """Only relative, file-ish targets are checkable offline."""
    if not target or target.startswith(("#", "/")):
        return False
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):  # http:, https:, mailto:
        return False
    return True


def check_file(md_path: Path) -> list[str]:
    problems: list[str] = []
    in_fence = False
    for lineno, line in enumerate(
        md_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in INLINE_LINK_RE.finditer(line):
            target = match.group(1).split("#", 1)[0].split("?", 1)[0]
            if not is_checkable(target) or not target:
                continue
            resolved = (md_path.parent / target).resolve()
            if not resolved.exists():
                rel = md_path.relative_to(REPO_ROOT)
                problems.append(f"{rel}:{lineno}: broken relative link -> {target}")
    return problems


def main() -> int:
    all_problems: list[str] = []
    files = iter_markdown_files()
    for md_path in files:
        all_problems.extend(check_file(md_path))

    if all_problems:
        sys.stderr.write("\n".join(all_problems) + "\n")
        sys.stderr.write(
            f"\n{len(all_problems)} broken relative link(s) across "
            f"{len(files)} markdown files.\n",
        )
        return 1

    sys.stdout.write(
        f"All relative links resolve across {len(files)} markdown files.\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
