Validibot self-hosted support bundle
=====================================

This zip contains diagnostic data from a self-hosted Validibot
deployment, suitable for emailing to support@validibot.com.

What's inside:

  app-snapshot.json      Django-side snapshot conforming to the
                         validibot.support-bundle.v1 schema. Sensitive
                         setting values are redacted with [REDACTED];
                         non-sensitive values are preserved verbatim.
                         If this file is missing, the web container
                         was down at bundle time and only host-side
                         data was captured. Look for app-snapshot-
                         error.log in that case.

  service-status.txt     docker compose ps output.

  recent-web.log         Last 200 lines of web container logs.
  recent-worker.log      Last 200 lines of worker container logs.
  recent-postgres.log    Last 200 lines of postgres container logs.

  disk-usage.txt         df -h output from the host.

  validators.txt         Validator backend inventory: backend slug,
                         OCI version label, image digest, size.

  versions.txt           Docker / OS / just versions on the host.

What's NOT inside (redacted or intentionally excluded):

  - Secrets, API tokens, signing keys, passwords. The app-snapshot
    redacts these by setting name and by value shape (PEM, JWT,
    bearer tokens, embedded URL credentials).

  - Raw submission contents. Operator data never leaves this host.

  - Validation findings or evidence bundles. Those are separate
    artefacts; share via your existing channel if support needs
    them.

  - Database contents. The bundle is metadata-only. No SQL dumps.

Operators are encouraged to spot-check the bundle before sending:

  unzip -l support-bundle-*.zip          # list contents
  unzip -p support-bundle-*.zip app-snapshot.json | jq .   # inspect

Send to support@validibot.com with a brief description of the
problem you're trying to diagnose.
