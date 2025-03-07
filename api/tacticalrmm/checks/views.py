import asyncio
from datetime import datetime as dt

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone as djangotime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from agents.models import Agent
from alerts.models import Alert
from automation.models import Policy
from tacticalrmm.constants import CheckStatus, CheckType
from tacticalrmm.helpers import notify_error
from tacticalrmm.permissions import _has_perm_on_agent

from .models import Check, CheckHistory, CheckResult
from .permissions import ChecksPerms, RunChecksPerms
from .serializers import CheckHistorySerializer, CheckSerializer


class GetAddChecks(APIView):
    permission_classes = [IsAuthenticated, ChecksPerms]

    def get(self, request, agent_id=None, policy=None):
        if agent_id:
            agent = get_object_or_404(Agent, agent_id=agent_id)
            checks = agent.get_checks_with_policies()
        elif policy:
            policy = get_object_or_404(Policy, id=policy)
            checks = Check.objects.filter(policy=policy)
        else:
            checks = Check.objects.filter_by_role(request.user)  # type: ignore
        return Response(CheckSerializer(checks, many=True).data)

    def post(self, request):

        data = request.data.copy()
        # Determine if adding check to Agent and replace agent_id with pk
        if "agent" in data.keys():
            agent = get_object_or_404(Agent, agent_id=data["agent"])
            if not _has_perm_on_agent(request.user, agent.agent_id):
                raise PermissionDenied()

            data["agent"] = agent.pk

        # set event id to 0 if wildcard because it needs to be an integer field for db
        # will be ignored anyway by the agent when doing wildcard check
        if data["check_type"] == CheckType.EVENT_LOG and data["event_id_is_wildcard"]:
            data["event_id"] = 0

        serializer = CheckSerializer(data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        new_check = serializer.save()

        return Response(f"{new_check.readable_desc} was added!")


class GetUpdateDeleteCheck(APIView):
    permission_classes = [IsAuthenticated, ChecksPerms]

    def get(self, request, pk):
        check = get_object_or_404(Check, pk=pk)
        if check.agent and not _has_perm_on_agent(request.user, check.agent.agent_id):
            raise PermissionDenied()

        return Response(CheckSerializer(check).data)

    def put(self, request, pk):
        check = get_object_or_404(Check, pk=pk)

        data = request.data.copy()

        if check.agent and not _has_perm_on_agent(request.user, check.agent.agent_id):
            raise PermissionDenied()

        # remove fields that should not be changed when editing a check from the frontend
        [data.pop(i) for i in Check.non_editable_fields() if i in data.keys()]

        # set event id to 0 if wildcard because it needs to be an integer field for db
        # will be ignored anyway by the agent when doing wildcard check
        if check.check_type == CheckType.EVENT_LOG:
            try:
                data["event_id_is_wildcard"]
            except KeyError:
                pass
            else:
                if data["event_id_is_wildcard"]:
                    data["event_id"] = 0

        serializer = CheckSerializer(instance=check, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        check = serializer.save()

        return Response(f"{check.readable_desc} was edited!")

    def delete(self, request, pk):
        check = get_object_or_404(Check, pk=pk)

        if check.agent and not _has_perm_on_agent(request.user, check.agent.agent_id):
            raise PermissionDenied()

        check.delete()

        return Response(f"{check.readable_desc} was deleted!")


class ResetCheck(APIView):
    permission_classes = [IsAuthenticated, ChecksPerms]

    def post(self, request, pk):
        result = get_object_or_404(CheckResult, pk=pk)

        if result.agent and not _has_perm_on_agent(request.user, result.agent.agent_id):
            raise PermissionDenied()

        result.status = CheckStatus.PASSING
        result.save()

        # resolve any alerts that are open
        alert = Alert.create_or_return_check_alert(
            result.assigned_check, agent=result.agent, skip_create=True
        )
        if alert:
            alert.resolve()

        return Response("The check status was reset")


class GetCheckHistory(APIView):
    permission_classes = [IsAuthenticated, ChecksPerms]

    def patch(self, request, pk):
        result = get_object_or_404(CheckResult, pk=pk)

        if result.agent and not _has_perm_on_agent(request.user, result.agent.agent_id):
            raise PermissionDenied()

        timeFilter = Q()

        if "timeFilter" in request.data:
            if request.data["timeFilter"] != 0:
                timeFilter = Q(
                    x__lte=djangotime.make_aware(dt.today()),
                    x__gt=djangotime.make_aware(dt.today())
                    - djangotime.timedelta(days=request.data["timeFilter"]),
                )

        check_history = (
            CheckHistory.objects.filter(
                check_id=result.assigned_check.id, agent_id=result.agent.agent_id
            )
            .filter(timeFilter)
            .order_by("-x")
        )

        return Response(CheckHistorySerializer(check_history, many=True).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, RunChecksPerms])
def run_checks(request, agent_id):
    agent = get_object_or_404(Agent, agent_id=agent_id)

    r = asyncio.run(agent.nats_cmd({"func": "runchecks"}, timeout=15))
    if r == "busy":
        return notify_error(f"Checks are already running on {agent.hostname}")
    elif r == "ok":
        return Response(f"Checks will now be re-run on {agent.hostname}")
    else:
        return notify_error("Unable to contact the agent")
