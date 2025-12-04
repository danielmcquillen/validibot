"""
Cloud Run services for advanced validation execution.

This package provides Django-side infrastructure for triggering Cloud Run Job
validators and receiving their results via callbacks.

Modules:
    envelope_builder: Creates typed input envelopes from Django models
    gcs_client: Uploads/downloads envelopes to Google Cloud Storage
    job_client: Triggers Cloud Run Jobs via Cloud Tasks
    token_service: Creates JWT callback tokens signed with GCP KMS
"""
