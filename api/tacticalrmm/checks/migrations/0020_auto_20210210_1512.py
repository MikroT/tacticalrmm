# Generated by Django 3.1.4 on 2021-02-10 15:12

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('checks', '0019_auto_20210205_1728'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='check',
            name='email_sent',
        ),
        migrations.RemoveField(
            model_name='check',
            name='resolved_email_sent',
        ),
        migrations.RemoveField(
            model_name='check',
            name='resolved_text_sent',
        ),
        migrations.RemoveField(
            model_name='check',
            name='text_sent',
        ),
    ]
