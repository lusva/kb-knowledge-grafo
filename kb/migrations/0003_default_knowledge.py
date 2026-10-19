from django.db import migrations


def create_default(apps, schema_editor):
    apps.get_model("kb", "KnowledgeBase").objects.get_or_create(
        slug="principale", defaults={"name": "Principale", "is_default": True})


class Migration(migrations.Migration):
    dependencies = [("kb", "0002_chunk_fts")]
    operations = [migrations.RunPython(create_default, migrations.RunPython.noop)]
