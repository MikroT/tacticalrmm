# Generated by Django 3.1.2 on 2020-11-01 23:12

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0022_update_site_primary_key"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="agent",
            name="client",
        ),
        migrations.RemoveField(
            model_name="agent",
            name="site",
        ),
    ]
