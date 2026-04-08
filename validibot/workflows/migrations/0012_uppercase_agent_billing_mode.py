"""Uppercase agent_billing_mode values.

Standardises the DB-stored values from lowercase (``author_pays``,
``agent_pays_x402``) to uppercase (``AUTHOR_PAYS``, ``AGENT_PAYS_X402``)
to match the Python constant names.  Also updates the field definition
so Django's ``choices`` and ``default`` use the new values.
"""

from django.db import migrations
from django.db import models


def uppercase_billing_mode(apps, schema_editor):
    Workflow = apps.get_model("workflows", "Workflow")
    Workflow.objects.filter(agent_billing_mode="author_pays").update(
        agent_billing_mode="AUTHOR_PAYS",
    )
    Workflow.objects.filter(agent_billing_mode="agent_pays_x402").update(
        agent_billing_mode="AGENT_PAYS_X402",
    )


def lowercase_billing_mode(apps, schema_editor):
    Workflow = apps.get_model("workflows", "Workflow")
    Workflow.objects.filter(agent_billing_mode="AUTHOR_PAYS").update(
        agent_billing_mode="author_pays",
    )
    Workflow.objects.filter(agent_billing_mode="AGENT_PAYS_X402").update(
        agent_billing_mode="agent_pays_x402",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0011_rename_agent_access_and_billing_mode"),
    ]

    operations = [
        migrations.RunPython(
            uppercase_billing_mode,
            reverse_code=lowercase_billing_mode,
        ),
        migrations.AlterField(
            model_name="workflow",
            name="agent_billing_mode",
            field=models.CharField(
                choices=[
                    ("AUTHOR_PAYS", "Author pays (plan quota)"),
                    ("AGENT_PAYS_X402", "Agent pays via x402 micropayment"),
                ],
                default="AUTHOR_PAYS",
                help_text=(
                    "Who pays when an agent invokes this workflow. "
                    "AUTHOR_PAYS uses your plan quota (authenticated agents only). "
                    "AGENT_PAYS_X402 requires agents to pay per call via x402."
                ),
                max_length=30,
            ),
        ),
    ]
