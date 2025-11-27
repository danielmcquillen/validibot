from __future__ import annotations

from allauth.account.models import EmailAddress
from django.core.management.base import BaseCommand
from faker import Faker

from simplevalidations.users.models import User


class Command(BaseCommand):
    help = "Create 10 random users with @example.com emails and verified/primary allauth addresses."

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=10,
            help="Number of users to create (default: 10).",
        )

    def handle(self, *args, **options):
        fake = Faker()
        count = options["count"]
        created = 0
        
        for _ in range(count):
            # Generate a unique username from first name
            first_name = fake.first_name().lower()
            username = first_name
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{first_name}{counter}"
                counter += 1
            
            email = f"{username}@example.com"
            full_name = fake.name()
            
            user = User.objects.create_user(
                username=username,
                email=email,
                password="test1234",
                name=full_name,
            )
            EmailAddress.objects.update_or_create(
                user=user,
                email=user.email,
                defaults={
                    "verified": True,
                    "primary": True,
                },
            )
            created += 1
            self.stdout.write(
                self.style.SUCCESS(f"Created user {user.username} ({user.name})"),
            )
        self.stdout.write(self.style.SUCCESS(f"Done. Created {created} user(s)."))

