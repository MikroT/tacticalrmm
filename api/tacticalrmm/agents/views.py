import asyncio
import datetime as dt
import os
import random
import string
import time

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone as djangotime
from meshctrl.utils import get_login_token
from packaging import version as pyver
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import CodeSignToken
from core.utils import get_core_settings, get_mesh_ws_url, remove_mesh_agent
from logs.models import AuditLog, DebugLog, PendingAction
from scripts.models import Script
from scripts.tasks import handle_bulk_command_task, handle_bulk_script_task
from tacticalrmm.constants import (
    AGENT_DEFER,
    AGENT_STATUS_OFFLINE,
    AGENT_STATUS_ONLINE,
    AgentHistoryType,
    AgentMonType,
    AgentPlat,
    CustomFieldModel,
    EvtLogNames,
    PAAction,
    PAStatus,
)
from tacticalrmm.helpers import notify_error
from tacticalrmm.permissions import (
    _has_perm_on_agent,
    _has_perm_on_client,
    _has_perm_on_site,
)
from tacticalrmm.utils import get_default_timezone, reload_nats
from winupdate.models import WinUpdate
from winupdate.serializers import WinUpdatePolicySerializer
from winupdate.tasks import bulk_check_for_updates_task, bulk_install_updates_task

from .models import Agent, AgentCustomField, AgentHistory, Note
from .permissions import (
    AgentHistoryPerms,
    AgentNotesPerms,
    AgentPerms,
    EvtLogPerms,
    InstallAgentPerms,
    ManageProcPerms,
    MeshPerms,
    PingAgentPerms,
    RebootAgentPerms,
    RecoverAgentPerms,
    RunBulkPerms,
    RunScriptPerms,
    SendCMDPerms,
    UpdateAgentPerms,
)
from .serializers import (
    AgentCustomFieldSerializer,
    AgentHistorySerializer,
    AgentHostnameSerializer,
    AgentNoteSerializer,
    AgentSerializer,
    AgentTableSerializer,
)
from .tasks import run_script_email_results_task, send_agent_update_task


class GetAgents(APIView):
    permission_classes = [IsAuthenticated, AgentPerms]

    def get(self, request):
        from checks.models import Check, CheckResult

        monitoring_type_filter = Q()
        client_site_filter = Q()

        monitoring_type = request.query_params.get("monitoring_type", None)
        if monitoring_type:
            if monitoring_type in AgentMonType.values:
                monitoring_type_filter = Q(monitoring_type=monitoring_type)
            else:
                return notify_error("monitoring type does not exist")

        if "site" in request.query_params.keys():
            client_site_filter = Q(site_id=request.query_params["site"])
        elif "client" in request.query_params.keys():
            client_site_filter = Q(site__client_id=request.query_params["client"])

        # by default detail=true
        if (
            "detail" not in request.query_params.keys()
            or "detail" in request.query_params.keys()
            and request.query_params["detail"] == "true"
        ):
            agents = (
                Agent.objects.filter_by_role(request.user)  # type: ignore
                .filter(monitoring_type_filter)
                .filter(client_site_filter)
                .defer(*AGENT_DEFER)
                .select_related(
                    "site__server_policy",
                    "site__workstation_policy",
                    "site__client__server_policy",
                    "site__client__workstation_policy",
                    "policy",
                    "alert_template",
                )
                .prefetch_related(
                    Prefetch(
                        "agentchecks",
                        queryset=Check.objects.select_related("script"),
                    ),
                    Prefetch(
                        "checkresults",
                        queryset=CheckResult.objects.select_related("assigned_check"),
                    ),
                )
                .annotate(
                    pending_actions_count=Count(
                        "pendingactions",
                        filter=Q(pendingactions__status=PAStatus.PENDING),
                    )
                )
                .annotate(
                    has_patches_pending=Exists(
                        WinUpdate.objects.filter(
                            agent_id=OuterRef("pk"), action="approve", installed=False
                        )
                    )
                )
            )
            serializer = AgentTableSerializer(agents, many=True)

        # if detail=false
        else:
            agents = (
                Agent.objects.filter_by_role(request.user)  # type: ignore
                .defer(*AGENT_DEFER)
                .select_related("site__client")
                .filter(monitoring_type_filter)
                .filter(client_site_filter)
            )
            serializer = AgentHostnameSerializer(agents, many=True)

        return Response(serializer.data)


