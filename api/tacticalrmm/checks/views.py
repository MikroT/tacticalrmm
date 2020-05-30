import validators
import datetime as dt

from django.shortcuts import get_object_or_404
from django.utils import timezone as djangotime

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)

from agents.models import Agent
from automation.models import Policy

from .models import Check
from scripts.models import Script

from autotasks.models import AutomatedTask

from .serializers import CheckSerializer
from scripts.serializers import ScriptSerializer

from .tasks import handle_check_email_alert_task, run_checks_task
from autotasks.tasks import delete_win_task_schedule


class GetAddCheck(APIView):
    def get(self, request):
        checks = Check.objects.all()
        return Response(CheckSerializer(checks, many=True).data)

    def post(self, request):
        # Determine if adding check to Policy or Agent
        if "policy" in request.data:
            policy = get_object_or_404(Policy, id=request.data["policy"])
            # Object used for filter and save
            parent = {"policy": policy}
        else:
            agent = get_object_or_404(Agent, pk=request.data["pk"])
            parent = {"agent": agent}

        script = None
        if "script" in request.data["check"]:
            script = get_object_or_404(Script, pk=request.data["check"]["script"])

        serializer = CheckSerializer(
            data=request.data["check"], partial=True, context=parent
        )
        serializer.is_valid(raise_exception=True)
        obj = serializer.save(**parent, script=script)

        return Response(f"{obj.readable_desc} was added!")


