from rest_framework import permissions

from tacticalrmm.permissions import _has_perm, _has_perm_on_agent


class WinSvcsPerms(permissions.BasePermission):
    def has_permission(self, r, view) -> bool:
        if "agent_id" in view.kwargs.keys():
            return _has_perm(r, "can_manage_winsvcs") and _has_perm_on_agent(
                r.user, view.kwargs["agent_id"]
            )
        else:
            return _has_perm(r, "can_manage_winsvcs")
