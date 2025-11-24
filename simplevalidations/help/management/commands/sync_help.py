# help/management/commands/sync_help_flatpages.py

from pathlib import Path

from django.conf import settings
from django.contrib.flatpages.models import FlatPage
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand


def _pretty_title_from_stem(stem: str) -> str:
    """
    Convert a filename stem into a human title.

    Examples:
      "basic_concepts" -> "Basic Concepts"
      "how_simple_validations_works" -> "How Simple Validations Works"
      "validators-overview" -> "Validators Overview"
    """
    return stem.replace("_", " ").replace("-", " ").title()


HELP_URL_PREFIX = "/app/help/"


class Command(BaseCommand):
    help = "Sync Markdown help files into django.contrib.flatpages"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be synced without writing to the database.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear all existing help flatpages before syncing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        clear = options["clear"]
        if clear:
            if dry_run:
                self.stdout.write("[DRY] Clearing all existing help flatpages.")
            else:
                help_pages = FlatPage.objects.filter(url__startswith="/help/")
                count = help_pages.count()
                help_pages.delete()
                self.stdout.write(f"Cleared {count} existing help flatpages.")

        # Adjust this if your path is different.
        # This matches: simplevalidations/docs/help_pages
        base_dir = Path(settings.BASE_DIR)
        help_root = base_dir / "docs" / "help_pages"

        if not help_root.exists():
            raise SystemExit(f"Help root does not exist: {help_root}")

        site = Site.objects.get_current()

        self.stdout.write(f"Syncing help pages from: {help_root}")
        if dry_run:
            self.stdout.write("DRY RUN: no changes will be written.\n")

        for md_path in sorted(help_root.rglob("*.md")):
            # Path relative to help_root, without suffix
            # e.g. concepts/basic_concepts.md -> ("concepts", "basic_concepts")
            rel = md_path.relative_to(help_root)
            stem_parts = rel.with_suffix("").parts

            # Decide URL suffix
            #
            #  - index.md at root             -> "index/"
            #  - concepts/index.md            -> "concepts/"
            #  - concepts/basic_concepts.md   -> "concepts/basic_concepts/"
            #
            if stem_parts == ("index",):
                url_suffix = "index/"
            elif stem_parts[-1] == "index":
                # section index: use parent directory as path
                url_suffix = "/".join(stem_parts[:-1]) + "/"
            else:
                url_suffix = "/".join(stem_parts) + "/"

            url = f"{HELP_URL_PREFIX}{url_suffix}"

            # Decide title
            #
            #  - index.md at root             -> "Help"
            #  - concepts/index.md            -> "Concepts"
            #  - concepts/basic_concepts.md   -> "Basic Concepts"
            #
            if stem_parts == ("index",):
                title = "Help"
            elif stem_parts[-1] == "index":
                # Use parent directory name for section index pages
                parent_stem = stem_parts[-2]
                title = _pretty_title_from_stem(parent_stem)
            else:
                title = _pretty_title_from_stem(stem_parts[-1])

            content = md_path.read_text(encoding="utf-8")

            if dry_run:
                self.stdout.write(f"[DRY] {url}  <-  {rel}  (title: {title})")
                continue

            page, created = FlatPage.objects.get_or_create(
                url=url,
                defaults={"title": title},
            )

            # If the page already exists, we still want to keep the title in sync
            page.title = title
            page.content = content
            page.save()
            page.sites.set([site])

            action = "Created" if created else "Updated"
            self.stdout.write(f"{action} {url}  <-  {rel}  (title: {title})")

        self.stdout.write(self.style.SUCCESS("Help pages sync complete."))