class GetUpdateDeleteCheck(APIView):
    def get(self, request, pk):
        check = get_object_or_404(Check, pk=pk)
        return Response(CheckSerializer(check).data)

    def patch(self, request, pk):
        check = get_object_or_404(Check, pk=pk)
        serializer = CheckSerializer(instance=check, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()

        return Response(f"{obj.readable_desc} was edited!")

    def delete(self, request, pk):
        check = get_object_or_404(Check, pk=pk)
        check.delete()
        return Response(f"{check.readable_desc} was deleted!")


@api_view()
def get_scripts(request):
    scripts = Script.objects.all()
    return Response(ScriptSerializer(scripts, many=True, read_only=True).data)


@api_view()
@authentication_classes((TokenAuthentication,))
@permission_classes((IsAuthenticated,))
def check_runner(request):
    agent = get_object_or_404(Agent, agent_id=request.data["agent_id"])
    return Response(CheckSerializer(agent).data)


@api_view(["PATCH"])
@authentication_classes((TokenAuthentication,))
@permission_classes((IsAuthenticated,))
def check_results(request):
    if request.data["check_type"] == "diskspace":
        check = get_object_or_404(DiskCheck, pk=request.data["id"])
        serializer = DiskCheckSerializer(
            instance=check, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(last_run=djangotime.now())
        check.handle_check(request.data)

    elif request.data["check_type"] == "cpuload":
        check = get_object_or_404(CpuLoadCheck, pk=request.data["id"])
        check.handle_check(request.data)

    elif request.data["check_type"] == "memory":
        check = get_object_or_404(MemCheck, pk=request.data["id"])
        check.handle_check(request.data)

    elif request.data["check_type"] == "winsvc":
        check = get_object_or_404(WinServiceCheck, pk=request.data["id"])
        serializer = WinServiceCheckSerializer(
            instance=check, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(last_run=djangotime.now())
        check.handle_check(request.data)

    elif request.data["check_type"] == "script":
        check = get_object_or_404(ScriptCheck, pk=request.data["id"])
        serializer = ScriptCheckSerializer(
            instance=check, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(last_run=djangotime.now())
        check.handle_check(request.data)

    elif request.data["check_type"] == "ping":
        check = get_object_or_404(PingCheck, pk=request.data["id"])
        serializer = PingCheckSerializer(
            instance=check, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(last_run=djangotime.now())
        check.handle_check(request.data)

    elif request.data["check_type"] == "eventlog":
        check = get_object_or_404(EventLogCheck, pk=request.data["id"])
        serializer = EventLogCheckSerializer(
            instance=check, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save(last_run=djangotime.now())
        check.handle_check(request.data)

    else:
        return Response("error", status=status.HTTP_400_BAD_REQUEST)

    return Response("ok")


@api_view()
def run_checks(request, pk):
    agent = get_object_or_404(Agent, pk=pk)
    run_checks_task.delay(agent.pk)
    return Response(agent.hostname)


@api_view()
def load_checks(request, pk):
    checks = Check.objects.filter(agent__pk=pk)
    return Response(CheckSerializer(checks, many=True).data)


@api_view()
def load_policy_checks(request, pk):
    policy = get_object_or_404(Policy, pk=pk)
    return Response(PolicyChecksSerializer(policy).data)


@api_view()
def get_disks(request, pk):
    return Response(get_object_or_404(Agent, pk=pk).disks)


@api_view()
def get_disks_for_policies(request):
    return Response(DiskCheck.all_disks())


@api_view(["PATCH"])
def edit_standard_check(request):
    if request.data["check_type"] == "diskspace":
        check = get_object_or_404(DiskCheck, pk=request.data["pk"])
        if not validate_threshold(request.data["threshold"]):
            error = {"error": "Please enter a valid threshold between 1 and 99"}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        check.threshold = request.data["threshold"]
        check.failures = request.data["failures"]
        check.save(update_fields=["threshold", "failures"])
        return Response("ok")

    elif request.data["check_type"] == "ping":
        check = get_object_or_404(PingCheck, pk=request.data["pk"])
        if not PingCheck.validate_hostname_or_ip(request.data["ip"]):
            error = {"error": "Please enter a valid hostname or IP"}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        check.name = request.data["name"]
        check.ip = request.data["ip"]
        check.failures = request.data["failures"]
        check.save(update_fields=["name", "ip", "failures"])
        return Response("ok")

    elif request.data["check_type"] == "cpuload":
        check = get_object_or_404(CpuLoadCheck, pk=request.data["pk"])
        if not validate_threshold(request.data["threshold"]):
            error = {"error": "Please enter a valid threshold between 1 and 99"}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        check.cpuload = request.data["threshold"]
        check.failures = request.data["failure"]
        check.save(update_fields=["cpuload", "failures"])
        return Response("ok")

    elif request.data["check_type"] == "mem":
        check = get_object_or_404(MemCheck, pk=request.data["pk"])
        if not validate_threshold(request.data["threshold"]):
            error = {"error": "Please enter a valid threshold between 1 and 99"}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        check.threshold = request.data["threshold"]
        check.failures = request.data["failure"]
        check.save(update_fields=["threshold", "failures"])
        return Response("ok")

    elif request.data["check_type"] == "winsvc":
        check = get_object_or_404(WinServiceCheck, pk=request.data["pk"])
        check.pass_if_start_pending = request.data["passifstartpending"]
        check.restart_if_stopped = request.data["restartifstopped"]
        check.failures = request.data["failures"]
        check.save(
            update_fields=["pass_if_start_pending", "restart_if_stopped", "failures"]
        )
        return Response("ok")

    elif request.data["check_type"] == "script":
        check = get_object_or_404(ScriptCheck, pk=request.data["pk"])
        check.failures = request.data["failures"]
        check.timeout = request.data["timeout"]
        check.save(update_fields=["failures", "timeout"])
        return Response(f"{check.script.name} was edited!")

    elif request.data["check_type"] == "eventlog":
        check = get_object_or_404(EventLogCheck, pk=request.data["pk"])
        serializer = EventLogCheckSerializer(
            instance=check, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        return Response("Event log check was edited")


@api_view()
def get_standard_check(request, checktype, pk):
    if checktype == "diskspace":
        check = DiskCheck.objects.get(pk=pk)
        return Response(DiskCheckSerializer(check).data)
    elif checktype == "ping":
        check = PingCheck.objects.get(pk=pk)
        return Response(PingCheckSerializer(check).data)
    elif checktype == "cpuload":
        check = CpuLoadCheck.objects.get(pk=pk)
        return Response(CpuLoadCheckSerializer(check).data)
    elif checktype == "mem":
        check = MemCheck.objects.get(pk=pk)
        return Response(MemCheckSerializer(check).data)
    elif checktype == "winsvc":
        check = WinServiceCheck.objects.get(pk=pk)
        return Response(WinServiceCheckSerializer(check).data)
    elif checktype == "script":
        check = ScriptCheck.objects.get(pk=pk)
        return Response(ScriptCheckSerializer(check).data)
    elif checktype == "eventlog":
        check = EventLogCheck.objects.get(pk=pk)
        return Response(EventLogCheckSerializer(check).data)


@api_view(["DELETE"])
def delete_standard_check(request):
    pk = request.data["pk"]
    if request.data["checktype"] == "diskspace":
        check = DiskCheck.objects.get(pk=pk)
    elif request.data["checktype"] == "ping":
        check = PingCheck.objects.get(pk=pk)
    elif request.data["checktype"] == "cpuload":
        check = CpuLoadCheck.objects.get(pk=pk)
    elif request.data["checktype"] == "memory":
        check = MemCheck.objects.get(pk=pk)
    elif request.data["checktype"] == "winsvc":
        check = WinServiceCheck.objects.get(pk=pk)
    elif request.data["checktype"] == "script":
        check = ScriptCheck.objects.get(pk=pk)
    elif request.data["checktype"] == "eventlog":
        check = EventLogCheck.objects.get(pk=pk)

    if check.task_on_failure:
        delete_win_task_schedule.delay(check.task_on_failure.pk)

    check.delete()
    return Response("ok")


@api_view(["PATCH"])
def check_alert(request):
    alert_type = request.data["alertType"]
    category = request.data["category"]
    checkid = request.data["checkid"]
    action = request.data["action"]
    if category == "diskspace":
        check = DiskCheck.objects.get(pk=checkid)
    elif category == "cpuload":
        check = CpuLoadCheck.objects.get(pk=checkid)
    elif category == "memory":
        check = MemCheck.objects.get(pk=checkid)
    elif category == "ping":
        check = PingCheck.objects.get(pk=checkid)
    elif category == "winsvc":
        check = WinServiceCheck.objects.get(pk=checkid)
    elif category == "script":
        check = ScriptCheck.objects.get(pk=checkid)
    elif category == "eventlog":
        check = EventLogCheck.objects.get(pk=checkid)
    else:
        return Response(
            {"error": "Something went wrong"}, status=status.HTTP_400_BAD_REQUEST
        )

    if alert_type == "email" and action == "enabled":
        check.email_alert = True
        check.save(update_fields=["email_alert"])
    elif alert_type == "email" and action == "disabled":
        check.email_alert = False
        check.save(update_fields=["email_alert"])
    elif alert_type == "text" and action == "enabled":
        check.text_alert = True
        check.save(update_fields=["text_alert"])
    elif alert_type == "text" and action == "disabled":
        check.text_alert = False
        check.save(update_fields=["text_alert"])
    else:
        return Response(
            {"error": "Something terrible went wrong"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response("ok")
