"""Tests for the workflow signal mapping views.

The signal mapping views provide a full CRUD editor for workflow-level
signal mappings (``WorkflowSignalMapping``), a sample data parser for
auto-discovering candidate signals, and an output promotion toggle for
``SignalDefinition.signal_name``.

All views require **manage** permission (``WORKFLOW_EDIT``) because
signal mapping is an authoring/configuration surface.

These tests verify:

* **HTML page** — the GET endpoint returns the editor page with the
  correct template, and supports JSON backward compat via Accept header.
* **CRUD** — create, edit, delete, and move operations work through
  modal forms and return the correct HTMx events.
* **Validation** — invalid signal names, reserved names, and duplicates
  are rejected with form errors.
* **Sample data** — JSON and XML parsing, error handling, deduplication,
  and both HTMx partial and JSON response modes.
* **Pre-fill** — query params from sample data candidates populate the
  create form.
* **Access control** — unauthenticated users are redirected, outsiders
  get 404, executors get 403.
* **Output promotion** — setting and clearing ``signal_name`` on a
  ``SignalDefinition``.
"""

from __future__ import annotations

import json

from django.test import Client
from django.test import TestCase
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import ValidationType
from validibot.validations.models import SignalDefinition
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


def _login_as_author(client: Client, workflow):
    """Log in as the workflow.user with author permissions in the org."""
    membership = workflow.user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    workflow.user.set_current_org(workflow.org)
    client.force_login(workflow.user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return workflow.user


# ── GET /workflows/<pk>/signals/ ───────────────────────────────────────
# The listing endpoint returns the full HTML editor page by default,
# or JSON when Accept: application/json is set.  HTMx partial requests
# return only the table fragment for in-place refresh.


class TestSignalMappingGetView(TestCase):
    """Tests for the WorkflowSignalMappingView GET endpoint."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_signal_mapping_get_returns_html_page(self):
        """GET without Accept header must return the full HTML editor page.

        The editor page is the primary UI for managing workflow-level
        signals.  It must use the correct template and include the
        workflow context.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "workflows/workflow_signal_mapping.html",
        )
        self.assertEqual(response.context["workflow"], workflow)

    def test_signal_mapping_get_returns_json_with_accept_header(self):
        """GET with Accept: application/json must return a JSON response
        containing workflow_id, workflow_name, and a mappings list.

        This preserves backward compatibility for API consumers and
        the existing test suite that relied on JSON responses.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.get(url, headers={"accept": "application/json"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        data = json.loads(response.content)
        self.assertEqual(data["workflow_id"], workflow.pk)
        self.assertEqual(data["workflow_name"], workflow.name)
        self.assertIsInstance(data["mappings"], list)

    def test_signal_mapping_get_json_includes_mappings(self):
        """Persisted WorkflowSignalMapping rows must appear in the JSON
        response.

        The JSON serialisation includes each mapping's id, name,
        source_path, default_value, on_missing, and data_type.  If a
        mapping is silently omitted the author sees a stale view of
        their signals and may unknowingly create duplicates.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)

        m1 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
            position=10,
        )
        m2 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="conductivity",
            source_path="materials[0].conductivity",
            on_missing="null",
            position=20,
        )

        url = reverse(
            "workflows:workflow_signal_mapping",
            kwargs={"pk": workflow.pk},
        )
        response = self.client.get(url, headers={"accept": "application/json"})
        data = json.loads(response.content)

        self.assertEqual(len(data["mappings"]), 2)

        first, second = data["mappings"]
        self.assertEqual(first["id"], m1.pk)
        self.assertEqual(first["name"], "emissivity")
        self.assertEqual(first["source_path"], "materials[0].emissivity")
        self.assertEqual(first["on_missing"], "error")

        self.assertEqual(second["id"], m2.pk)
        self.assertEqual(second["name"], "conductivity")
        self.assertEqual(second["on_missing"], "null")

    def test_signal_mapping_htmx_returns_table_partial(self):
        """GET with HX-Request header must return only the table partial.

        The signal mapping editor uses event-driven refresh: after any
        CRUD operation, the container fires an HTMx request to reload
        just the table.  Returning the full page would break the swap.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            position=10,
        )

        url = reverse(
            "workflows:workflow_signal_mapping",
            kwargs={"pk": workflow.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "workflows/partials/signal_mapping_table.html",
        )
        self.assertContains(response, "s.emissivity")


# ── Access control ─────────────────────────────────────────────────────
# Signal mapping views require manage (WORKFLOW_EDIT) permission.
# WorkflowObjectMixin resolves the workflow (including guest/public),
# then the view checks user_can_manage_workflow() and returns 403 for
# non-managing users.  Unauthenticated users are redirected to login;
# users without any workflow access get a 404.


class TestSignalMappingAccessControl(TestCase):
    """Tests for authentication and authorisation on the signal mapping views."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_signal_mapping_get_requires_auth(self):
        """An unauthenticated request must redirect to the login page.

        Without this check, anonymous users could enumerate workflow
        signal mappings, leaking internal data-path information.
        """
        workflow = WorkflowFactory()
        url = reverse(
            "workflows:workflow_signal_mapping",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_signal_mapping_get_requires_workflow_access(self):
        """A logged-in user without access to the workflow must get 404.

        WorkflowObjectMixin uses get_object_or_404, which returns 404
        rather than 403 to avoid leaking whether a workflow exists.
        This prevents enumeration attacks where an attacker probes
        sequential IDs to discover valid workflows.
        """
        workflow = WorkflowFactory()
        outsider = UserFactory()
        self.client.force_login(outsider)

        url = reverse(
            "workflows:workflow_signal_mapping",
            kwargs={"pk": workflow.pk},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_signal_mapping_get_requires_manage_permission(self):
        """An org member with only EXECUTOR role (no WORKFLOW_EDIT) must get 403.

        Signal mapping is an authoring surface.  Executors can run
        workflows but must not be able to view or modify signal
        configuration.  If this returned 200, a non-author could
        discover internal data-path mappings.
        """
        workflow = WorkflowFactory()
        executor = UserFactory()
        grant_role(executor, workflow.org, RoleCode.EXECUTOR)
        executor.set_current_org(workflow.org)
        self.client.force_login(executor)
        session = self.client.session
        session["active_org_id"] = workflow.org_id
        session.save()

        url = reverse(
            "workflows:workflow_signal_mapping",
            kwargs={"pk": workflow.pk},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 403)


# ── Create signal mapping ────────────────────────────────────────────
# The create view serves a modal form (GET) and processes the
# submission (POST).  It follows the two-template modal pattern used
# by the assertion CRUD.


class TestSignalMappingCreateView(TestCase):
    """Tests for the WorkflowSignalMappingCreateView."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_create_get_returns_modal_form(self):
        """GET must return the modal form partial with the correct template.

        The form is loaded via HTMx into the modal shell.  If the
        template is wrong or the form is missing, the modal will show
        a spinner forever.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "workflows/partials/signal_mapping_form.html",
        )
        self.assertIn("form", response.context)

    def test_create_valid_saves_mapping(self):
        """POST with valid data must create a WorkflowSignalMapping and
        return a 204 with signals-changed event.

        The 204 tells HTMx that the response has no body to swap.
        The HX-Trigger header fires the signals-changed event which
        causes the table container to reload.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "emissivity",
                "source_path": "materials[0].emissivity",
                "on_missing": "error",
                "default_value": "",
                "data_type": "",
            },
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("signals-changed", response["HX-Trigger"])
        self.assertEqual(
            WorkflowSignalMapping.objects.filter(workflow=workflow).count(),
            1,
        )
        mapping = WorkflowSignalMapping.objects.get(workflow=workflow)
        self.assertEqual(mapping.name, "emissivity")
        self.assertEqual(mapping.source_path, "materials[0].emissivity")

    def test_create_invalid_name_returns_form_errors(self):
        """POST with an invalid signal name (not a CEL identifier) must
        return 200 with form errors visible.

        Returning 200 (not 400) is important because HTMx needs to
        swap the re-rendered form into the modal.  A non-200 status
        would trigger HTMx error handling instead.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "123invalid",
                "source_path": "some.path",
                "on_missing": "error",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "workflows/partials/signal_mapping_form.html",
        )
        # No mapping should have been created
        self.assertEqual(
            WorkflowSignalMapping.objects.filter(workflow=workflow).count(),
            0,
        )

    def test_create_reserved_name_returns_error(self):
        """POST with a reserved namespace name (e.g. 'payload') must be
        rejected.

        Reserved names would shadow built-in CEL namespaces, making
        the signal inaccessible or causing ambiguous resolution.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "payload",
                "source_path": "some.path",
                "on_missing": "error",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            WorkflowSignalMapping.objects.filter(workflow=workflow).count(),
            0,
        )

    def test_create_duplicate_name_returns_error(self):
        """POST with a name that already exists in the workflow must be
        rejected.

        Duplicate signal names would make ``s.<name>`` ambiguous in
        CEL expressions, leading to non-deterministic resolution.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
        )
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "emissivity",
                "source_path": "different.path",
                "on_missing": "error",
            },
        )

        self.assertEqual(response.status_code, 200)
        # Still only one mapping
        self.assertEqual(
            WorkflowSignalMapping.objects.filter(workflow=workflow).count(),
            1,
        )

    def test_create_prefill_from_query_params(self):
        """GET with prefill_name and prefill_path query params must
        pre-populate the form fields.

        This is used by the sample data panel: each candidate's "Add"
        button links to the create URL with pre-fill params so the
        author doesn't have to retype the discovered path and name.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.get(
            url + "?prefill_name=temperature&prefill_path=sensors.temp",
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial.get("name"), "temperature")
        self.assertEqual(form.initial.get("source_path"), "sensors.temp")

    def test_create_with_default_value_json(self):
        """POST with a valid JSON default_value must persist the parsed
        value on the mapping.

        Authors may supply fallback values of any JSON type.  The form
        accepts raw JSON strings and the view parses them before saving.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "target_eui",
                "source_path": "energy.target_eui",
                "on_missing": "null",
                "default_value": "42.5",
                "data_type": "number",
            },
        )

        self.assertEqual(response.status_code, 204)
        mapping = WorkflowSignalMapping.objects.get(workflow=workflow)
        self.assertEqual(mapping.default_value, 42.5)
        self.assertEqual(mapping.data_type, "number")

    def test_create_invalid_default_value_returns_error(self):
        """POST with invalid JSON in default_value must be rejected.

        If we stored raw strings that aren't valid JSON, the resolver
        would fail at runtime when trying to parse the default.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "target_eui",
                "source_path": "energy.target_eui",
                "on_missing": "error",
                "default_value": "not valid json {{{",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            WorkflowSignalMapping.objects.filter(workflow=workflow).count(),
            0,
        )


# ── Edit signal mapping ──────────────────────────────────────────────
# The edit view pre-populates the form from the existing mapping and
# updates it on valid POST.


class TestSignalMappingEditView(TestCase):
    """Tests for the WorkflowSignalMappingEditView."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_edit_returns_prepopulated_form(self):
        """GET must return a form pre-populated with the mapping's values.

        If the form isn't pre-populated, the author has to re-enter all
        fields when making a small change, which is error-prone and
        frustrating.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
            default_value=0.9,
        )
        url = reverse(
            "workflows:workflow_signal_mapping_edit",
            kwargs={"pk": workflow.pk, "mapping_id": mapping.pk},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial["name"], "emissivity")
        self.assertEqual(form.initial["source_path"], "materials[0].emissivity")
        self.assertEqual(form.initial["on_missing"], "error")
        self.assertEqual(form.initial["default_value"], "0.9")

    def test_edit_valid_updates_mapping(self):
        """POST with valid data must update the existing mapping and
        return 204 with signals-changed event.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
        )
        url = reverse(
            "workflows:workflow_signal_mapping_edit",
            kwargs={"pk": workflow.pk, "mapping_id": mapping.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "emissivity_updated",
                "source_path": "materials[1].emissivity",
                "on_missing": "null",
                "default_value": "",
                "data_type": "",
            },
        )

        self.assertEqual(response.status_code, 204)
        mapping.refresh_from_db()
        self.assertEqual(mapping.name, "emissivity_updated")
        self.assertEqual(mapping.source_path, "materials[1].emissivity")
        self.assertEqual(mapping.on_missing, "null")

    def test_edit_nonexistent_returns_404(self):
        """Editing a mapping that doesn't exist must return 404.

        This prevents confusing errors if a mapping was deleted by
        another user while the edit modal was open.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_edit",
            kwargs={"pk": workflow.pk, "mapping_id": 99999},
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 404)

    def test_edit_keeps_same_name_valid(self):
        """Editing a mapping and keeping the same name must not trigger
        a uniqueness error.

        The form passes exclude_mapping_id so the uniqueness check
        skips the mapping being edited.  Without this, every edit
        would fail with 'name already exists'.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
            on_missing="error",
        )
        url = reverse(
            "workflows:workflow_signal_mapping_edit",
            kwargs={"pk": workflow.pk, "mapping_id": mapping.pk},
        )

        response = self.client.post(
            url,
            {
                "name": "emissivity",
                "source_path": "materials[1].emissivity",
                "on_missing": "null",
                "default_value": "",
                "data_type": "",
            },
        )

        self.assertEqual(response.status_code, 204)
        mapping.refresh_from_db()
        self.assertEqual(mapping.source_path, "materials[1].emissivity")


