from django.db import migrations, models
import django.db.models.deletion

import validibot.validations.models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0004_alter_role_code"),
        ("projects", "0002_initial"),
        ("validations", "0027_remove_rulesetassertion_ck_ruleset_assertion_target_oneof_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="FMUModel",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True, default="")),
                ("file", models.FileField(upload_to=validibot.validations.models._fmu_upload_path)),
                ("fmi_version", models.CharField(choices=[("2.0", "FMI 2.0"), ("3.0", "FMI 3.0")], default="2.0", max_length=8)),
                ("kind", models.CharField(choices=[("ModelExchange", "Model Exchange"), ("CoSimulation", "Co-Simulation")], default="CoSimulation", max_length=32)),
                ("is_approved", models.BooleanField(default=False)),
                ("size_bytes", models.BigIntegerField(default=0)),
                ("introspection_metadata", models.JSONField(blank=True, default=dict)),
                ("org", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="fmu_models", to="users.organization")),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fmu_models", to="projects.project")),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="FMUProbeResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("RUNNING", "Running"), ("SUCCEEDED", "Succeeded"), ("FAILED", "Failed")], default="PENDING", max_length=16)),
                ("last_error", models.TextField(blank=True, default="")),
                ("details", models.JSONField(blank=True, default=dict)),
                ("fmu_model", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="probe_result", to="validations.fmumodel")),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="FMIVariable",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                ("causality", models.CharField(max_length=64)),
                ("variability", models.CharField(blank=True, default="", max_length=64)),
                ("value_reference", models.BigIntegerField(default=0)),
                ("value_type", models.CharField(max_length=64)),
                ("unit", models.CharField(blank=True, default="", max_length=128)),
                ("catalog_entry", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fmi_variables", to="validations.validatorcatalogentry")),
                ("fmu_model", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="variables", to="validations.fmumodel")),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="fmivariable",
            index=models.Index(fields=["fmu_model", "name"], name="validations_fmivariable_fmu_model_4e1d1e_idx"),
        ),
        migrations.AddField(
            model_name="validator",
            name="fmu_model",
            field=models.ForeignKey(blank=True, help_text="FMU artifact backing this validator (only used for FMI validators).", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="validators", to="validations.fmumodel"),
        ),
        migrations.AddField(
            model_name="validatorcatalogentry",
            name="default_value",
            field=models.JSONField(blank=True, default=None, help_text="Optional default applied when the signal is hidden.", null=True),
        ),
        migrations.AddField(
            model_name="validatorcatalogentry",
            name="is_hidden",
            field=models.BooleanField(default=False, help_text="Hidden signals remain available to bindings but are not shown in authoring interfaces."),
        ),
    ]
