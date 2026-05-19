from django.db import migrations

SHACL_OUTPUT_SIGNALS = [
    (
        "parse_ok",
        "Parse OK",
        "boolean",
        "Whether the submitted RDF parsed successfully.",
        10,
    ),
    (
        "parse_serialization",
        "Parse Serialization",
        "string",
        "RDF serialization used by the SHACL parser.",
        20,
    ),
    (
        "triple_count",
        "Triple Count",
        "number",
        "Number of triples in the submitted RDF graph.",
        30,
    ),
    (
        "namespaces_present",
        "Namespaces Present",
        "object",
        "Namespace URI list seen in the submitted RDF graph.",
        40,
    ),
    (
        "has_s223_namespace",
        "Has ASHRAE 223P Namespace",
        "boolean",
        "Whether the graph uses the ASHRAE 223P namespace.",
        50,
    ),
    (
        "has_g36_namespace",
        "Has Guideline 36 Namespace",
        "boolean",
        "Whether the graph uses the Guideline 36 namespace.",
        60,
    ),
    (
        "has_brick_namespace",
        "Has Brick Namespace",
        "boolean",
        "Whether the graph uses the Brick namespace.",
        70,
    ),
    (
        "shacl_violation_count",
        "SHACL Violation Count",
        "number",
        "Number of SHACL violation results.",
        80,
    ),
    (
        "shacl_warning_count",
        "SHACL Warning Count",
        "number",
        "Number of SHACL warning results.",
        90,
    ),
    (
        "shacl_info_count",
        "SHACL Info Count",
        "number",
        "Number of SHACL info results.",
        100,
    ),
    (
        "shacl_total_count",
        "SHACL Total Result Count",
        "number",
        "Total number of SHACL results at all severities.",
        110,
    ),
]


def seed_shacl_output_signals(apps, schema_editor):
    """Backfill SHACL catalog output signals for existing validator rows."""
    Validator = apps.get_model("validations", "Validator")
    SignalDefinition = apps.get_model("validations", "SignalDefinition")

    for validator in Validator.objects.filter(validation_type="SHACL"):
        for contract_key, label, data_type, description, order in SHACL_OUTPUT_SIGNALS:
            SignalDefinition.objects.update_or_create(
                validator=validator,
                contract_key=contract_key,
                direction="output",
                defaults={
                    "native_name": contract_key,
                    "label": label,
                    "description": description,
                    "data_type": data_type,
                    "order": order,
                    "origin_kind": "catalog",
                    "source_kind": "internal",
                    "is_path_editable": False,
                    "provider_binding": {},
                    "metadata": {},
                },
            )


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0047_alter_validationrun_source_choices"),
    ]

    operations = [
        migrations.RunPython(seed_shacl_output_signals, migrations.RunPython.noop),
    ]
