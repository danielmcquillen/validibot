# some_app/management/commands/sync_flatpages_from_md.py
import pathlib

from django.contrib.flatpages.models import FlatPage
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand

HELP_DIR = pathlib.Path("docs/help_pages")


class Command(BaseCommand):
    help = "Sync Markdown help files into FlatPage records"

    def handle(self, *args, **options):
        site = Site.objects.get_current()

        for path in HELP_DIR.glob("*.md"):
            slug = path.stem  # "getting-started"
            url = f"/help/{slug}/"

            content = path.read_text(encoding="utf-8")

            page, created = FlatPage.objects.get_or_create(
                url=url,
                defaults={
                    "title": slug.replace("-", " ").title(),
                },
            )
            page.content = content
            page.save()
            page.sites.set([site])

            self.stdout.write(f"{'Created' if created else 'Updated'} {url}")