# ── Delete signal mapping ────────────────────────────────────────────
# The delete view removes the mapping and fires the signals-changed
# event so the table reloads.


class TestSignalMappingDeleteView(TestCase):
    """Tests for the WorkflowSignalMappingDeleteView."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_delete_removes_mapping(self):
        """POST must delete the mapping and return 204.

        After deletion, the mapping must no longer exist in the
        database.  The signals-changed event in the response header
        triggers the table to reload without the deleted row.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
        )
        url = reverse(
            "workflows:workflow_signal_mapping_delete",
            kwargs={"pk": workflow.pk, "mapping_id": mapping.pk},
        )

        response = self.client.post(url, headers={"x-csrftoken": "dummy"})

        self.assertEqual(response.status_code, 204)
        self.assertIn("signals-changed", response["HX-Trigger"])
        self.assertFalse(
            WorkflowSignalMapping.objects.filter(pk=mapping.pk).exists(),
        )

    def test_delete_requires_manage_permission(self):
        """An executor must not be able to delete signal mappings.

        Delete is a destructive operation on the workflow's signal
        configuration.  Only authors should be able to modify signals.
        """
        workflow = WorkflowFactory()
        executor = UserFactory()
        grant_role(executor, workflow.org, RoleCode.EXECUTOR)
        executor.set_current_org(workflow.org)
        self.client.force_login(executor)
        session = self.client.session
        session["active_org_id"] = workflow.org_id
        session.save()

        mapping = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="emissivity",
            source_path="materials[0].emissivity",
        )
        url = reverse(
            "workflows:workflow_signal_mapping_delete",
            kwargs={"pk": workflow.pk, "mapping_id": mapping.pk},
        )

        response = self.client.post(url)

        self.assertEqual(response.status_code, 403)
        # Mapping should still exist
        self.assertTrue(
            WorkflowSignalMapping.objects.filter(pk=mapping.pk).exists(),
        )


