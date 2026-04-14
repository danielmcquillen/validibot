"""One-shot wipe of all MFA authenticators.

Rationale:

Before this migration lands, TOTP secrets and recovery-code seeds were
stored in cleartext via allauth's no-op default MFA adapter. We now
encrypt them at rest via ``validibot.users.mfa_adapter.ValidibotMFAAdapter``.
The new adapter's ``decrypt()`` assumes the value is Fernet ciphertext —
handed a legacy cleartext value, it raises ``InvalidToken``, which means
any user enrolled before this migration would be locked into a broken
authenticator that can neither be verified nor deactivated.

Chosen approach (Option C from the design discussion):

Wipe every ``mfa_authenticator`` row. Users re-enroll from scratch under
the new encrypted adapter. This is safe because:

1. Validibot is pre-release with only a handful of test users at the
   time this migration is written; re-enrolment is a small, recoverable
   disruption.
2. The alternative — lazy-accept-then-re-encrypt cleartext on next read
   — leaves plaintext secrets sitting in the database until the user
   happens to log in, which is the vulnerability we're trying to close.
3. A forward-only clean slate is simpler to audit than a conditional
   migration path that has to distinguish encrypted values from
   cleartext.

If this migration is ever applied to a database with real enrolled
users (which should NOT happen — it'd be a deployment bug), those
users are effectively logged out of MFA and will be prompted to
re-enroll on their next visit to the Security page. No password reset
is required.
"""

from django.db import migrations


def wipe_authenticators(apps, schema_editor):
    """Delete every row in the ``mfa_authenticator`` table.

    We load the model via ``apps.get_model`` so the migration runs
    against the historical model state, not the live one — standard
    Django migration hygiene, in case the Authenticator model changes
    shape in a later allauth version.
    """
    Authenticator = apps.get_model("mfa", "Authenticator")
    Authenticator.objects.all().delete()


def reverse_noop(apps, schema_editor):
    """No reverse — we can't un-wipe deleted authenticators.

    Rolling back this migration doesn't restore the rows (and shouldn't,
    since they held cleartext secrets we no longer want anywhere). A
    user who had MFA enabled before the forward migration will still
    need to re-enrol even if the migration is reversed.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0002_organization_trial_fields"),
        ("mfa", "0003_authenticator_type_uniq"),
    ]

    operations = [
        migrations.RunPython(wipe_authenticators, reverse_noop),
    ]
