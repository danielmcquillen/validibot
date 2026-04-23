"""Worker-only API endpoints for the tracking app.

Currently hosts the Cloud Tasks receiver used by
:class:`validibot.tracking.dispatch.cloud_tasks.CloudTasksTrackingDispatcher`.
New async entry points (bulk-replay, deduplication sweep, etc.)
should live here so the public API surface stays separate from
infrastructure-only endpoints.
"""
