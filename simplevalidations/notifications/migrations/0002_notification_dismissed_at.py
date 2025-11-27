from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="dismissed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
