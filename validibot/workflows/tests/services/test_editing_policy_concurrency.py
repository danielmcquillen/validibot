"""PostgreSQL concurrency regressions for admission versus Mutable editing.

Row-lock behavior cannot be represented faithfully with mocks or SQLite. These
tests use independent database connections to prove the shared workflow lock
orders the two transactions and prevents a run from seeing a partial change.
"""

from __future__ import annotations

from queue import Queue
from threading import Event
from threading import Thread

import pytest
from django.db import close_old_connections
from django.db import transaction

from validibot.submissions.constants import OutputRetention
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ValidationRunSource
from validibot.validations.models import ValidationRun
from validibot.validations.services.run_admission import admit_validation_run
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.models import Workflow
from validibot.workflows.services.editing_policy import WorkflowDefinitionInUseError
from validibot.workflows.services.editing_policy import (
    guard_workflow_definition_mutation,
)
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db(transaction=True)

SHORT_BLOCKING_WINDOW_SECONDS = 0.2
THREAD_TIMEOUT_SECONDS = 5


def _thread(target, errors: Queue[BaseException]) -> Thread:
    """Run a database worker and surface its unexpected exception to the test."""

    def checked_target():
        close_old_connections()
        try:
            target()
        except BaseException as exc:
            errors.put(exc)
        finally:
            close_old_connections()

    worker = Thread(target=checked_target, daemon=True)
    worker.start()
    return worker


def test_admission_commit_first_causes_semantic_save_to_retry():
    """A committed run marker wins the race and leaves no partial definition edit."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    submission = SubmissionFactory(workflow=workflow)
    admission_has_lock = Event()
    allow_admission_commit = Event()
    mutation_finished = Event()
    errors: Queue[BaseException] = Queue()
    outcome: dict[str, object] = {}

    def admit_while_holding_outer_transaction():
        with transaction.atomic():
            admit_validation_run(
                org=workflow.org,
                workflow=workflow,
                submission=submission,
                user=workflow.user,
                source=ValidationRunSource.LAUNCH_PAGE,
            )
            admission_has_lock.set()
            assert allow_admission_commit.wait(THREAD_TIMEOUT_SECONDS)

    def attempt_mutation():
        assert admission_has_lock.wait(THREAD_TIMEOUT_SECONDS)
        try:
            with guard_workflow_definition_mutation(workflow.pk):
                Workflow.objects.filter(pk=workflow.pk).update(
                    allowed_file_types=["json", "text"],
                )
        except WorkflowDefinitionInUseError as exc:
            outcome["error"] = exc
        finally:
            mutation_finished.set()

    admission_thread = _thread(admit_while_holding_outer_transaction, errors)
    assert admission_has_lock.wait(THREAD_TIMEOUT_SECONDS)
    mutation_thread = _thread(attempt_mutation, errors)

    assert not mutation_finished.wait(SHORT_BLOCKING_WINDOW_SECONDS)
    allow_admission_commit.set()
    admission_thread.join(THREAD_TIMEOUT_SECONDS)
    mutation_thread.join(THREAD_TIMEOUT_SECONDS)

    assert errors.empty(), list(errors.queue)
    assert isinstance(outcome.get("error"), WorkflowDefinitionInUseError)
    workflow.refresh_from_db()
    assert workflow.allowed_file_types == ["json"]


def test_semantic_commit_first_is_visible_to_later_admission():
    """A save that wins the lock race commits wholly before launch snapshots it."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    submission = SubmissionFactory(workflow=workflow)
    mutation_has_lock = Event()
    allow_mutation_commit = Event()
    admission_finished = Event()
    errors: Queue[BaseException] = Queue()
    outcome: dict[str, object] = {}

    def mutate_while_holding_transaction():
        with guard_workflow_definition_mutation(workflow.pk):
            Workflow.objects.filter(pk=workflow.pk).update(
                output_retention=OutputRetention.STORE_PERMANENTLY,
            )
            mutation_has_lock.set()
            assert allow_mutation_commit.wait(THREAD_TIMEOUT_SECONDS)

    def attempt_admission():
        assert mutation_has_lock.wait(THREAD_TIMEOUT_SECONDS)
        outcome["run"] = admit_validation_run(
            org=workflow.org,
            workflow=workflow,
            submission=submission,
            user=workflow.user,
            source=ValidationRunSource.LAUNCH_PAGE,
        )
        admission_finished.set()

    mutation_thread = _thread(mutate_while_holding_transaction, errors)
    assert mutation_has_lock.wait(THREAD_TIMEOUT_SECONDS)
    admission_thread = _thread(attempt_admission, errors)

    assert not admission_finished.wait(SHORT_BLOCKING_WINDOW_SECONDS)
    allow_mutation_commit.set()
    mutation_thread.join(THREAD_TIMEOUT_SECONDS)
    admission_thread.join(THREAD_TIMEOUT_SECONDS)

    assert errors.empty(), list(errors.queue)
    run = outcome.get("run")
    assert isinstance(run, ValidationRun)
    assert run.output_retention_policy == OutputRetention.STORE_PERMANENTLY
