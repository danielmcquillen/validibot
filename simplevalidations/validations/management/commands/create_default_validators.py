from django.core.management.base import BaseCommand

from simplevalidations.validations.utils import create_default_validators


class Command(BaseCommand):
    help = "Create baseline validator records for common validation types."

    def handle(self, *args, **options):
        created, updated = create_default_validators()
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created {created} validator(s)."))
        self.stdout.write(self.style.SUCCESS(f"Updated {updated} existing validator(s)."))
