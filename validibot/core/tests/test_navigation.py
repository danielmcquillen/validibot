from django.test import Client
from django.test import TestCase
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.utils import ensure_all_roles_exist


def _login_with_membership(client: Client, membership):
    user = membership.user
    user.set_current_org(membership.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = membership.org.id
    session.save()


class NavigationVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_viewer_nav_shows_limited_links(self):
        membership = MembershipFactory()
        membership.set_roles({RoleCode.WORKFLOW_VIEWER})
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("workflows:workflow_list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("Dashboard", html)
        self.assertNotIn("Validator Library", html)
        self.assertNotIn('group-label mt-4">\n        Design', html)
        self.assertNotIn('group-label mt-4">\n        Analytics', html)
        self.assertNotIn('group-label mt-4">\n        Admin', html)
        self.assertIn("Workflows", html)
        self.assertIn("Validation Runs", html)

    def test_author_nav_shows_design_sections(self):
        membership = MembershipFactory()
        membership.set_roles({RoleCode.AUTHOR})
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("workflows:workflow_list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Dashboard", html)
        self.assertIn("Validator Library", html)
        self.assertIn("group-label", html)

    def test_zero_role_nav_shows_no_app_links(self):
        membership = MembershipFactory()
        membership.set_roles(set())
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("workflows:workflow_list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("Dashboard", html)
        self.assertNotIn("Validator Library", html)
        self.assertNotIn("Validation Runs", html)
        self.assertNotIn("nav-link text-white", html)

    def test_superuser_nav_shows_full_access(self):
        """Superusers should see full navigation regardless of membership roles.

        Even when a superuser has a membership with no explicit roles assigned,
        they should still see all navigation sections (Dashboard, Workflows,
        Validator Library, Admin sections, etc.).
        """
        # Create a membership for the superuser with NO roles
        membership = MembershipFactory()
        membership.user.is_superuser = True
        membership.user.save()
        membership.set_roles(set())  # No roles assigned
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("workflows:workflow_list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Superuser should see all nav sections despite having no roles
        self.assertIn("Dashboard", html)
        self.assertIn("Validator Library", html)
        self.assertIn("Workflows", html)
        self.assertIn("Validation Runs", html)
        # Should see admin sections
        self.assertIn("Projects", html)
        self.assertIn("Members", html)
