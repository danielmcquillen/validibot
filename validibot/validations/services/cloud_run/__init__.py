"""
GCP-specific services for advanced validation execution.

This package provides Django-side infrastructure for triggering Cloud Run Job
validators on Google Cloud Platform and receiving their results via callbacks.
For other deployment targets, see the services/execution/ package.

Modules:
    envelope_builder: Creates typed input envelopes from Django models (shared)
    gcs_client: Uploads/downloads envelopes to Google Cloud Storage (GCP only)
    job_client: Triggers Cloud Run Jobs via the Jobs API (GCP only)
    launcher: Orchestrates the full GCP validation launch flow
"""
