# Generated by Django 3.1.1 on 2020-10-02 12:57

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_auto_20200922_1344"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="user",
            name="created_by",
        ),
        migrations.RemoveField(
            model_name="user",
            name="created_time",
        ),
        migrations.RemoveField(
            model_name="user",
            name="modified_by",
        ),
        migrations.RemoveField(
            model_name="user",
            name="modified_time",
        ),
    ]