# ── Move signal mapping ──────────────────────────────────────────────
# The move view swaps position values with the adjacent mapping.


class TestSignalMappingMoveView(TestCase):
    """Tests for the WorkflowSignalMappingMoveView."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_move_down_swaps_positions(self):
        """Moving a mapping down must swap its position with the next one.

        Position ordering controls the display order in the UI and
        the resolution order in the runtime.  If swapping is broken,
        authors lose the ability to control signal evaluation order.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        m1 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="first",
            source_path="a",
            position=10,
        )
        m2 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="second",
            source_path="b",
            position=20,
        )
        url = reverse(
            "workflows:workflow_signal_mapping_move",
            kwargs={"pk": workflow.pk, "mapping_id": m1.pk},
        )

        response = self.client.post(
            url,
            {"direction": "down"},
        )

        self.assertEqual(response.status_code, 204)
        m1.refresh_from_db()
        m2.refresh_from_db()
        # After moving first down, second should come first
        self.assertGreater(m1.position, m2.position)

    def test_move_up_swaps_positions(self):
        """Moving a mapping up must swap its position with the previous one."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        m1 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="first",
            source_path="a",
            position=10,
        )
        m2 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="second",
            source_path="b",
            position=20,
        )
        url = reverse(
            "workflows:workflow_signal_mapping_move",
            kwargs={"pk": workflow.pk, "mapping_id": m2.pk},
        )

        response = self.client.post(
            url,
            {"direction": "up"},
        )

        self.assertEqual(response.status_code, 204)
        m1.refresh_from_db()
        m2.refresh_from_db()
        # After moving second up, it should come first
        self.assertLess(m2.position, m1.position)

    def test_move_first_up_no_change(self):
        """Moving the first mapping up must be a no-op (204 with no
        position change).

        The UI disables the up button for the first row, but the
        server must also handle it gracefully in case of stale HTML.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        m1 = WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="first",
            source_path="a",
            position=10,
        )
        original_position = m1.position
        url = reverse(
            "workflows:workflow_signal_mapping_move",
            kwargs={"pk": workflow.pk, "mapping_id": m1.pk},
        )

        response = self.client.post(
            url,
            {"direction": "up"},
        )

        self.assertEqual(response.status_code, 204)
        m1.refresh_from_db()
        self.assertEqual(m1.position, original_position)


