"""HTTP registration for app map and annotation routes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response

from _async_utils import run_blocking
from apps._app import App
from map_annotations import MapAnnotationDraft
from mod_web_auth import ModWebUser
from .map_service import NodeMapService, NodeMapProxyResponse
from .route_contracts import NodeAuthenticatedRouteService
from node_auth import NodeApiScope

_MAP_SOURCE_HEADER_NAME = "X-Yukibot-Map-Source"
_MAP_CACHE_UPDATED_AT_HEADER_NAME = "X-Yukibot-Map-Cache-Updated-At"


def _response_headers(proxy_response: NodeMapProxyResponse) -> dict[str, str]:
    headers = dict(proxy_response.headers)
    headers[_MAP_SOURCE_HEADER_NAME] = "stale" if proxy_response.is_stale else "live"
    if proxy_response.cache_updated_at_unix_ms is not None:
        headers[_MAP_CACHE_UPDATED_AT_HEADER_NAME] = str(proxy_response.cache_updated_at_unix_ms)
    return headers

def register_map_routes(
    nicegui_app: Any,
    *,
    auth: NodeAuthenticatedRouteService,
    resolve_app: Callable[[str], App],
    map_annotation_creator_name: Callable[[App, int, ModWebUser | None], str | None],
    maps: NodeMapService,
    api_prefix: str,
    traffic_log: logging.Logger,
) -> None:
    """Register app map, map-proxy, and map-annotation endpoints."""

    async def _proxy_response(
        app: App,
        relative_path: str,
        raw_query: str,
        *,
        allow_stale_on_error: bool = False,
    ) -> NodeMapProxyResponse:
        return await run_blocking(
            lambda: maps.proxy_response(
                app=app,
                relative_path=relative_path,
                raw_query=raw_query,
                allow_stale_on_error=allow_stale_on_error,
            )
        )

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/manifest")
    async def _map_manifest(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info("Node API map manifest request: node=%s app=%s", auth.node_name, app_name)
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = resolve_app(app_name)
        manifest, source = await run_blocking(maps.build_manifest_result, app)
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
        traffic_log.info("Node API map annotation list request: node=%s app=%s", auth.node_name, app_name)
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = resolve_app(app_name)
        annotations = await run_blocking(maps.build_annotation_list, app)
        return annotations.to_mapping()

    @nicegui_app.post(f"{api_prefix}/apps/{{app_name}}/map/annotations")
    async def _create_map_annotation(
        app_name: str,
        payload: dict[str, object],
        request: Request,
        access_token: str | None = None,
    ) -> dict[str, object]:
        traffic_log.info("Node API map annotation create request: node=%s app=%s", auth.node_name, app_name)
        context = auth.require_access(
            request,
            access_token,
            app_name=app_name,
            scopes=(NodeApiScope.MAP_WRITE,),
        )
        context = auth.require_actor(context)
        actor_user_id = context.require_actor_user_id()
        app = resolve_app(app_name)
        draft = MapAnnotationDraft.from_mapping(payload)
        context = auth.with_current_web_user(context, request)
        created_by_name = map_annotation_creator_name(
            app,
            actor_user_id,
            context.web_user,
        )
        result = await run_blocking(
            lambda: maps.create_annotation(
                app=app,
                draft=draft,
                created_by_user_id=actor_user_id,
                created_by_name=created_by_name,
            )
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
            auth.node_name,
            app_name,
            annotation_id,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_WRITE,))
        app = resolve_app(app_name)
        result = await run_blocking(
            lambda: maps.delete_annotation(app=app, annotation_id=annotation_id)
        )
        return result.to_mapping()

    @nicegui_app.get(f"{api_prefix}/apps/{{app_name}}/map/players")
    async def _map_players(
        app_name: str,
        request: Request,
        access_token: str | None = None,
    ) -> Response:
        traffic_log.info("Node API map players request: node=%s app=%s", auth.node_name, app_name)
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = resolve_app(app_name)
        proxy_response = await _proxy_response(
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
            auth.node_name,
            app_name,
            world_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = resolve_app(app_name)
        proxy_response = await _proxy_response(
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
            auth.node_name,
            app_name,
            world_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = resolve_app(app_name)
        proxy_response = await _proxy_response(
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
            auth.node_name,
            app_name,
            world_name,
            z,
            tile_name,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = resolve_app(app_name)
        proxy_response = await _proxy_response(
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
            auth.node_name,
            app_name,
            asset_path,
        )
        auth.require_access(request, access_token, app_name=app_name, scopes=(NodeApiScope.MAP_READ,))
        app = resolve_app(app_name)
        proxy_response = await _proxy_response(
            app,
            f"images/{quote(asset_path, safe='/')}",
            request.url.query,
        )
        return Response(
            content=proxy_response.content,
            media_type=proxy_response.media_type,
            headers=_response_headers(proxy_response),
        )
