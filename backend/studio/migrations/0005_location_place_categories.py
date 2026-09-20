from django.db import migrations


def update_categories(apps, schema_editor):
    # The old broad labels do not identify a specific place type.
    apps.get_model("studio", "Asset").objects.filter(
        kind="location", category__in=["interior", "exterior", "urban", "nature"]
    ).update(category="other")


class Migration(migrations.Migration):
    dependencies = [("studio", "0004_asset_analysis_started")]
    operations = [migrations.RunPython(update_categories, migrations.RunPython.noop)]
