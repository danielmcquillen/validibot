from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0014_validationrunstepsummary_validationrunsummary_and_more"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="ValidationRunStepSummary",
            new_name="ValidationStepRunSummary",
        ),
        migrations.RenameIndex(
            model_name="validationsteprunsummary",
            new_name="validations_summary_4cdb67_idx",
            old_name="validations_summary_fe9733_idx",
        ),
        migrations.AlterField(
            model_name="validationsteprunsummary",
            name="summary",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="step_summaries",
                to="validations.validationrunsummary",
            ),
        ),
        migrations.RemoveField(
            model_name="validationsteprunsummary",
            name="workflow_step",
        ),
        migrations.AddField(
            model_name="validationsteprunsummary",
            name="step_run",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="step_summary",
                to="validations.validationsteprun",
            ),
        ),
    ]
