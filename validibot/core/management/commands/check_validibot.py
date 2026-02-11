"""
Verify that Validibot is set up correctly and all services are healthy.

This command runs a comprehensive set of checks to verify your Validibot
installation is working properly. Run it after initial setup, after upgrades,
or when troubleshooting issues.

Usage:
    python manage.py check_validibot
    python manage.py check_validibot --verbose
    python manage.py check_validibot --fix  # Attempt to fix issues

What this command checks:
    1. Database connectivity and migrations
    2. Cache/Redis connectivity
    3. Storage backend configuration
    4. Email configuration (optional)
    5. Site configuration
    6. Required data (roles, permissions, validators)
    7. Background task system (Celery)
    8. Docker availability (for advanced validators)
    9. Security settings

Based on patterns from:
    - GitLab's `gitlab:check` rake task
    - Zulip's health check plugins
    - Sentry's `/_health/` endpoint
    - NetBox's health check plugin

For more information, see: https://docs.validibot.com/troubleshooting
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.management.base import BaseCommand

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class CheckStatus(Enum):
    """Status of a health check."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass
class CheckResult:
    """Result of a single health check."""

    name: str
    status: CheckStatus
    message: str
    details: str | None = None
    fix_hint: str | None = None


class Command(BaseCommand):
    """
    Verify Validibot installation health.

    Runs comprehensive checks on all system components and reports any
    issues found. Use --verbose for detailed output, or --fix to attempt
    automatic fixes for common issues.
    """

    help = "Verify Validibot is set up correctly and all services are healthy"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.results: list[CheckResult] = []
        self.verbose = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            "-v",
            action="store_true",
            help="Show detailed output for each check",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Attempt to automatically fix common issues",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output results as JSON (for scripting)",
        )

    def handle(self, *args, **options):
        self.verbose = options.get("verbose", False)
        self.fix_mode = options.get("fix", False)
        self.json_output = options.get("json", False)

        if not self.json_output:
            self.stdout.write("")
            self.stdout.write(self.style.HTTP_INFO("=" * 60))
            self.stdout.write(self.style.HTTP_INFO("  Validibot Health Check"))
            self.stdout.write(self.style.HTTP_INFO("=" * 60))
            self.stdout.write("")

        # Run all checks
        checks: list[tuple[str, Callable]] = [
            ("Database", self._check_database),
            ("Migrations", self._check_migrations),
            ("Cache", self._check_cache),
            ("Storage", self._check_storage),
            ("Site Configuration", self._check_site),
            ("Roles & Permissions", self._check_roles_permissions),
            ("Validators", self._check_validators),
            ("Background Tasks", self._check_celery),
            ("Docker", self._check_docker),
            ("Email", self._check_email),
            ("Security", self._check_security),
        ]

        for section_name, check_func in checks:
            if not self.json_output:
                self.stdout.write(
                    self.style.MIGRATE_HEADING(f"Checking {section_name}...")
                )
            try:
                check_func()
            except Exception as e:
                self._add_result(
                    section_name,
                    CheckStatus.ERROR,
                    f"Check failed with exception: {e}",
                )
            if not self.json_output:
                self.stdout.write("")

        # Output results
        if self.json_output:
            self._output_json()
        else:
            self._output_summary()

        # Exit with error code if any errors found
        has_errors = any(r.status == CheckStatus.ERROR for r in self.results)
        if has_errors:
            sys.exit(1)

    def _add_result(
        self,
        name: str,
        status: CheckStatus,
        message: str,
        details: str | None = None,
        fix_hint: str | None = None,
    ):
        """Add a check result."""
        result = CheckResult(
            name=name,
            status=status,
            message=message,
            details=details,
            fix_hint=fix_hint,
        )
        self.results.append(result)

        if self.json_output:
            return

        # Display result
        if status == CheckStatus.OK:
            icon = self.style.SUCCESS("✓")
            msg = self.style.SUCCESS(message)
        elif status == CheckStatus.WARNING:
            icon = self.style.WARNING("!")
            msg = self.style.WARNING(message)
        elif status == CheckStatus.ERROR:
            icon = self.style.ERROR("✗")
            msg = self.style.ERROR(message)
        else:  # SKIPPED
            icon = self.style.NOTICE("-")
            msg = self.style.NOTICE(message)

        self.stdout.write(f"  {icon} {msg}")

        if self.verbose and details:
            for line in details.split("\n"):
                self.stdout.write(f"      {line}")

        if status in (CheckStatus.ERROR, CheckStatus.WARNING) and fix_hint:
            self.stdout.write(f"      Fix: {fix_hint}")

    # =========================================================================
    # Database Checks
    # =========================================================================

    def _check_database(self):
        """Check database connectivity and basic health."""
        from django.db import connection

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

            # Get database info
            db_settings = settings.DATABASES.get("default", {})
            engine = db_settings.get("ENGINE", "unknown")
            name = db_settings.get("NAME", "unknown")

            # Check PostgreSQL version if applicable
            if "postgresql" in engine:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT version()")
                    version = cursor.fetchone()[0]
                    details = f"Engine: {engine}\nDatabase: {name}\nVersion: {version}"
            else:
                details = f"Engine: {engine}\nDatabase: {name}"

            self._add_result(
                "Database connection",
                CheckStatus.OK,
                "Database is accessible",
                details=details if self.verbose else None,
            )

        except Exception as e:
            self._add_result(
                "Database connection",
                CheckStatus.ERROR,
                f"Cannot connect to database: {e}",
                fix_hint="Check DATABASE_URL or DATABASES settings",
            )

    def _check_migrations(self):
        """Check that all migrations have been applied."""
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        try:
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())

            if plan:
                pending = len(plan)
                self._add_result(
                    "Migrations",
                    CheckStatus.WARNING,
                    f"{pending} unapplied migration(s)",
                    details="\n".join(f"  - {m[0]}" for m in plan[:10]),
                    fix_hint="Run: python manage.py migrate",
                )

                if self.fix_mode:
                    from django.core.management import call_command

                    self.stdout.write("      Applying migrations...")
                    call_command("migrate", verbosity=0)
                    self._add_result(
                        "Migrations (fixed)",
                        CheckStatus.OK,
                        "Migrations applied successfully",
                    )
            else:
                self._add_result(
                    "Migrations",
                    CheckStatus.OK,
                    "All migrations applied",
                )

        except Exception as e:
            self._add_result(
                "Migrations",
                CheckStatus.ERROR,
                f"Cannot check migrations: {e}",
            )

    # =========================================================================
    # Cache Checks
    # =========================================================================

    def _check_cache(self):
        """Check cache/Redis connectivity."""
        from django.core.cache import cache

        test_key = "validibot_health_check"
        test_value = "ok"

        try:
            # Test set and get
            cache.set(test_key, test_value, timeout=10)
            result = cache.get(test_key)

            if result == test_value:
                # Get cache backend info
                cache_backend = settings.CACHES.get("default", {}).get(
                    "BACKEND", "unknown"
                )
                location = settings.CACHES.get("default", {}).get("LOCATION", "")

                details = f"Backend: {cache_backend}"
                if location:
                    # Mask password in Redis URL
                    if "@" in str(location):
                        location = location.split("@")[-1]
                    details += f"\nLocation: {location}"

                self._add_result(
                    "Cache",
                    CheckStatus.OK,
                    "Cache is working",
                    details=details if self.verbose else None,
                )

                # Clean up
                cache.delete(test_key)
            else:
                self._add_result(
                    "Cache",
                    CheckStatus.ERROR,
                    "Cache read/write failed",
                    fix_hint="Check REDIS_URL or CACHES settings",
                )

        except Exception as e:
            self._add_result(
                "Cache",
                CheckStatus.ERROR,
                f"Cannot connect to cache: {e}",
                fix_hint="Check REDIS_URL or CACHES settings",
            )

    # =========================================================================
    # Storage Checks
    # =========================================================================

    def _check_storage(self):
        """Check file storage configuration."""
        from django.core.files.storage import default_storage

        try:
            storage_class = default_storage.__class__.__name__
            details = f"Storage backend: {storage_class}"

            # Check if storage is accessible
            if hasattr(default_storage, "bucket_name"):
                # GCS storage
                bucket = getattr(default_storage, "bucket_name", "unknown")
                details += f"\nGCS Bucket: {bucket}"

                # Try to list (limited) to verify access
                try:
                    # Just check if we can access the bucket
                    list(default_storage.listdir(""))[:1]
                    self._add_result(
                        "Storage",
                        CheckStatus.OK,
                        f"GCS storage configured ({bucket})",
                        details=details if self.verbose else None,
                    )
                except Exception as e:
                    self._add_result(
                        "Storage",
                        CheckStatus.ERROR,
                        f"Cannot access GCS bucket: {e}",
                        fix_hint="Check GS_BUCKET_NAME and GCP credentials",
                    )

            elif hasattr(default_storage, "location"):
                # Local filesystem storage
                location = default_storage.location
                details += f"\nLocation: {location}"

                if os.path.exists(location):
                    # Check if writable
                    test_file = os.path.join(location, ".validibot_health_check")
                    try:
                        with open(test_file, "w") as f:
                            f.write("test")
                        os.remove(test_file)
                        self._add_result(
                            "Storage",
                            CheckStatus.OK,
                            "Local storage is writable",
                            details=details if self.verbose else None,
                        )
                    except OSError as e:
                        self._add_result(
                            "Storage",
                            CheckStatus.ERROR,
                            f"Storage directory not writable: {e}",
                            fix_hint=f"Check permissions on {location}",
                        )
                else:
                    self._add_result(
                        "Storage",
                        CheckStatus.ERROR,
                        f"Storage directory does not exist: {location}",
                        fix_hint=f"Create directory: mkdir -p {location}",
                    )
            else:
                self._add_result(
                    "Storage",
                    CheckStatus.OK,
                    f"Storage configured ({storage_class})",
                    details=details if self.verbose else None,
                )

        except Exception as e:
            self._add_result(
                "Storage",
                CheckStatus.ERROR,
                f"Storage check failed: {e}",
            )

    # =========================================================================
    # Site Configuration Checks
    # =========================================================================

    def _check_site(self):
        """Check Django Sites framework configuration."""
        from django.contrib.sites.models import Site

        try:
            site = Site.objects.get(id=settings.SITE_ID)

            if site.domain in ("example.com", "localhost"):
                self._add_result(
                    "Site domain",
                    CheckStatus.WARNING,
                    f"Site domain is '{site.domain}' (default/development value)",
                    fix_hint="Run: python manage.py setup_validibot --domain yourdomain.com",
                )
            else:
                self._add_result(
                    "Site domain",
                    CheckStatus.OK,
                    f"Site domain: {site.domain}",
                )

            self._add_result(
                "Site name",
                CheckStatus.OK,
                f"Site name: {site.name}",
            )

        except Site.DoesNotExist:
            self._add_result(
                "Site configuration",
                CheckStatus.ERROR,
                f"Site with ID {settings.SITE_ID} does not exist",
                fix_hint="Run: python manage.py setup_validibot",
            )

    # =========================================================================
    # Roles & Permissions Checks
    # =========================================================================

    def _check_roles_permissions(self):
        """Check that required roles and permissions exist."""
        from django.contrib.auth.models import Permission

        from validibot.users.constants import RoleCode
        from validibot.users.models import Role

        # Check roles
        expected_roles = set(r.value for r in RoleCode)
        existing_roles = set(Role.objects.values_list("code", flat=True))
        missing_roles = expected_roles - existing_roles

        if missing_roles:
            self._add_result(
                "Roles",
                CheckStatus.ERROR,
                f"Missing roles: {', '.join(missing_roles)}",
                fix_hint="Run: python manage.py setup_validibot",
            )

            if self.fix_mode:
                for code in missing_roles:
                    label = RoleCode(code).label
                    Role.objects.create(code=code, name=label)
                self._add_result(
                    "Roles (fixed)",
                    CheckStatus.OK,
                    f"Created {len(missing_roles)} missing roles",
                )
        else:
            self._add_result(
                "Roles",
                CheckStatus.OK,
                f"{len(existing_roles)} roles configured",
            )

        # Check custom permissions
        from validibot.core.management.commands.setup_validibot import (
            DEFAULT_PERMISSIONS,
        )

        expected_perms = set(p[0] for p in DEFAULT_PERMISSIONS)
        existing_perms = set(
            Permission.objects.filter(codename__in=expected_perms).values_list(
                "codename", flat=True
            )
        )
        missing_perms = expected_perms - existing_perms

        if missing_perms:
            self._add_result(
                "Permissions",
                CheckStatus.ERROR,
                f"Missing permissions: {', '.join(list(missing_perms)[:5])}...",
                fix_hint="Run: python manage.py setup_validibot",
            )
        else:
            self._add_result(
                "Permissions",
                CheckStatus.OK,
                f"{len(existing_perms)} custom permissions configured",
            )

    # =========================================================================
    # Validators Checks
    # =========================================================================

    def _check_validators(self):
        """Check that default validators exist."""
        from validibot.validations.models import Validator

        validator_count = Validator.objects.filter(is_system=True).count()

        if validator_count == 0:
            self._add_result(
                "Validators",
                CheckStatus.ERROR,
                "No system validators found",
                fix_hint="Run: python manage.py setup_validibot",
            )
        else:
            self._add_result(
                "Validators",
                CheckStatus.OK,
                f"{validator_count} system validators configured",
            )

        # Check for active validators
        active_count = Validator.objects.filter(is_active=True).count()
        if active_count == 0:
            self._add_result(
                "Active validators",
                CheckStatus.WARNING,
                "No active validators found",
            )

    # =========================================================================
    # Background Tasks Checks
    # =========================================================================

    def _check_celery(self):
        """Check Celery/background task system."""
        # Check if Celery is configured
        broker_url = getattr(settings, "CELERY_BROKER_URL", None)

        if not broker_url:
            self._add_result(
                "Celery broker",
                CheckStatus.SKIPPED,
                "CELERY_BROKER_URL not configured (using sync mode or Cloud Tasks)",
            )
            return

        try:
            from celery import Celery

            app = Celery()
            app.config_from_object("django.conf:settings", namespace="CELERY")

            # Try to ping the broker
            conn = app.connection()
            conn.ensure_connection(max_retries=1, timeout=5)
            conn.close()

            self._add_result(
                "Celery broker",
                CheckStatus.OK,
                "Celery broker is accessible",
            )

        except Exception as e:
            self._add_result(
                "Celery broker",
                CheckStatus.ERROR,
                f"Cannot connect to Celery broker: {e}",
                fix_hint="Check CELERY_BROKER_URL setting",
            )

        # Check Celery Beat schedules
        try:
            from django_celery_beat.models import PeriodicTask

            schedule_count = PeriodicTask.objects.filter(enabled=True).count()
            if schedule_count == 0:
                self._add_result(
                    "Celery Beat schedules",
                    CheckStatus.WARNING,
                    "No periodic tasks configured",
                    fix_hint="Run: python manage.py setup_validibot",
                )
            else:
                self._add_result(
                    "Celery Beat schedules",
                    CheckStatus.OK,
                    f"{schedule_count} periodic tasks configured",
                )

        except ImportError:
            self._add_result(
                "Celery Beat",
                CheckStatus.SKIPPED,
                "django_celery_beat not installed",
            )

    # =========================================================================
    # Docker Checks
    # =========================================================================

    def _check_docker(self):
        """Check Docker availability for advanced validators."""
        import shutil

        docker_path = shutil.which("docker")

        if not docker_path:
            self._add_result(
                "Docker",
                CheckStatus.WARNING,
                "Docker not found in PATH",
                details="Advanced validators (EnergyPlus, FMI) require Docker",
                fix_hint="Install Docker or configure VALIDATOR_RUNNER for cloud execution",
            )
            return

        try:
            import subprocess

            result = subprocess.run(
                ["docker", "info"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                self._add_result(
                    "Docker",
                    CheckStatus.OK,
                    "Docker is available and running",
                )

                # Check for validator images
                self._check_validator_images()
            else:
                self._add_result(
                    "Docker",
                    CheckStatus.WARNING,
                    "Docker installed but not accessible",
                    details=result.stderr[:200] if result.stderr else None,
                    fix_hint="Start Docker daemon or check permissions",
                )

        except subprocess.TimeoutExpired:
            self._add_result(
                "Docker",
                CheckStatus.WARNING,
                "Docker command timed out",
            )
        except Exception as e:
            self._add_result(
                "Docker",
                CheckStatus.WARNING,
                f"Cannot check Docker: {e}",
            )

    def _check_validator_images(self):
        """Check if validator Docker images are available."""
        import subprocess

        # Expected validator images
        expected_images = [
            "validibot-validator-energyplus",
            "validibot-validator-fmi",
        ]

        try:
            result = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                available_images = set(result.stdout.strip().split("\n"))
                found = [img for img in expected_images if img in available_images]
                missing = [
                    img for img in expected_images if img not in available_images
                ]

                if found:
                    self._add_result(
                        "Validator images",
                        CheckStatus.OK,
                        f"{len(found)} validator image(s) available",
                        details="\n".join(f"  - {img}" for img in found)
                        if self.verbose
                        else None,
                    )

                if missing and self.verbose:
                    self._add_result(
                        "Validator images (optional)",
                        CheckStatus.SKIPPED,
                        f"{len(missing)} validator image(s) not installed",
                        details="\n".join(f"  - {img}" for img in missing),
                        fix_hint="Build images: just build energyplus fmi",
                    )

        except Exception:
            pass  # Non-critical, skip silently

    # =========================================================================
    # Email Checks
    # =========================================================================

    def _check_email(self):
        """Check email configuration."""
        email_backend = getattr(settings, "EMAIL_BACKEND", "")

        if "console" in email_backend.lower() or "dummy" in email_backend.lower():
            self._add_result(
                "Email",
                CheckStatus.WARNING,
                f"Using development email backend: {email_backend.split('.')[-1]}",
                details="Emails will not be sent to real addresses",
                fix_hint="Configure EMAIL_HOST, EMAIL_PORT, etc. for production",
            )
            return

        if "smtp" in email_backend.lower() or "ses" in email_backend.lower():
            email_host = getattr(settings, "EMAIL_HOST", None)
            email_port = getattr(settings, "EMAIL_PORT", 587)

            if not email_host:
                self._add_result(
                    "Email",
                    CheckStatus.ERROR,
                    "EMAIL_HOST not configured",
                    fix_hint="Set EMAIL_HOST in settings or environment",
                )
                return

            # Try to connect to SMTP server
            try:
                sock = socket.create_connection((email_host, email_port), timeout=5)
                sock.close()
                self._add_result(
                    "Email",
                    CheckStatus.OK,
                    f"SMTP server reachable ({email_host}:{email_port})",
                )
            except (OSError, TimeoutError) as e:
                self._add_result(
                    "Email",
                    CheckStatus.WARNING,
                    f"Cannot connect to SMTP server: {e}",
                    fix_hint="Check EMAIL_HOST and EMAIL_PORT settings",
                )
        else:
            self._add_result(
                "Email",
                CheckStatus.OK,
                f"Email backend: {email_backend.split('.')[-1]}",
            )

    # =========================================================================
    # Security Checks
    # =========================================================================

    def _check_security(self):
        """Check security-related settings."""
        issues = []

        # Check DEBUG mode
        if settings.DEBUG:
            issues.append(("DEBUG mode enabled", "Set DEBUG=False in production"))

        # Check SECRET_KEY
        secret_key = getattr(settings, "SECRET_KEY", "")
        if "changeme" in secret_key.lower() or len(secret_key) < 32:
            issues.append(("Weak SECRET_KEY", "Generate a strong random SECRET_KEY"))

        # Check ALLOWED_HOSTS
        allowed_hosts = getattr(settings, "ALLOWED_HOSTS", [])
        if "*" in allowed_hosts:
            issues.append(("ALLOWED_HOSTS contains '*'", "Set specific allowed hosts"))
        elif not allowed_hosts:
            issues.append(
                ("ALLOWED_HOSTS is empty", "Add your domain to ALLOWED_HOSTS")
            )

        # Check CSRF settings
        csrf_trusted = getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
        if not csrf_trusted and not settings.DEBUG:
            issues.append(
                (
                    "CSRF_TRUSTED_ORIGINS not set",
                    "Add your domain to CSRF_TRUSTED_ORIGINS",
                )
            )

        # Check SECURE settings for production
        if not settings.DEBUG:
            if not getattr(settings, "SECURE_SSL_REDIRECT", False):
                issues.append(("SECURE_SSL_REDIRECT is False", "Enable HTTPS redirect"))
            if not getattr(settings, "SESSION_COOKIE_SECURE", False):
                issues.append(
                    ("SESSION_COOKIE_SECURE is False", "Enable secure cookies")
                )

        if issues:
            for issue, fix in issues:
                status = CheckStatus.WARNING if settings.DEBUG else CheckStatus.ERROR
                self._add_result(
                    "Security",
                    status,
                    issue,
                    fix_hint=fix,
                )
        else:
            self._add_result(
                "Security",
                CheckStatus.OK,
                "Security settings look good",
            )

    # =========================================================================
    # Output
    # =========================================================================

    def _output_summary(self):
        """Output summary of all checks."""
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write(self.style.HTTP_INFO("  Summary"))
        self.stdout.write(self.style.HTTP_INFO("=" * 60))
        self.stdout.write("")

        # Count by status
        ok_count = sum(1 for r in self.results if r.status == CheckStatus.OK)
        warn_count = sum(1 for r in self.results if r.status == CheckStatus.WARNING)
        error_count = sum(1 for r in self.results if r.status == CheckStatus.ERROR)
        skip_count = sum(1 for r in self.results if r.status == CheckStatus.SKIPPED)

        self.stdout.write(f"  {self.style.SUCCESS('✓')} Passed:   {ok_count}")
        if warn_count:
            self.stdout.write(f"  {self.style.WARNING('!')} Warnings: {warn_count}")
        if error_count:
            self.stdout.write(f"  {self.style.ERROR('✗')} Errors:   {error_count}")
        if skip_count:
            self.stdout.write(f"  {self.style.NOTICE('-')} Skipped:  {skip_count}")

        self.stdout.write("")

        if error_count > 0:
            self.stdout.write(
                self.style.ERROR("  Some checks failed. Please fix the errors above.")
            )
            self.stdout.write("  You can try: python manage.py check_validibot --fix")
        elif warn_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    "  Validibot is working but some warnings were found."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS("  All checks passed! Validibot is healthy.")
            )

        self.stdout.write("")

    def _output_json(self):
        """Output results as JSON."""
        import json

        output = {
            "status": "ok"
            if not any(r.status == CheckStatus.ERROR for r in self.results)
            else "error",
            "checks": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "message": r.message,
                    "details": r.details,
                    "fix_hint": r.fix_hint,
                }
                for r in self.results
            ],
            "summary": {
                "ok": sum(1 for r in self.results if r.status == CheckStatus.OK),
                "warnings": sum(
                    1 for r in self.results if r.status == CheckStatus.WARNING
                ),
                "errors": sum(1 for r in self.results if r.status == CheckStatus.ERROR),
                "skipped": sum(
                    1 for r in self.results if r.status == CheckStatus.SKIPPED
                ),
            },
        }

        self.stdout.write(json.dumps(output, indent=2))
