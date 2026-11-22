"""Standalone HTTP frontend and API; no NiceGUI client or UI lifecycle."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Annotated, Protocol
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from _async_utils import run_blocking

from .network_models import (
    MAX_REPORT_BYTES,
    DEFAULT_BANDWIDTH_SIZE,
    DEFAULT_BANDWIDTH_DURATION,
    NetworkAppList,
    NetworkAppName,
    NetworkBandwidthEndpoint,
    NetworkBandwidthSelection,
    NetworkBandwidthSize,
    NetworkBandwidthDuration,
    NetworkConfiguration,
    NetworkContext,
    NetworkIdentity,
    NetworkNodeId,
    NetworkReport,
    NetworkReportInput,
    NetworkReportList,
)
from .network_reports import NetworkReportStore

FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "frontend" / "dist"


class NetworkBackend(Protocol):
    def network_identity(self, request: Request) -> NetworkIdentity: ...
    def network_configuration(
        self, identity: NetworkIdentity
    ) -> NetworkConfiguration: ...
    async def network_apps(self) -> NetworkAppList: ...
    async def network_bandwidth_session(
        self,
        request: Request,
        target_id: str,
        origin: str,
        size_bytes: NetworkBandwidthSize = DEFAULT_BANDWIDTH_SIZE,
        duration_ms: NetworkBandwidthDuration = DEFAULT_BANDWIDTH_DURATION,
    ) -> NetworkBandwidthEndpoint: ...
    async def network_context(
        self,
        request: Request,
        node_name: str | None = None,
        app_name: str | None = None,
    ) -> NetworkContext: ...

    @property
    def network_reports(self) -> NetworkReportStore: ...


class NetworkBodyLimitMiddleware:
    """Bound report JSON before FastAPI buffers and validates the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["path"] != "/api/network/reports"
            or scope["method"] != "POST"
        ):
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_REPORT_BYTES:
                await HTMLResponse(
                    "Diagnostics report exceeds the 2 MiB limit.", status_code=413
                )(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {
                "type": "http.request",
                "body": b"".join(chunks),
                "more_body": False,
            }

        await self.app(scope, replay, send)


def create_network_router(
    backend: NetworkBackend | None = None, *, frontend_root: Path = FRONTEND_ROOT
) -> APIRouter:
    def no_cache(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"

    router = APIRouter(tags=["network-diagnostics"], dependencies=[Depends(no_cache)])

    def service() -> NetworkBackend:
        if backend is None:
            raise RuntimeError("Network diagnostics backend is not configured.")
        return backend

    def require_same_origin(request: Request) -> str:
        origin = str(request.base_url).rstrip("/")
        supplied = request.headers.get("origin")
        if supplied is not None and supplied != origin:
            raise HTTPException(403, "Diagnostics requests must use the same origin.")
        return origin

    @router.get("/mod-web/network", response_model=None, include_in_schema=False)
    async def page(request: Request) -> HTMLResponse | FileResponse | RedirectResponse:
        try:
            service().network_identity(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse(
                    f"/auth/login?{urlencode({'next_path': '/mod-web/network'})}",
                    status_code=303,
                )
            return HTMLResponse(
                f"<h1>Diagnostics unavailable</h1><p>{escape(str(error.detail))}</p>",
                status_code=error.status_code,
            )
        index = frontend_root / "index.html"
        if not index.is_file():
            return HTMLResponse(
                "<h1>Diagnostics frontend has not been built</h1><p>Run <code>pnpm --dir frontend install</code> "
                "and <code>pnpm --dir frontend build</code> before starting Portal.</p>",
                status_code=503,
            )
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    @router.get("/mod-web/frontend/assets/{asset_path:path}", include_in_schema=False)
    async def asset(asset_path: str) -> FileResponse:
        root = (frontend_root / "assets").resolve()
        target = (root / asset_path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Frontend asset not found.")
        return FileResponse(
            target, headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    @router.get("/api/network/config", operation_id="network_configuration")
    async def configuration(request: Request) -> NetworkConfiguration:
        return service().network_configuration(service().network_identity(request))

    @router.get("/api/network/context", operation_id="network_context")
    async def context(
        request: Request,
        node_name: Annotated[NetworkNodeId | None, Query()] = None,
        app_name: Annotated[NetworkAppName | None, Query()] = None,
    ) -> NetworkContext:
        service().network_identity(request)
        if (node_name is None) != (app_name is None):
            raise HTTPException(422, "Choose both a node and an app.")
        return await service().network_context(request, node_name, app_name)

    @router.get("/api/network/apps", operation_id="network_apps")
    async def apps(request: Request) -> NetworkAppList:
        service().network_identity(request)
        return await service().network_apps()

    @router.get("/api/network/reports", operation_id="network_reports")
    async def reports(request: Request) -> NetworkReportList:
        identity = service().network_identity(request)
        return NetworkReportList(
            reports=await run_blocking(service().network_reports.list, identity)
        )

    @router.post(
        "/api/network/bandwidth/{target_id}", operation_id="network_bandwidth_session"
    )
    async def bandwidth_session(
        request: Request,
        target_id: NetworkNodeId,
        payload: NetworkBandwidthSelection | None = None,
    ) -> NetworkBandwidthEndpoint:
        service().network_identity(request)
        origin = require_same_origin(request)
        selection = NetworkBandwidthSelection() if payload is None else payload
        return await service().network_bandwidth_session(
            request, target_id, origin, selection.size_bytes, selection.duration_ms
        )

    @router.get("/api/network/reports/{report_id}", operation_id="network_report")
    async def report(report_id: UUID, request: Request) -> NetworkReport:
        identity = service().network_identity(request)
        result = await run_blocking(service().network_reports.get, report_id, identity)
        if result is None:
            raise HTTPException(404, "Diagnostics report not found.")
        return result

    @router.post("/api/network/reports", operation_id="save_network_report")
    async def save_report(
        recording: NetworkReportInput, request: Request
    ) -> NetworkReport:
        identity = service().network_identity(request)
        require_same_origin(request)
        try:
            return await run_blocking(
                service().network_reports.save, recording, identity
            )
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error

    return router
