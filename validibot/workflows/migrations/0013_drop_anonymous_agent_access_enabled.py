# Generated manually on 2026-04-13

from django.db import migrations


class Migration(migrations.Migration):
    """Drop the stray ``anonymous_agent_access_enabled`` column.

    An earlier (uncommitted) version of migration 0011 renamed
    ``agent_access_enabled`` to ``anonymous_agent_access_enabled``.  That
    rename was reverted in-place before 0011 was committed, but any
    database that applied the pre-revert version still has the old column
    alongside the correct one.  With a NOT NULL constraint and no default,
    the orphan column causes IntegrityError on every workflow insert.

    This migration drops the column if it exists and is a no-op otherwise,
    so it is safe to apply to any environment regardless of whether the
    drift is present.

    ``state_operations=[]`` is required because the column never existed
    in the current Django model state — we are only correcting the
    database, not the model.
    """

    dependencies = [
        ("workflows", "0012_uppercase_agent_billing_mode"),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE workflows_workflow "
                "DROP COLUMN IF EXISTS anonymous_agent_access_enabled"
            ),
            reverse_sql=migrations.RunSQL.noop,
            state_operations=[],
        ),
    ]