class GetUpdateDeleteAgent(APIView):
    permission_classes = [IsAuthenticated, AgentPerms]

    # get agent details
    def get(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)
        return Response(AgentSerializer(agent).data)

    # edit agent
    def put(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)

        a_serializer = AgentSerializer(instance=agent, data=request.data, partial=True)
        a_serializer.is_valid(raise_exception=True)
        a_serializer.save()

        if "winupdatepolicy" in request.data.keys():
            policy = agent.winupdatepolicy.get()  # type: ignore
            p_serializer = WinUpdatePolicySerializer(
                instance=policy, data=request.data["winupdatepolicy"][0]
            )
            p_serializer.is_valid(raise_exception=True)
            p_serializer.save()

        if "custom_fields" in request.data.keys():

            for field in request.data["custom_fields"]:

                custom_field = field
                custom_field["agent"] = agent.pk

                if AgentCustomField.objects.filter(
                    field=field["field"], agent=agent.pk
                ):
                    value = AgentCustomField.objects.get(
                        field=field["field"], agent=agent.pk
                    )
                    serializer = AgentCustomFieldSerializer(
                        instance=value, data=custom_field
                    )
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                else:
                    serializer = AgentCustomFieldSerializer(data=custom_field)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

        return Response("The agent was updated successfully")

    # uninstall agent
    def delete(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)

        code = "foo"
        if agent.plat == AgentPlat.LINUX:
            with open(settings.LINUX_AGENT_SCRIPT, "r") as f:
                code = f.read()

        asyncio.run(agent.nats_cmd({"func": "uninstall", "code": code}, wait=False))
        name = agent.hostname
        mesh_id = agent.mesh_node_id
        agent.delete()
        reload_nats()
        uri = get_mesh_ws_url()
        asyncio.run(remove_mesh_agent(uri, mesh_id))
        return Response(f"{name} will now be uninstalled.")


class AgentProcesses(APIView):
    permission_classes = [IsAuthenticated, ManageProcPerms]

    # list agent processes
    def get(self, request, agent_id):
        if getattr(settings, "DEMO", False):
            from tacticalrmm.demo_views import demo_get_procs

            return demo_get_procs()

        agent = get_object_or_404(Agent, agent_id=agent_id)
        r = asyncio.run(agent.nats_cmd(data={"func": "procs"}, timeout=5))
        if r == "timeout" or r == "natsdown":
            return notify_error("Unable to contact the agent")
        return Response(r)

    # kill agent process
    def delete(self, request, agent_id, pid):
        agent = get_object_or_404(Agent, agent_id=agent_id)
        r = asyncio.run(
            agent.nats_cmd({"func": "killproc", "procpid": int(pid)}, timeout=15)
        )

        if r == "timeout" or r == "natsdown":
            return notify_error("Unable to contact the agent")
        elif r != "ok":
            return notify_error(r)

        return Response(f"Process with PID: {pid} was ended successfully")


class AgentMeshCentral(APIView):
    permission_classes = [IsAuthenticated, MeshPerms]

    # get mesh urls
    def get(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)
        core = get_core_settings()

        if not core.mesh_disable_auto_login:
            token = get_login_token(
                key=core.mesh_token, user=f"user//{core.mesh_username}"
            )
            token_param = f"login={token}&"
        else:
            token_param = ""

        control = f"{core.mesh_site}/?{token_param}gotonode={agent.mesh_node_id}&viewmode=11&hide=31"
        terminal = f"{core.mesh_site}/?{token_param}gotonode={agent.mesh_node_id}&viewmode=12&hide=31"
        file = f"{core.mesh_site}/?{token_param}gotonode={agent.mesh_node_id}&viewmode=13&hide=31"

        AuditLog.audit_mesh_session(
            username=request.user.username,
            agent=agent,
            debug_info={"ip": request._client_ip},
        )

        ret = {
            "hostname": agent.hostname,
            "control": control,
            "terminal": terminal,
            "file": file,
            "status": agent.status,
            "client": agent.client.name,
            "site": agent.site.name,
        }
        return Response(ret)

    # start mesh recovery
    def post(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)
        data = {"func": "recover", "payload": {"mode": "mesh"}}
        r = asyncio.run(agent.nats_cmd(data, timeout=90))
        if r != "ok":
            return notify_error("Unable to contact the agent")

        return Response(f"Repaired mesh agent on {agent.hostname}")


