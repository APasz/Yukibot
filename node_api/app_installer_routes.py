"""HTTP routes for node-local app installation jobs."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import Request

from _audit import audit_log
from _security import Power_Level
from .app_installer import NodeAppInstallCatalog, NodeAppInstallRequest, NodeAppInstallStatus
from .route_contracts import HttpExceptionFactory, NodeAuthenticatedRouteService
from node_auth import NodeApiScope


class NodeAppInstallerRouteService(Protocol):
    """Installation operations supplied by the node API service."""

    async def build_app_install_catalog(self) -> NodeAppInstallCatalog: ...

    async def start_app_install(
        self,
        *,
        request: NodeAppInstallRequest,
        actor_user_id: int,
    ) -> NodeAppInstallStatus: ...

    def app_install_status(self, *, job_id: str) -> NodeAppInstallStatus: ...


def register_app_installer_routes(
    nicegui_app: Any,
    *,
    service: NodeAppInstallerRouteService,
    auth: NodeAuthenticatedRouteService,
    api_prefix: str,
    http_exception: HttpExceptionFactory,
    traffic_log: logging.Logger,
) -> None:
    """Register the Sudo-only app installation catalogue and job endpoints."""

    @nicegui_app.get(f"{api_prefix}/app-installer")
    async def _catalog(request: Request, access_token: str | None = None) -> dict[str, object]:
        traffic_log.info("Node API app installer catalog request: node=%s", auth.node_name)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APP_MANAGE,))
        return (await service.build_app_install_catalog()).to_mapping()

    @nicegui_app.post(f"{api_prefix}/app-installer/jobs")
    async def _start_job(
        payload: NodeAppInstallRequest,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API app installer start request: node=%s scope=%s",
            auth.node_name,
            payload.scope,
        )
        context = auth.require_access(
            request,
            access_token,
            app_name=None,
            scopes=(NodeApiScope.APP_MANAGE,),
        )
        actor_user_id = auth.require_actor(context).require_actor_user_id()
        try:
            status = await service.start_app_install(request=payload, actor_user_id=actor_user_id)
        except ValueError as xcp:
            raise http_exception(400, str(xcp)) from xcp
        except RuntimeError as xcp:
            raise http_exception(409, str(xcp)) from xcp
        audit_log(
            "app.install_started",
            actor_user_id=actor_user_id,
            node_name=auth.node_name,
            app_scope=payload.scope,
            instance_key=payload.instance_key,
            steam_branch_id=payload.steam_branch_id,
            job_id=status.job_id,
            required_level=Power_Level.sudo.name,
        )
        return status.to_mapping()

    @nicegui_app.get(f"{api_prefix}/app-installer/jobs/{{job_id}}")
    async def _job_status(
        job_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API app installer status request: node=%s job=%s", auth.node_name, job_id)
        auth.require_access(request, access_token, app_name=None, scopes=(NodeApiScope.APP_MANAGE,))
        try:
            return service.app_install_status(job_id=job_id).to_mapping()
        except LookupError as xcp:
            raise http_exception(404, "Install job was not found.") from xcp


__all__: tuple[str, ...] = ("register_app_installer_routes",)
