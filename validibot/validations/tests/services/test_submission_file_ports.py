"""Tests for dispatch-time submitted artifact-port file materialization.

The workflow launch page persists extra files for submitted artifact ports.
These tests verify that execution backends can turn those persisted files into
port-keyed runtime URIs for validator envelopes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from validibot.submissions.models import SubmissionInputFile
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.services.submission_file_ports import (
    upload_submitted_input_files_to_gcs,
)
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def test_upload_submitted_input_files_to_gcs_uses_port_keyed_uri(tmp_path):
    """Cloud dispatch should copy submitted port files into the run bundle."""

    with override_settings(MEDIA_ROOT=str(tmp_path)):
        submission = SubmissionFactory()
        step = WorkflowStepFactory(workflow=submission.workflow)
        port_file = SubmissionInputFile(
            submission=submission,
            workflow_step=step,
            port_key="weather_file",
        )
        port_file.set_file(
            uploaded_file=SimpleUploadedFile(
                "weather.epw",
                b"LOCATION,Test Weather",
                content_type="application/vnd.energyplus.epw",
            ),
            filename="weather.epw",
        )
        port_file.full_clean()
        port_file.save()

        with patch(
            "validibot.validations.services.cloud_run.gcs_client.upload_file"
        ) as upload_file:
            uri_map = upload_submitted_input_files_to_gcs(
                submission=submission,
                step=step,
                execution_bundle_uri="gs://bucket/runs/org/run",
            )

    assert uri_map["weather_file"].startswith(
        "gs://bucket/runs/org/run/submitted/weather_file/"
    )
    assert uri_map["weather_file"].endswith(".epw")
    upload_file.assert_called_once()
    kwargs = upload_file.call_args.kwargs
    assert kwargs["content"] == b"LOCATION,Test Weather"
    assert kwargs["uri"] == uri_map["weather_file"]
    assert kwargs["content_type"] == "application/vnd.energyplus.epw"
