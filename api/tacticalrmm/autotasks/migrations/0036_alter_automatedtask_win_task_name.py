# Generated by Django 4.0.3 on 2022-04-15 20:52

import autotasks.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('autotasks', '0035_auto_20220415_1818'),
    ]

    operations = [
        migrations.AlterField(
            model_name='automatedtask',
            name='win_task_name',
            field=models.CharField(blank=True, default=autotasks.models.generate_task_name, max_length=255, unique=True),
        ),
    ]