@api_view(["GET"])
@permission_classes([IsAuthenticated, AgentPerms])
def get_agent_versions(request):
    agents = (
        Agent.objects.defer(*AGENT_DEFER)
        .filter_by_role(request.user)  # type: ignore
        .select_related("site__client")
    )
    return Response(
        {
            "versions": [settings.LATEST_AGENT_VER],
            "agents": AgentHostnameSerializer(agents, many=True).data,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated, UpdateAgentPerms])
def update_agents(request):
    q = (
        Agent.objects.filter_by_role(request.user)  # type: ignore
        .filter(agent_id__in=request.data["agent_ids"])
        .only("agent_id", "version")
    )
    agent_ids: list[str] = [
        i.agent_id
        for i in q
        if pyver.parse(i.version) < pyver.parse(settings.LATEST_AGENT_VER)
    ]
    send_agent_update_task.delay(agent_ids=agent_ids)
    return Response("ok")


@api_view(["GET"])
@permission_classes([IsAuthenticated, PingAgentPerms])
def ping(request, agent_id):
    agent = get_object_or_404(Agent, agent_id=agent_id)
    status = AGENT_STATUS_OFFLINE
    attempts = 0
    while 1:
        r = asyncio.run(agent.nats_cmd({"func": "ping"}, timeout=2))
        if r == "pong":
            status = AGENT_STATUS_ONLINE
            break
        else:
            attempts += 1
            time.sleep(0.5)

        if attempts >= 3:
            break

    return Response({"name": agent.hostname, "status": status})


@api_view(["GET"])
@permission_classes([IsAuthenticated, EvtLogPerms])
def get_event_log(request, agent_id, logtype, days):
    if getattr(settings, "DEMO", False):
        from tacticalrmm.demo_views import demo_get_eventlog

        return demo_get_eventlog()

    agent = get_object_or_404(Agent, agent_id=agent_id)
    timeout = 180 if logtype == EvtLogNames.SECURITY else 30

    data = {
        "func": "eventlog",
        "timeout": timeout,
        "payload": {
            "logname": logtype,
            "days": str(days),
        },
    }
    r = asyncio.run(agent.nats_cmd(data, timeout=timeout + 2))
    if r == "timeout" or r == "natsdown":
        return notify_error("Unable to contact the agent")

    return Response(r)


@api_view(["POST"])
@permission_classes([IsAuthenticated, SendCMDPerms])
def send_raw_cmd(request, agent_id):
    agent = get_object_or_404(Agent, agent_id=agent_id)
    timeout = int(request.data["timeout"])
    if request.data["shell"] == "custom" and request.data["custom_shell"]:
        shell = request.data["custom_shell"]
    else:
        shell = request.data["shell"]

    data = {
        "func": "rawcmd",
        "timeout": timeout,
        "payload": {
            "command": request.data["cmd"],
            "shell": shell,
        },
    }

    hist = AgentHistory.objects.create(
        agent=agent,
        type=AgentHistoryType.CMD_RUN,
        command=request.data["cmd"],
        username=request.user.username[:50],
    )
    data["id"] = hist.pk

    r = asyncio.run(agent.nats_cmd(data, timeout=timeout + 2))

    if r == "timeout":
        return notify_error("Unable to contact the agent")

    AuditLog.audit_raw_command(
        username=request.user.username,
        agent=agent,
        cmd=request.data["cmd"],
        shell=shell,
        debug_info={"ip": request._client_ip},
    )

    return Response(r)


