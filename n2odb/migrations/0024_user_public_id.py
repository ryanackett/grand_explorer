import uuid

from django.db import migrations, models


def fill_public_ids(apps, schema_editor):
    """Give every existing user a distinct UUID before the unique constraint lands."""
    User = apps.get_model("n2odb", "User")
    for user in User.objects.filter(public_id__isnull=True):
        user.public_id = uuid.uuid4()
        user.save(update_fields=["public_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("n2odb", "0023_uploadsubmission_manuscript_source_sha256_and_more"),
    ]

    operations = [
        # Add nullable first so existing rows don't collide on a shared default…
        migrations.AddField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, null=True),
        ),
        # …backfill distinct values…
        migrations.RunPython(fill_public_ids, migrations.RunPython.noop),
        # …then enforce uniqueness (non-null).
        migrations.AlterField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
