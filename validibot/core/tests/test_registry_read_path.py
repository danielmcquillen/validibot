"""
Regression tests for the scheduled-task registry read-path contract.

### What's being pinned

The registry has two write paths (static built-ins in
``SCHEDULED_ADMIN_TASKS``, runtime registration via
``register_scheduled_admin_task``) but only ONE supported read
path: the accessor functions (``get_all_admin_tasks``,
``get_admin_tasks_for_backend``, ``get_admin_task_by_id``,
``get_enabled_admin_tasks``). Consumers that read
``SCHEDULED_ADMIN_TASKS`` directly silently miss every
extension task.

This exact bug was caught by a post-merge review: the
``sync_schedules --list`` command was iterating
``SCHEDULED_ADMIN_TASKS`` directly, so the cloud-registered
license-hash task never showed up in the operator-visible task
listing. The fix routed ``--list`` through ``get_all_admin_tasks``.

These tests pin both the positive contract (registered dynamic
tasks surface via accessors) and the regression hazard (every
known consumer of the registry reads through accessors).
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from validibot.core.tasks.registry import Backend
from validibot.core.tasks.registry import ScheduledAdminTaskDefinition
from validibot.core.tasks.registry import get_admin_task_by_id
from validibot.core.tasks.registry import get_admin_tasks_for_backend
from validibot.core.tasks.registry import get_all_admin_tasks
from validibot.core.tasks.registry import get_enabled_admin_tasks
from validibot.core.tasks.registry import register_scheduled_admin_task
from validibot.core.tasks.registry import reset_dynamic_tasks

# Synthetic task used across all tests. Declared once at module
# scope so the name / id / celery task string are consistent.
_TEST_TASK = ScheduledAdminTaskDefinition(
    id="test-dynamic-task",
    name="Synthetic Dynamic Test Task",
    celery_task="tests.synthetic_dynamic_task",
    api_endpoint="/api/v1/scheduled/test-dynamic-task/",
    schedule_cron="0 0 * * *",
    description=(
        "Synthetic task registered at runtime to verify the "
        "registry read-path contract."
    ),
)


@pytest.fixture
def dynamic_task():
    """Register ``_TEST_TASK`` at fixture setup, clean up at teardown.

    Using a fixture (rather than a module-level register) keeps
    the registration scope tight — only tests that ask for the
    fixture see the task. Other tests in this file or others
    that inspect the registry are unaffected.
    """
    reset_dynamic_tasks()
    register_scheduled_admin_task(_TEST_TASK)
    yield _TEST_TASK
    reset_dynamic_tasks()


class TestAccessorsSeeDynamicTasks:
    """The accessor functions must surface runtime-registered tasks.

    If any of these fail, a consumer calling the corresponding
    accessor would miss extension tasks — the same class of bug
    that broke ``sync_schedules --list``.
    """

    def test_get_all_admin_tasks_includes_dynamic(self, dynamic_task):
        """``get_all_admin_tasks`` must return both static and dynamic
        tasks in one tuple — this is the canonical "full registry"
        reader.
        """
        all_ids = {t.id for t in get_all_admin_tasks()}
        assert dynamic_task.id in all_ids

    def test_get_admin_tasks_for_backend_includes_dynamic(self, dynamic_task):
        """``get_admin_tasks_for_backend`` must include dynamic tasks
        that support the requested backend.

        The synthetic task defaults to all backends, so it should
        appear for every backend filter.
        """
        for backend in (Backend.CELERY, Backend.GCP):
            ids = {t.id for t in get_admin_tasks_for_backend(backend)}
            assert dynamic_task.id in ids, (
                f"dynamic task missing from backend={backend.value} listing — "
                "get_admin_tasks_for_backend is reading SCHEDULED_ADMIN_TASKS "
                "directly instead of get_all_admin_tasks"
            )

    def test_get_admin_task_by_id_finds_dynamic(self, dynamic_task):
        """``get_admin_task_by_id`` must find dynamically-registered
        tasks. A lookup that reads only the static tuple would
        return ``None`` for any extension task id.
        """
        found = get_admin_task_by_id(dynamic_task.id)
        assert found is not None
        assert found.id == dynamic_task.id

    def test_get_enabled_admin_tasks_includes_dynamic(self, dynamic_task):
        """``get_enabled_admin_tasks`` must include enabled dynamic
        tasks (the synthetic task defaults to ``enabled=True``).
        """
        ids = {t.id for t in get_enabled_admin_tasks()}
        assert dynamic_task.id in ids


class TestSyncSchedulesListSeesDynamicTasks:
    """``python manage.py sync_schedules --list`` must surface
    runtime-registered tasks.

    This is the regression test for the original bug: the
    management command read ``SCHEDULED_ADMIN_TASKS`` directly,
    so cloud-registered tasks were invisible to operators running
    ``--list`` to audit what was scheduled.
    """

    def test_list_text_output_includes_dynamic_task(self, dynamic_task):
        """The human-readable ``--list`` output must mention the
        dynamic task's name or id somewhere.

        We don't pin exact formatting — that changes cosmetically
        over time — just that the task appears in the listing at
        all.
        """
        out = io.StringIO()
        call_command("sync_schedules", "--list", stdout=out)
        output = out.getvalue()

        assert dynamic_task.id in output or dynamic_task.name in output, (
            "sync_schedules --list text output does not include the "
            "dynamic task — the command is reading SCHEDULED_ADMIN_TASKS "
            "directly again"
        )

    def test_list_json_output_includes_dynamic_task(self, dynamic_task):
        """The JSON-formatted output must include the dynamic task
        too — tooling that parses ``--format=json`` depends on
        parity with the text output.
        """
        import json

        out = io.StringIO()
        call_command(
            "sync_schedules",
            "--list",
            "--format=json",
            stdout=out,
        )
        payload = json.loads(out.getvalue())

        ids = {entry["id"] for entry in payload}
        assert dynamic_task.id in ids


class TestRegisterScheduledAdminTaskGuards:
    """The register function has safety rails — duplicate ids must
    raise rather than silently clobber.

    A silent overwrite would be the worst kind of bug here: two
    AppConfigs registering the same id would both think they won,
    and whichever ran last would set the schedule. Raising makes
    the collision visible at Django startup.
    """

    def test_duplicate_registration_raises(self):
        """Registering the same task id twice must raise ValueError
        so the conflict is visible at boot, not silently resolved.
        """
        reset_dynamic_tasks()
        try:
            register_scheduled_admin_task(_TEST_TASK)
            with pytest.raises(ValueError, match=_TEST_TASK.id):
                register_scheduled_admin_task(_TEST_TASK)
        finally:
            reset_dynamic_tasks()