class Reboot(APIView):
    permission_classes = [IsAuthenticated, RebootAgentPerms]
    # reboot now
    def post(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)
        r = asyncio.run(agent.nats_cmd({"func": "rebootnow"}, timeout=10))
        if r != "ok":
            return notify_error("Unable to contact the agent")

        return Response("ok")

    # reboot later
    def patch(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)
        if agent.is_posix:
            return notify_error(f"Not currently implemented for {agent.plat}")

        try:
            obj = dt.datetime.strptime(request.data["datetime"], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return notify_error("Invalid date")

        task_name = "TacticalRMM_SchedReboot_" + "".join(
            random.choice(string.ascii_letters) for _ in range(10)
        )

        expire_date = obj + djangotime.timedelta(minutes=5)

        nats_data = {
            "func": "schedtask",
            "schedtaskpayload": {
                "type": "schedreboot",
                "enabled": True,
                "delete_expired_task_after": True,
                "start_when_available": False,
                "multiple_instances": 2,
                "trigger": "runonce",
                "name": task_name,
                "start_year": int(dt.datetime.strftime(obj, "%Y")),
                "start_month": int(dt.datetime.strftime(obj, "%-m")),
                "start_day": int(dt.datetime.strftime(obj, "%-d")),
                "start_hour": int(dt.datetime.strftime(obj, "%-H")),
                "start_min": int(dt.datetime.strftime(obj, "%-M")),
                "expire_year": int(expire_date.strftime("%Y")),
                "expire_month": int(expire_date.strftime("%-m")),
                "expire_day": int(expire_date.strftime("%-d")),
                "expire_hour": int(expire_date.strftime("%-H")),
                "expire_min": int(expire_date.strftime("%-M")),
            },
        }

        r = asyncio.run(agent.nats_cmd(nats_data, timeout=10))
        if r != "ok":
            return notify_error(r)

        details = {"taskname": task_name, "time": str(obj)}
        PendingAction.objects.create(
            agent=agent, action_type=PAAction.SCHED_REBOOT, details=details
        )
        nice_time = dt.datetime.strftime(obj, "%B %d, %Y at %I:%M %p")
        return Response(
            {"time": nice_time, "agent": agent.hostname, "task_name": task_name}
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated, InstallAgentPerms])
def install_agent(request):
    from knox.models import AuthToken

    from accounts.models import User
    from agents.utils import get_agent_url

    client_id = request.data["client"]
    site_id = request.data["site"]
    version = settings.LATEST_AGENT_VER
    arch = request.data["arch"]

    if not _has_perm_on_site(request.user, site_id):
        raise PermissionDenied()

    inno = (
        f"winagent-v{version}.exe" if arch == "64" else f"winagent-v{version}-x86.exe"
    )

    # TODO refactor this, install method should not be same as plat
    if request.data["installMethod"] == AgentPlat.LINUX:
        plat = AgentPlat.LINUX
    else:
        plat = AgentPlat.WINDOWS

    download_url = get_agent_url(arch, plat)

    installer_user = User.objects.filter(is_installer_user=True).first()

    _, token = AuthToken.objects.create(
        user=installer_user, expiry=dt.timedelta(hours=request.data["expires"])
    )

    if request.data["installMethod"] == "exe":
        from tacticalrmm.utils import generate_winagent_exe

        return generate_winagent_exe(
            client=client_id,
            site=site_id,
            agent_type=request.data["agenttype"],
            rdp=request.data["rdp"],
            ping=request.data["ping"],
            power=request.data["power"],
            arch=arch,
            token=token,
            api=request.data["api"],
            file_name=request.data["fileName"],
        )

    elif request.data["installMethod"] == AgentPlat.LINUX:
        # TODO
        # linux agents are in beta for now, only available for sponsors for testing
        # remove this after it's out of beta

        code_token = CodeSignToken.objects.first()
        if not code_token:
            return notify_error("Missing code signing token")

        if not code_token.is_valid:
            return notify_error("Code signing token is not valid")

        from agents.utils import generate_linux_install

        return generate_linux_install(
            client=str(client_id),
            site=str(site_id),
            agent_type=request.data["agenttype"],
            arch=arch,
            token=token,
            api=request.data["api"],
            download_url=download_url,
        )

    elif request.data["installMethod"] == "manual":
        cmd = [
            inno,
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "&&",
            "ping",
            "127.0.0.1",
            "-n",
            "5",
            "&&",
            r'"C:\Program Files\TacticalAgent\tacticalrmm.exe"',
            "-m",
            "install",
            "--api",
            request.data["api"],
            "--client-id",
            client_id,
            "--site-id",
            site_id,
            "--agent-type",
            request.data["agenttype"],
            "--auth",
            token,
        ]

        if int(request.data["rdp"]):
            cmd.append("--rdp")
        if int(request.data["ping"]):
            cmd.append("--ping")
        if int(request.data["power"]):
            cmd.append("--power")

        resp = {
            "cmd": " ".join(str(i) for i in cmd),
            "url": download_url,
        }

        return Response(resp)

    elif request.data["installMethod"] == "powershell":

        ps = os.path.join(settings.BASE_DIR, "core/installer.ps1")

        with open(ps, "r") as f:
            text = f.read()

        replace_dict = {
            "innosetupchange": inno,
            "clientchange": str(client_id),
            "sitechange": str(site_id),
            "apichange": request.data["api"],
            "atypechange": request.data["agenttype"],
            "powerchange": str(request.data["power"]),
            "rdpchange": str(request.data["rdp"]),
            "pingchange": str(request.data["ping"]),
            "downloadchange": download_url,
            "tokenchange": token,
        }

        for i, j in replace_dict.items():
            text = text.replace(i, j)

        file_name = "rmm-installer.ps1"
        ps1 = os.path.join(settings.EXE_DIR, file_name)

        if os.path.exists(ps1):
            try:
                os.remove(ps1)
            except Exception as e:
                DebugLog.error(message=str(e))

        with open(ps1, "w") as f:
            f.write(text)

        if settings.DEBUG:
            with open(ps1, "r") as f:
                response = HttpResponse(f.read(), content_type="text/plain")
                response["Content-Disposition"] = f"inline; filename={file_name}"
                return response
        else:
            response = HttpResponse()
            response["Content-Disposition"] = f"attachment; filename={file_name}"
            response["X-Accel-Redirect"] = f"/private/exe/{file_name}"
            return response


