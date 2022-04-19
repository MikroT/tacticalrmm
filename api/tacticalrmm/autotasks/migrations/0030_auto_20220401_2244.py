# Generated by Django 3.2.12 on 2022-04-01 22:44

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("checks", "0025_auto_20210917_1954"),
        ("agents", "0046_alter_agenthistory_command"),
        ("autotasks", "0029_alter_automatedtask_task_type"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="automatedtask",
            name="retvalue",
        ),
        migrations.AlterField(
            model_name="automatedtask",
            name="assigned_check",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assignedtasks",
                to="checks.check",
            ),
        ),
        migrations.AlterField(
            model_name="automatedtask",
            name="win_task_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.CreateModel(
            name="TaskResult",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("retcode", models.IntegerField(blank=True, null=True)),
                ("stdout", models.TextField(blank=True, null=True)),
                ("stderr", models.TextField(blank=True, null=True)),
                ("execution_time", models.CharField(default="0.0000", max_length=100)),
                ("last_run", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("passing", "Passing"),
                            ("failing", "Failing"),
                            ("pending", "Pending"),
                        ],
                        default="pending",
                        max_length=30,
                    ),
                ),
                (
                    "sync_status",
                    models.CharField(
                        choices=[
                            ("synced", "Synced With Agent"),
                            ("notsynced", "Waiting On Agent Checkin"),
                            ("pendingdeletion", "Pending Deletion on Agent"),
                            ("initial", "Initial Task Sync"),
                        ],
                        default="initial",
                        max_length=100,
                    ),
                ),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="taskresults",
                        to="agents.agent",
                    ),
                ),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="taskresults",
                        to="autotasks.automatedtask",
                    ),
                ),
            ],
            options={
                "unique_together": {("agent", "task")},
            },
        ),
    ]
