"""
Add ``agent_public_discovery`` boolean to Workflow.

Separates two previously-conflated questions:

1. Can agents access this workflow at all (via MCP, for org members)?
   → ``agent_access_enabled``
2. Can agents from outside the org discover and run it (public x402)?
   → ``agent_public_discovery``  (NEW)

The data migration backfills ``agent_public_discovery=True`` for every
workflow that was already published on the public x402 catalog
(``agent_access_enabled=True AND agent_billing_mode='AGENT_PAYS_X402'``).
"""

from django.db import migrations, models


def backfill_public_discovery(apps, schema_editor):
    """Set agent_public_discovery=True for existing public x402 workflows."""
    Workflow = apps.get_model("workflows", "Workflow")
    Workflow.objects.filter(
        agent_access_enabled=True,
        agent_billing_mode="AGENT_PAYS_X402",
    ).update(agent_public_discovery=True)


def reverse_backfill(apps, schema_editor):
    """Clear agent_public_discovery on reverse — safe no-op."""
    Workflow = apps.get_model("workflows", "Workflow")
    Workflow.objects.filter(agent_public_discovery=True).update(
        agent_public_discovery=False,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0013_drop_anonymous_agent_access_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflow",
            name="agent_public_discovery",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "List this workflow on the cross-org public catalog so "
                    "agents outside your organization can discover and run "
                    "it via x402 micropayments. Requires 'Agent access "
                    "enabled' and automatically sets billing to 'Agent pays "
                    "via x402'."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="workflow",
            name="agent_access_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Master switch for all agent access via MCP. When "
                    "enabled, authenticated agents in your organization can "
                    "discover and invoke this workflow. For public cross-org "
                    "discovery, also enable 'Public discovery'."
                ),
            ),
        ),
        migrations.RunPython(
            backfill_public_discovery,
            reverse_code=reverse_backfill,
        ),
    ]