@api_view(["POST"])
@permission_classes([IsAuthenticated, RecoverAgentPerms])
def recover(request, agent_id: str) -> Response:
    agent: Agent = get_object_or_404(
        Agent.objects.defer(*AGENT_DEFER), agent_id=agent_id
    )
    mode = request.data["mode"]

    if mode == "tacagent":
        uri = get_mesh_ws_url()
        agent.recover(mode, uri, wait=False)
        return Response("Recovery will be attempted shortly")

    elif mode == "mesh":
        r, err = agent.recover(mode, "")
        if err:
            return notify_error(f"Unable to complete recovery: {r}")

    return Response("Successfully completed recovery")


@api_view(["POST"])
@permission_classes([IsAuthenticated, RunScriptPerms])
def run_script(request, agent_id):
    agent = get_object_or_404(Agent, agent_id=agent_id)
    script = get_object_or_404(Script, pk=request.data["script"])
    output = request.data["output"]
    args = request.data["args"]
    req_timeout = int(request.data["timeout"]) + 3

    AuditLog.audit_script_run(
        username=request.user.username,
        agent=agent,
        script=script.name,
        debug_info={"ip": request._client_ip},
    )

    hist = AgentHistory.objects.create(
        agent=agent,
        type=AgentHistoryType.SCRIPT_RUN,
        script=script,
        username=request.user.username[:50],
    )
    history_pk = hist.pk

    if output == "wait":
        r = agent.run_script(
            scriptpk=script.pk,
            args=args,
            timeout=req_timeout,
            wait=True,
            history_pk=history_pk,
        )
        return Response(r)

    elif output == "email":
        emails = (
            [] if request.data["emailMode"] == "default" else request.data["emails"]
        )
        run_script_email_results_task.delay(
            agentpk=agent.pk,
            scriptpk=script.pk,
            nats_timeout=req_timeout,
            emails=emails,
            args=args,
        )
    elif output == "collector":
        from core.models import CustomField

        r = agent.run_script(
            scriptpk=script.pk,
            args=args,
            timeout=req_timeout,
            wait=True,
            history_pk=history_pk,
        )

        custom_field = CustomField.objects.get(pk=request.data["custom_field"])

        if custom_field.model == CustomFieldModel.AGENT:
            field = custom_field.get_or_create_field_value(agent)
        elif custom_field.model == CustomFieldModel.CLIENT:
            field = custom_field.get_or_create_field_value(agent.client)
        elif custom_field.model == CustomFieldModel.SITE:
            field = custom_field.get_or_create_field_value(agent.site)
        else:
            return notify_error("Custom Field was invalid")

        value = (
            r.strip()
            if request.data["save_all_output"]
            else r.strip().split("\n")[-1].strip()
        )

        field.save_to_field(value)
        return Response(r)
    elif output == "note":
        r = agent.run_script(
            scriptpk=script.pk,
            args=args,
            timeout=req_timeout,
            wait=True,
            history_pk=history_pk,
        )

        Note.objects.create(agent=agent, user=request.user, note=r)
        return Response(r)
    else:
        agent.run_script(
            scriptpk=script.pk, args=args, timeout=req_timeout, history_pk=history_pk
        )

    return Response(f"{script.name} will now be run on {agent.hostname}")


