import django.db.models.deletion
from django.db import migrations, models

import core.models

OLD = {"pc": "шт", "kg": "кг", "l": "л"}


def to_units(apps, schema_editor):
    Unit = apps.get_model("core", "Unit")
    RawMaterial = apps.get_model("core", "RawMaterial")
    for name in ("шт", "кг", "гр", "л"):
        Unit.objects.get_or_create(name=name)
    for material in RawMaterial.objects.all():
        material.unit_ref = Unit.objects.get(name=OLD.get(material.unit, "шт"))
        material.save(update_fields=["unit_ref"])


def from_units(apps, schema_editor):
    back = {v: k for k, v in OLD.items()}
    RawMaterial = apps.get_model("core", "RawMaterial")
    for material in RawMaterial.objects.select_related("unit_ref"):
        material.unit = back.get(material.unit_ref.name, "pc")
        material.save(update_fields=["unit"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_roles"),
    ]

    operations = [
        migrations.CreateModel(
            name="Unit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Например: шт, кг, гр, л, пачка", max_length=20, unique=True,
                                          verbose_name="Единица")),
            ],
            options={"verbose_name": "Единица измерения", "verbose_name_plural": "Единицы измерения",
                     "ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="rawmaterial",
            name="unit_ref",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to="core.unit"),
        ),
        migrations.RunPython(to_units, from_units),
        migrations.RemoveField(model_name="rawmaterial", name="unit"),
        migrations.RenameField(model_name="rawmaterial", old_name="unit_ref", new_name="unit"),
        migrations.AlterField(
            model_name="rawmaterial",
            name="unit",
            field=models.ForeignKey(default=core.models.default_unit, on_delete=django.db.models.deletion.PROTECT,
                                    related_name="materials", to="core.unit", verbose_name="Единица"),
        ),
        migrations.AddField(
            model_name="shipment",
            name="method",
            field=models.CharField(choices=[("cash", "Наличные"), ("card", "Карта"), ("consign", "Консигнация"),
                                            ("debt", "В долг")], default="cash",
                                   help_text="Консигнация: магазин платит за проданное, непроданное оформляется возвратом",
                                   max_length=10, verbose_name="Оплата"),
        ),
    ]
