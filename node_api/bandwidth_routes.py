"""Authorize browser bandwidth sessions using the existing node actor grant."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response

from node_auth import NodeApiScope
from web_api.network_bandwidth import (
    NetworkBandwidthService,
    create_bandwidth_transfer_router,
)
from web_api.network_models import (
    NetworkBandwidthGrant,
    NetworkBandwidthSessionRequest,
)

from .route_contracts import NodeAuthenticatedRouteService


def register_bandwidth_routes(
    app: FastAPI,
    *,
    auth: NodeAuthenticatedRouteService,
    service: NetworkBandwidthService,
    api_prefix: str,
) -> None:
    prefix = f"{api_prefix}/bandwidth"
    app.include_router(create_bandwidth_transfer_router(service, prefix=prefix))

    @app.post(f"{prefix}/sessions")
    async def session(
        payload: NetworkBandwidthSessionRequest,
        request: Request,
        response: Response,
        access_token: str | None = None,
    ) -> NetworkBandwidthGrant:
        context = auth.require_access(
            request, access_token, app_name=None, scopes=(NodeApiScope.APPS_READ,)
        )
        actor_id = auth.require_actor(context).require_actor_user_id()
        response.headers["Cache-Control"] = "no-store"
        return service.issue(
            actor_id=str(actor_id),
            origin=payload.origin,
            size_bytes=payload.size_bytes,
            duration_ms=payload.duration_ms,
        )