class GetAddNotes(APIView):
    permission_classes = [IsAuthenticated, AgentNotesPerms]

    def get(self, request, agent_id=None):
        if agent_id:
            agent = get_object_or_404(Agent, agent_id=agent_id)
            notes = Note.objects.filter(agent=agent)
        else:
            notes = Note.objects.filter_by_role(request.user)  # type: ignore

        return Response(AgentNoteSerializer(notes, many=True).data)

    def post(self, request):
        agent = get_object_or_404(Agent, agent_id=request.data["agent_id"])
        if not _has_perm_on_agent(request.user, agent.agent_id):
            raise PermissionDenied()

        if "note" not in request.data.keys():
            return notify_error("Cannot add an empty note")

        data = {
            "note": request.data["note"],
            "agent": agent.pk,
            "user": request.user.pk,
        }

        serializer = AgentNoteSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response("Note added!")


class GetEditDeleteNote(APIView):
    permission_classes = [IsAuthenticated, AgentNotesPerms]

    def get(self, request, pk):
        note = get_object_or_404(Note, pk=pk)

        if not _has_perm_on_agent(request.user, note.agent.agent_id):
            raise PermissionDenied()

        return Response(AgentNoteSerializer(note).data)

    def put(self, request, pk):
        note = get_object_or_404(Note, pk=pk)

        if not _has_perm_on_agent(request.user, note.agent.agent_id):
            raise PermissionDenied()

        serializer = AgentNoteSerializer(instance=note, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response("Note edited!")

    def delete(self, request, pk):
        note = get_object_or_404(Note, pk=pk)

        if not _has_perm_on_agent(request.user, note.agent.agent_id):
            raise PermissionDenied()

        note.delete()
        return Response("Note was deleted!")


@api_view(["POST"])
@permission_classes([IsAuthenticated, RunBulkPerms])
def bulk(request):
    if request.data["target"] == "agents" and not request.data["agents"]:
        return notify_error("Must select at least 1 agent")

    if request.data["target"] == "client":
        if not _has_perm_on_client(request.user, request.data["client"]):
            raise PermissionDenied()
        q = Agent.objects.filter_by_role(request.user).filter(  # type: ignore
            site__client_id=request.data["client"]
        )

    elif request.data["target"] == "site":
        if not _has_perm_on_site(request.user, request.data["site"]):
            raise PermissionDenied()
        q = Agent.objects.filter_by_role(request.user).filter(  # type: ignore
            site_id=request.data["site"]
        )

    elif request.data["target"] == "agents":
        q = Agent.objects.filter_by_role(request.user).filter(  # type: ignore
            agent_id__in=request.data["agents"]
        )

    elif request.data["target"] == "all":
        q = Agent.objects.filter_by_role(request.user).only("pk", "monitoring_type")  # type: ignore

    else:
        return notify_error("Something went wrong")

    if request.data["monType"] == "servers":
        q = q.filter(monitoring_type=AgentMonType.SERVER)
    elif request.data["monType"] == "workstations":
        q = q.filter(monitoring_type=AgentMonType.WORKSTATION)

    if request.data["osType"] == AgentPlat.WINDOWS:
        q = q.filter(plat=AgentPlat.WINDOWS)
    elif request.data["osType"] == AgentPlat.LINUX:
        q = q.filter(plat=AgentPlat.LINUX)

    agents: list[int] = [agent.pk for agent in q]

    if not agents:
        return notify_error("No agents where found meeting the selected criteria")

    AuditLog.audit_bulk_action(
        request.user,
        request.data["mode"],
        request.data,
        debug_info={"ip": request._client_ip},
    )

    if request.data["mode"] == "command":
        if request.data["shell"] == "custom" and request.data["custom_shell"]:
            shell = request.data["custom_shell"]
        else:
            shell = request.data["shell"]

        handle_bulk_command_task.delay(
            agents,
            request.data["cmd"],
            shell,
            request.data["timeout"],
            request.user.username[:50],
            run_on_offline=request.data["offlineAgents"],
        )
        return Response(f"Command will now be run on {len(agents)} agents")

    elif request.data["mode"] == "script":
        script = get_object_or_404(Script, pk=request.data["script"])
        handle_bulk_script_task.delay(
            script.pk,
            agents,
            request.data["args"],
            request.data["timeout"],
            request.user.username[:50],
        )
        return Response(f"{script.name} will now be run on {len(agents)} agents")

    elif request.data["mode"] == "patch":

        if request.data["patchMode"] == "install":
            bulk_install_updates_task.delay(agents)
            return Response(
                f"Pending updates will now be installed on {len(agents)} agents"
            )
        elif request.data["patchMode"] == "scan":
            bulk_check_for_updates_task.delay(agents)
            return Response(f"Patch status scan will now run on {len(agents)} agents")

    return notify_error("Something went wrong")


@api_view(["POST"])
@permission_classes([IsAuthenticated, AgentPerms])
def agent_maintenance(request):

    if request.data["type"] == "Client":
        if not _has_perm_on_client(request.user, request.data["id"]):
            raise PermissionDenied()

        count = (
            Agent.objects.filter_by_role(request.user)  # type: ignore
            .filter(site__client_id=request.data["id"])
            .update(maintenance_mode=request.data["action"])
        )

    elif request.data["type"] == "Site":
        if not _has_perm_on_site(request.user, request.data["id"]):
            raise PermissionDenied()

        count = (
            Agent.objects.filter_by_role(request.user)  # type: ignore
            .filter(site_id=request.data["id"])
            .update(maintenance_mode=request.data["action"])
        )

    else:
        return notify_error("Invalid data")

    if count:
        action = "disabled" if not request.data["action"] else "enabled"
        return Response(f"Maintenance mode has been {action} on {count} agents")
    else:
        return Response(
            f"No agents have been put in maintenance mode. You might not have permissions to the resources."
        )


class WMI(APIView):
    permission_classes = [IsAuthenticated, AgentPerms]

    def post(self, request, agent_id):
        agent = get_object_or_404(Agent, agent_id=agent_id)
        r = asyncio.run(agent.nats_cmd({"func": "sysinfo"}, timeout=20))
        if r != "ok":
            return notify_error("Unable to contact the agent")
        return Response("Agent WMI data refreshed successfully")


class AgentHistoryView(APIView):
    permission_classes = [IsAuthenticated, AgentHistoryPerms]

    def get(self, request, agent_id=None):
        if agent_id:
            agent = get_object_or_404(Agent, agent_id=agent_id)
            history = AgentHistory.objects.filter(agent=agent)
        else:
            history = AgentHistory.objects.filter_by_role(request.user)  # type: ignore
        ctx = {"default_tz": get_default_timezone()}
        return Response(AgentHistorySerializer(history, many=True, context=ctx).data)
