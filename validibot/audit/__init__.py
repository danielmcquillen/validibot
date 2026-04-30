"""Append-only audit log for configuration changes and security events.

This app is Pillar 3 of the four-pillar observability taxonomy. It
complements but does not replace:

* **Application logs** (Pillar 1) — unstructured debug/error traces in
  Cloud Logging.
* **Product analytics** (``validibot.tracking``, Pillar 2) — high-volume
  behavioural events.
* **Specialised ledgers** (Pillar 4) — domain-specific immutable tables
  like ``X402Payment`` or ``LicenseAcceptance`` that live in cloud.

The models live in community so self-hosted Pro deployments get the
same compliance-grade audit trail that the hosted cloud offering does.
A later phase adds the ``AUDIT_LOG``-gated Pro UI for listing,
exporting, and drilling into entries.
"""