# ── POST /workflows/<pk>/signals/sample-data/ ─────────────────────────
# The sample-data endpoint accepts pasted JSON or XML, traverses the
# structure, and returns candidate signal mappings.  This powers the
# "paste sample data" feature in the signal editor.


class TestSignalMappingSampleDataView(TestCase):
    """Tests for the WorkflowSignalMappingSampleDataView POST endpoint."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_sample_data_post_parses_json(self):
        """POST with valid JSON sample data must return candidate signals.

        JSON is the most common submission format.  If parsing fails or
        produces no candidates, the sample-data feature is broken for
        the majority of workflows.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        sample = json.dumps({"temperature": 22.5, "humidity": 45})
        response = self.client.post(url, {"sample_data": sample})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("candidates", data)
        self.assertGreater(len(data["candidates"]), 0)

    def test_sample_data_post_parses_xml(self):
        """POST with valid XML sample data must return candidate signals.

        XML is common in building-energy domains (gbXML, IDF wrappers).
        The view falls back to XML parsing when JSON fails, so this test
        confirms the fallback path works end-to-end.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        sample = "<root><temperature>22.5</temperature><humidity>45</humidity></root>"
        response = self.client.post(url, {"sample_data": sample})

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("candidates", data)
        self.assertGreater(len(data["candidates"]), 0)

    def test_sample_data_post_empty_returns_400(self):
        """POST with no sample data must return 400.

        An empty payload means the author clicked "parse" without pasting
        anything.  Returning 400 with a clear error message lets the UI
        display inline feedback rather than failing silently.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(url, {"sample_data": ""})

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("error", data)

    def test_sample_data_post_invalid_returns_400(self):
        """POST with unparseable data (neither JSON nor XML) must return 400.

        The view tries JSON first, then XML.  If both fail, a 400 with
        a descriptive error is the correct response so the UI can tell the
        author their paste was invalid.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(url, {"sample_data": "not json or xml {{{"})

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertIn("error", data)

    def test_sample_data_candidates_have_correct_shape(self):
        """Each candidate must have path, value, and suggested_name keys.

        The front-end signal editor reads these three keys to populate
        the candidate row: the data path for the source_path field, the
        sampled value for preview, and the suggested name for the signal
        name input.  Missing any key would break the UI binding.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        sample = json.dumps({"floor_area": 120.5})
        response = self.client.post(url, {"sample_data": sample})
        data = json.loads(response.content)

        self.assertEqual(len(data["candidates"]), 1)
        candidate = data["candidates"][0]
        self.assertIn("path", candidate)
        self.assertIn("value", candidate)
        self.assertIn("suggested_name", candidate)
        self.assertEqual(candidate["path"], "floor_area")
        self.assertEqual(candidate["suggested_name"], "floor_area")

    def test_sample_data_deduplicates_names(self):
        """When multiple fields share the same leaf key, suggested_name must
        get numeric suffixes to avoid collisions.

        Without deduplication, the signal editor would pre-fill duplicate
        names, and saving them would violate the unique (workflow, name)
        constraint at the database level.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        # Nested structure where "value" appears at multiple leaf paths
        sample = json.dumps(
            {
                "sensor_a": {"value": 10},
                "sensor_b": {"value": 20},
                "sensor_c": {"value": 30},
            }
        )
        response = self.client.post(url, {"sample_data": sample})
        data = json.loads(response.content)

        suggested_names = [c["suggested_name"] for c in data["candidates"]]
        # All suggested names must be unique
        self.assertEqual(len(suggested_names), len(set(suggested_names)))
        # The first occurrence keeps the base name; subsequent ones get suffixes
        self.assertIn("value", suggested_names)

    def test_sample_data_sanitizes_suggested_names(self):
        """Keys with special characters must produce valid CEL identifiers.

        Real-world JSON payloads contain keys like ``"a&b"``,
        ``"surface type"``, or ``"100%"``.  These cannot be used as
        signal names directly because signal names must match the CEL
        identifier regex ``^[a-zA-Z_][a-zA-Z0-9_]*$``.  The view must
        strip non-identifier characters, replace spaces/hyphens with
        underscores, and fall back to ``field_<index>`` for keys that
        reduce to an empty string.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        sample = json.dumps(
            {
                "a&b": 1,
                "surface type": 2,
                "100percent": 3,
                "!!!": 4,
            }
        )
        response = self.client.post(url, {"sample_data": sample})
        data = json.loads(response.content)

        names = {c["suggested_name"] for c in data["candidates"]}
        self.assertIn("ab", names)  # stripped &
        self.assertIn("surface_type", names)  # space → _
        self.assertIn("_100percent", names)  # digit-leading → prefixed
        self.assertIn("field_3", names)  # all-special → fallback

    def test_sample_data_htmx_returns_html_partial(self):
        """POST with HX-Request header must return an HTML partial.

        The sample data panel uses HTMx to swap the results into the
        page.  Non-HTMx requests still return JSON for backward
        compatibility.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_signal_mapping_sample_data",
            kwargs={"pk": workflow.pk},
        )

        sample = json.dumps({"temperature": 22.5})
        response = self.client.post(
            url, {"sample_data": sample}, headers={"hx-request": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(
            response,
            "workflows/partials/sample_data_results.html",
        )
        self.assertContains(response, "temperature")


# ── Output promotion ──────────────────────────────────────────────────
# The promote output view toggles signal_name on a SignalDefinition
# to make a validator output available in the s.* namespace for
# downstream steps.


class TestPromoteOutputView(TestCase):
    """Tests for the WorkflowStepPromoteOutputView."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def _create_output_signal(self, workflow, step=None):
        """Create a step with an output SignalDefinition for testing."""
        if step is None:
            validator = ValidatorFactory(
                org=workflow.org,
                validation_type=ValidationType.BASIC,
                is_system=False,
            )
            step = WorkflowStepFactory(
                workflow=workflow,
                validator=validator,
                order=10,
            )
        return SignalDefinition.objects.create(
            workflow_step=step,
            contract_key="site_eui",
            direction=SignalDirection.OUTPUT,
            signal_name="",
        )

    def test_promote_sets_signal_name(self):
        """POST with a valid signal_name must set it on the
        SignalDefinition and return 204 with signals-changed event.

        This is the core promotion flow: an author names a validator
        output as a signal so downstream steps can reference it via
        ``s.<name>`` in CEL expressions.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        signal_def = self._create_output_signal(workflow)

        url = reverse(
            "workflows:workflow_step_promote_output",
            kwargs={
                "pk": workflow.pk,
                "step_id": signal_def.workflow_step_id,
                "signal_id": signal_def.pk,
            },
        )

        response = self.client.post(url, {"signal_name": "eui"})

        self.assertEqual(response.status_code, 204)
        self.assertIn("signals-changed", response["HX-Trigger"])
        signal_def.refresh_from_db()
        self.assertEqual(signal_def.signal_name, "eui")

    def test_unpromote_clears_signal_name(self):
        """POST with an empty signal_name must clear the promotion.

        Authors need to be able to remove a promotion if they change
        their mind or if the signal name conflicts.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        signal_def = self._create_output_signal(workflow)
        signal_def.signal_name = "eui"
        signal_def.save(update_fields=["signal_name"])

        url = reverse(
            "workflows:workflow_step_promote_output",
            kwargs={
                "pk": workflow.pk,
                "step_id": signal_def.workflow_step_id,
                "signal_id": signal_def.pk,
            },
        )

        response = self.client.post(url, {"signal_name": ""})

        self.assertEqual(response.status_code, 204)
        signal_def.refresh_from_db()
        self.assertEqual(signal_def.signal_name, "")

    def test_promote_duplicate_name_returns_error(self):
        """POST with a signal_name that already exists in the workflow
        (as a mapping or another promoted output) must return 400.

        Cross-table uniqueness prevents ambiguous s.<name> references
        where two different sources claim the same name.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        # Create a mapping that uses the name "eui"
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="eui",
            source_path="energy.eui",
        )
        signal_def = self._create_output_signal(workflow)

        url = reverse(
            "workflows:workflow_step_promote_output",
            kwargs={
                "pk": workflow.pk,
                "step_id": signal_def.workflow_step_id,
                "signal_id": signal_def.pk,
            },
        )

        response = self.client.post(url, {"signal_name": "eui"})

        self.assertEqual(response.status_code, 400)
        signal_def.refresh_from_db()
        self.assertEqual(signal_def.signal_name, "")

    def test_promote_invalid_name_returns_error(self):
        """POST with an invalid CEL identifier must return 400.

        The signal name must be a valid CEL identifier so it can be
        used as s.<name> in expressions.  Non-identifiers would cause
        CEL parse errors at runtime.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        signal_def = self._create_output_signal(workflow)

        url = reverse(
            "workflows:workflow_step_promote_output",
            kwargs={
                "pk": workflow.pk,
                "step_id": signal_def.workflow_step_id,
                "signal_id": signal_def.pk,
            },
        )

        response = self.client.post(url, {"signal_name": "123bad"})

        self.assertEqual(response.status_code, 400)
        signal_def.refresh_from_db()
        self.assertEqual(signal_def.signal_name, "")
