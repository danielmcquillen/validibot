"""Keep the historical migration slot without certificate-era behavior.

This project no longer ships the old community-side signed certificate
action, and we do not need backward compatibility with that interim
schema. The migration ID remains in place because downstream commercial
packages depend on it in the graph, but it intentionally does nothing
for fresh installs.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0001_initial"),
        ("workflows", "0008_input_schema_fields"),
    ]

    operations = []
