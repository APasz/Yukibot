"""HTTP registration for node-level system routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from .node_service import NodeManagementService
from .system import NodeSystemAction
from .system_service import NodeSystemMonitoringService
from .route_contracts import HttpExceptionFactory, NodeAuthenticatedRouteService
from node_auth import NodeApiScope
from restart_targets import RestartTarget

def register_system_routes(
    nicegui_app: Any,
    *,
    auth: NodeAuthenticatedRouteService,
    monitoring: NodeSystemMonitoringService,
    management: NodeManagementService,
    api_prefix: str,
    max_log_lines: int,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register the core node-system endpoints against a service implementation."""

    @nicegui_app.get(f"{api_prefix}/system")
    async def _system_summary(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API system summary request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APPS_READ,))
        return monitoring.build_summary().to_mapping()

    @nicegui_app.get(f"{api_prefix}/system/history")
    async def _system_history(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API system history request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APPS_READ,))
        return monitoring.build_history().to_mapping()

    @nicegui_app.get(f"{api_prefix}/system/logs")
    async def _system_logs(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API system log catalog request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
        return monitoring.build_log_catalog().to_mapping()

    @nicegui_app.get(f"{api_prefix}/system/logs/{{log_path:path}}")
    async def _system_log_tail(
        log_path: str,
        request: Request,
        access_token: str | None = None,
        max_lines: int = 200,
    ) -> dict[str, object]:
        traffic_log.info("Node API system log tail request: node=%s log=%s", auth.node_name, log_path)
        if max_lines < 1 or max_lines > max_log_lines:
            raise http_exception(400, f"System log line limit must be between 1 and {max_log_lines}.")
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
        return monitoring.build_log_tail(log_path=log_path, max_lines=max_lines).to_mapping()

    @nicegui_app.get(f"{api_prefix}/system/capabilities")
    async def _system_capabilities(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API system capabilities request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
        return management.system_capabilities().to_mapping()

    @nicegui_app.get(f"{api_prefix}/system/restart-state")
    async def _restart_state(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API restart state request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
        return management.read_restart_state().to_mapping()

    @nicegui_app.get(f"{api_prefix}/system/restart-schedules")
    async def _restart_schedules(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API restart schedule request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.NODE_OPERATE,))
        return management.read_restart_schedules().to_mapping()

    @nicegui_app.post(f"{api_prefix}/system/restart-schedules")
    async def _update_restart_schedule(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API restart schedule update request: node=%s", auth.node_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=None,
            scopes=(NodeApiScope.NODE_OPERATE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        target = _restart_target(payload.get("target"), http_exception=http_exception)
        interval_minutes = _optional_int(
            payload.get("interval_minutes"),
            field_name="Restart schedule interval",
            http_exception=http_exception,
        )
        anchor_timestamp = _optional_int(
            payload.get("anchor_timestamp"),
            field_name="Restart schedule anchor timestamp",
            http_exception=http_exception,
        )
        result = await management.update_restart_schedule(
            target=target,
            interval_minutes=interval_minutes,
            anchor_timestamp=anchor_timestamp,
            actor_user_id=actor_user_id,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/system/restart-schedules/{{target_name}}/skip")
    async def _skip_restart_schedule(
        target_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API restart schedule skip request: node=%s target=%s",
            auth.node_name,
            target_name,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=None,
            scopes=(NodeApiScope.NODE_OPERATE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        target = _restart_target(target_name, http_exception=http_exception)
        return (await management.skip_restart_schedule(target=target, actor_user_id=actor_user_id)).to_mapping()

    @nicegui_app.post(f"{api_prefix}/system/actions")
    async def _system_action(
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API system action request: node=%s", auth.node_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=None,
            scopes=(NodeApiScope.NODE_OPERATE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        raw_action = payload.get("action")
        if not isinstance(raw_action, str):
            raise http_exception(400, "Node system action is invalid.")
        try:
            action = NodeSystemAction(raw_action)
        except ValueError as xcp:
            raise http_exception(400, "Unknown node system action.") from xcp
        auto_restart_running_apps = payload.get("auto_restart_running_apps", True)
        if not isinstance(auto_restart_running_apps, bool):
            raise http_exception(400, "Node system action auto-restart option must be boolean.")
        silent = payload.get("silent", False)
        if not isinstance(silent, bool):
            raise http_exception(400, "Node system action silent option must be boolean.")
        result = await management.schedule_system_action(
            action=action,
            auto_restart_running_apps=auto_restart_running_apps,
            silent=silent,
            actor_user_id=actor_user_id,
        )
        return result.to_mapping()


def _restart_target(value: object, *, http_exception: HttpExceptionFactory) -> RestartTarget:
    if not isinstance(value, str):
        raise http_exception(400, "Restart schedule target is invalid.")
    try:
        return RestartTarget(value)
    except ValueError as xcp:
        raise http_exception(400, "Unknown restart schedule target.") from xcp


def _optional_int(
    value: object,
    *,
    field_name: str,
    http_exception: HttpExceptionFactory,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise http_exception(400, f"{field_name} is invalid.")
    return value
