"""HTTP registration for app map and annotation routes."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response

from _async_utils import run_blocking
from apps._app import App
from map_annotations import MapAnnotationDraft, MapAnnotationMutationResult, MapManifest
from mod_web_auth import ModWebAuthService, ModWebUser
from node_api_route_contracts import MappingResponse, NodeAuthenticatedRouteService
from node_auth import NodeApiScope

_MAP_SOURCE_HEADER_NAME = "X-Yukibot-Map-Source"
_MAP_CACHE_UPDATED_AT_HEADER_NAME = "X-Yukibot-Map-Cache-Updated-At"


class _MapProxyResponse(Protocol):
    @property
    def content(self) -> bytes: ...

    @property
    def media_type(self) -> str | None: ...

    @property
    def headers(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def is_stale(self) -> bool: ...

    @property
    def cache_updated_at_unix_ms(self) -> int | None: ...


def _response_headers(proxy_response: _MapProxyResponse) -> dict[str, str]:
    headers = dict(proxy_response.headers)
    headers[_MAP_SOURCE_HEADER_NAME] = "stale" if proxy_response.is_stale else "live"
    if proxy_response.cache_updated_at_unix_ms is not None:
        headers[_MAP_CACHE_UPDATED_AT_HEADER_NAME] = str(proxy_response.cache_updated_at_unix_ms)
    return headers


class NodeMapRouteService(NodeAuthenticatedRouteService, Protocol):
    """Map operations exposed through the HTTP API."""

    _web_auth: ModWebAuthService | None

    def _resolve_app(self, app_name: str) -> App: ...

    def _build_map_manifest_result(self, app: App) -> tuple[MapManifest, _MapProxyResponse]: ...

    def build_map_annotation_list(self, app: App) -> MappingResponse: ...

    def create_map_annotation(
        self,
        app: App,
        draft: MapAnnotationDraft,
        created_by_user_id: int | None,
        created_by_name: str | None,
    ) -> MapAnnotationMutationResult: ...

    def delete_map_annotation(self, app: App, annotation_id: str) -> MapAnnotationMutationResult: ...

    def _map_annotation_creator_name(
        self,
        app: App,
        *,
        actor_user_id: int,
        user: ModWebUser | None,
    ) -> str | None: ...

    def _squaremap_proxy_response(
        self,
        app: App,
        relative_path: str,
        raw_query: str = "",
        *,
        allow_stale_on_error: bool = False,
    ) -> _MapProxyResponse: ...

def register_map_routes(
    nicegui_app: Any,
    *,
    service: NodeMapRouteService,
    api_prefix: str,
    traffic_log: logging.Logger,
) -> None:
    """Register app map, map-proxy, and map-annotation endpoints."""

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/manifest")
    async def _map_manifest(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info("Node API map manifest request: node=%s app=%s", service.node_name, app_name)
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = service._resolve_app(app_name)
        manifest, source = await run_blocking(service._build_map_manifest_result, app)
        return Response(
            content=json.dumps(manifest.to_mapping()),
            media_type="application/json",
            headers=_response_headers(source),
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/annotations")
    async def _map_annotations(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API map annotation list request: node=%s app=%s", service.node_name, app_name)
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = service._resolve_app(app_name)
        annotations = await run_blocking(service.build_map_annotation_list, app)
        return annotations.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/map/annotations")
    async def _create_map_annotation(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API map annotation create request: node=%s app=%s", service.node_name, app_name)
        grant = service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_WRITE,))
        actor_user_id = service._request_actor_user_id(
            request=request,
            access_token=access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MAP_WRITE,),
            verified_grant=grant,
        )
        app = service._resolve_app(app_name)
        draft = MapAnnotationDraft.from_mapping(payload)
        user = None if service._web_auth is None else service._web_auth.current_user(request)
        created_by_name = service._map_annotation_creator_name(app, actor_user_id=actor_user_id, user=user)
        result = await run_blocking(
            service.create_map_annotation,
            app,
            draft,
            actor_user_id,
            created_by_name,
        )
        return result.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/map/annotations/{{annotation_id}}/delete")
    async def _delete_map_annotation(
        app_name: str,
        annotation_id: str,
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info(
            "Node API map annotation delete request: node=%s app=%s annotation_id=%s",
            service.node_name,
            app_name,
            annotation_id,
        )
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_WRITE,))
        app = service._resolve_app(app_name)
        result = await run_blocking(service.delete_map_annotation, app, annotation_id)
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/players")
    async def _map_players(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info("Node API map players request: node=%s app=%s", service.node_name, app_name)
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = service._resolve_app(app_name)
        proxy_response = await run_blocking(
            service._squaremap_proxy_response,
            app,
            "tiles/players.json",
            request.url.query,
        )
        return Response(
            content=proxy_response.content,
            media_type=proxy_response.media_type,
            headers=_response_headers(proxy_response),
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/worlds/{{world_name}}/settings")
    async def _map_world_settings(
        app_name: str,
        world_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info(
            "Node API map world settings request: node=%s app=%s world=%s",
            service.node_name,
            app_name,
            world_name,
        )
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = service._resolve_app(app_name)
        proxy_response = await run_blocking(
            service._squaremap_proxy_response,
            app,
            f"tiles/{quote(world_name, safe='')}/settings.json",
            request.url.query,
            allow_stale_on_error=True,
        )
        return Response(
            content=proxy_response.content,
            media_type=proxy_response.media_type,
            headers=_response_headers(proxy_response),
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/worlds/{{world_name}}/markers")
    async def _map_world_markers(
        app_name: str,
        world_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info(
            "Node API map world markers request: node=%s app=%s world=%s",
            service.node_name,
            app_name,
            world_name,
        )
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = service._resolve_app(app_name)
        proxy_response = await run_blocking(
            service._squaremap_proxy_response,
            app,
            f"tiles/{quote(world_name, safe='')}/markers.json",
            request.url.query,
            allow_stale_on_error=True,
        )
        return Response(
            content=proxy_response.content,
            media_type=proxy_response.media_type,
            headers=_response_headers(proxy_response),
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/worlds/{{world_name}}/tiles/{{z}}/{{tile_name}}")
    async def _map_world_tile(
        app_name: str,
        world_name: str,
        z: int,
        tile_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info(
            "Node API map tile request: node=%s app=%s world=%s z=%s tile=%s",
            service.node_name,
            app_name,
            world_name,
            z,
            tile_name,
        )
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = service._resolve_app(app_name)
        proxy_response = await run_blocking(
            service._squaremap_proxy_response,
            app,
            f"tiles/{quote(world_name, safe='')}/{z}/{quote(tile_name, safe='')}",
            request.url.query,
        )
        return Response(
            content=proxy_response.content,
            media_type=proxy_response.media_type,
            headers=_response_headers(proxy_response),
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/assets/{{asset_path:path}}")
    async def _map_asset(
        app_name: str,
        asset_path: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info(
            "Node API map asset request: node=%s app=%s asset=%s",
            service.node_name,
            app_name,
            asset_path,
        )
        service._require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = service._resolve_app(app_name)
        proxy_response = await run_blocking(
            service._squaremap_proxy_response,
            app,
            f"images/{quote(asset_path, safe='/')}",
            request.url.query,
        )
        return Response(
            content=proxy_response.content,
            media_type=proxy_response.media_type,
            headers=_response_headers(proxy_response),
        )
