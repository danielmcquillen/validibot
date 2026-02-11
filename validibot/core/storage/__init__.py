"""
Storage backends for Validibot.

Validibot uses two distinct storage systems:

1. **Media Storage** (Django's STORAGES["default"] and STORAGES["public"])
   - Standard Django media files: user avatars, workflow images, etc.
   - Configured via Django's built-in STORAGES setting
   - Uses django-storages for cloud providers (S3, GCS)
   - Public files served directly via CDN/web server

2. **Data Storage** (this module)
   - Validation pipeline files: submissions, envelopes, validator outputs
   - NEVER publicly accessible - internal use only
   - Supports signed URLs for authenticated downloads
   - Configured via DATA_STORAGE_BACKEND setting

Usage:
    from validibot.core.storage import get_data_storage

    storage = get_data_storage()
    storage.write("runs/run-123/input/envelope.json", envelope_bytes)
    content = storage.read("runs/run-123/output/envelope.json")
    url = storage.get_download_url("runs/run-123/output/artifacts/report.pdf", expires_in=3600)
"""

from validibot.core.storage.base import DataStorage
from validibot.core.storage.registry import get_data_storage

__all__ = [
    "DataStorage",
    "get_data_storage",
]
