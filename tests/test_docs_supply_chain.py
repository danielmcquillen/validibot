"""Guard the developer-documentation browser supply chain.

Zensical normally loads Mermaid from a public CDN when it discovers a Mermaid
diagram. Production CSP intentionally blocks that behavior. These tests keep
the locally pinned runtime, its license, the template load order, and the
no-CDN policy from silently drifting during dependency or documentation
upgrades.
"""

import json
import re
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = REPOSITORY_ROOT / "package.json"
DOCS_ROOT = REPOSITORY_ROOT / "docs" / "dev_docs"
TEMPLATE = DOCS_ROOT / "overrides" / "main.html"
VENDOR_DIRECTORY = DOCS_ROOT / "javascripts" / "vendor"
CDN_LIBRARY_HOST_PATTERN = re.compile(
    rb"(?:unpkg\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com"
    rb"|fonts\.googleapis\.com|fonts\.gstatic\.com)",
    re.IGNORECASE,
)


class DeveloperDocsSupplyChainTests(unittest.TestCase):
    """Enforce local, lockfile-pinned documentation browser libraries."""

    def test_mermaid_dependency_uses_an_exact_version(self) -> None:
        """An exact npm pin makes the reviewed artifact reproducible."""
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        version = package["devDependencies"]["mermaid"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")

    def test_mermaid_runtime_and_license_are_vendored(self) -> None:
        """A fresh clone must build docs without downloading runtime code."""
        runtime = VENDOR_DIRECTORY / "mermaid.min.js"
        license_file = VENDOR_DIRECTORY / "MERMAID-LICENSE.txt"
        self.assertGreater(runtime.stat().st_size, 1_000_000)
        self.assertIn(
            "MIT License",
            license_file.read_text(encoding="utf-8"),
        )

    def test_fonts_are_exactly_pinned_and_vendored(self) -> None:
        """Docs typography must not depend on mutable Google Fonts responses."""
        package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        font_packages = (
            "@fontsource-variable/inter",
            "@fontsource-variable/jetbrains-mono",
            "@fontsource-variable/space-grotesk",
        )
        for name in font_packages:
            self.assertRegex(
                package["devDependencies"][name],
                r"^\d+\.\d+\.\d+$",
            )

        font_directory = DOCS_ROOT / "fonts"
        self.assertGreater(
            (font_directory / "inter-latin-wght-normal.woff2").stat().st_size,
            10_000,
        )
        self.assertGreater(
            (font_directory / "jetbrains-mono-latin-wght-normal.woff2").stat().st_size,
            10_000,
        )
        self.assertGreater(
            (font_directory / "space-grotesk-latin-wght-normal.woff2").stat().st_size,
            10_000,
        )

    def test_theme_disables_remote_font_generation(self) -> None:
        """Zensical must not emit its default Google Fonts stylesheet links."""
        configuration = (REPOSITORY_ROOT / "mkdocs.dev.yml").read_text(
            encoding="utf-8",
        )
        self.assertIn("font: false", configuration)
        self.assertIn("- stylesheets/fonts.css", configuration)

    def test_local_mermaid_loads_before_zensical_bundle(self) -> None:
        """Zensical must see window.mermaid before considering its CDN fallback."""
        template = TEMPLATE.read_text(encoding="utf-8")
        local_runtime = "javascripts/vendor/mermaid.min.js"
        self.assertIn(local_runtime, template)
        self.assertLess(template.index(local_runtime), template.index("super()"))

    def test_docs_source_has_no_public_cdn_library_hosts(self) -> None:
        """Executable docs libraries must be served by Validibot itself."""
        offenders: list[str] = []
        for path in [REPOSITORY_ROOT / "mkdocs.dev.yml", *DOCS_ROOT.rglob("*")]:
            if not path.is_file():
                continue
            if CDN_LIBRARY_HOST_PATTERN.search(path.read_bytes()):
                offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
