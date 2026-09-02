"""Squaremap proxying, manifest assembly, cache handling, and annotations."""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests

from apps._app import App
from map_annotations import (
    AppMapAnnotationStore,
    MapAnnotationDraft,
    MapAnnotationList,
    MapAnnotationMutationResult,
    MapManifest,
    MapWorldSummary,
)
from map_cache import AppMapJsonCacheStore, MapJsonCacheEntry

_SQUAREMAP_REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class NodeMapProxyResponse:
    content: bytes
    media_type: str | None
    headers: tuple[tuple[str, str], ...] = ()
    is_stale: bool = False
    cache_updated_at_unix_ms: int | None = None


class NodeMapService:
    """Owns Squaremap-backed map operations for a node API service."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        http_exception: Callable[[int, str], Exception],
        logger: logging.Logger,
    ) -> None:
        self._node_name = node_name
        self._http_exception = http_exception
        self._log = logger

    def build_manifest(self, app: App) -> MapManifest:
        manifest, _ = self.build_manifest_result(app)
        return manifest

    def build_manifest_result(
        self, app: App
    ) -> tuple[MapManifest, NodeMapProxyResponse]:
        public_map_url = self._require_map_app(app)
        settings_response = self.proxy_response(
            app=app,
            relative_path="tiles/settings.json",
            allow_stale_on_error=True,
        )
        settings = self._json_object_from_response(
            settings_response, "tiles/settings.json"
        )
        worlds = self._world_summaries(settings)
        if not worlds:
            raise self._http_exception(
                502, f"Squaremap did not expose any worlds for {app.friendly}."
            )
        initial_world_name = self._initial_world_name(public_map_url)
        known_world_names = {world.name.casefold() for world in worlds}
        if (
            initial_world_name is None
            or initial_world_name.casefold() not in known_world_names
        ):
            initial_world_name = worlds[0].name
        return (
            MapManifest(
                app_name=app.name,
                app_friendly=app.friendly,
                node_name=self._node_name(),
                public_map_url=public_map_url,
                icon_base_url="./assets",
                initial_world_name=initial_world_name,
                worlds=worlds,
            ),
            settings_response,
        )

    def build_annotation_list(self, app: App) -> MapAnnotationList:
        self._require_map_app(app)
        annotations = self._annotation_store(app).list_annotations()
        return MapAnnotationList(
            app_name=app.name,
            app_friendly=app.friendly,
            node_name=self._node_name(),
            annotations=annotations,
        )

    def create_annotation(
        self,
        *,
        app: App,
        draft: MapAnnotationDraft,
        created_by_user_id: int | None,
        created_by_name: str | None,
    ) -> MapAnnotationMutationResult:
        self._require_map_app(app)
        annotation = self._annotation_store(app).create_annotation(
            draft=draft,
            created_by_user_id=created_by_user_id,
            created_by_name=created_by_name,
        )
        return MapAnnotationMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node_name=self._node_name(),
            message=f"Created map annotation {annotation.annotation_id}.",
            annotation=annotation,
        )

    def delete_annotation(
        self, *, app: App, annotation_id: str
    ) -> MapAnnotationMutationResult:
        self._require_map_app(app)
        try:
            removed = self._annotation_store(app).delete_annotation(annotation_id)
        except KeyError as xcp:
            raise self._http_exception(
                404, f"Unknown map annotation: {annotation_id}"
            ) from xcp
        return MapAnnotationMutationResult(
            app_name=app.name,
            app_friendly=app.friendly,
            node_name=self._node_name(),
            message=f"Deleted map annotation {removed.annotation_id}.",
            deleted_annotation_id=removed.annotation_id,
        )

    def proxy_response(
        self,
        *,
        app: App,
        relative_path: str,
        raw_query: str = "",
        allow_stale_on_error: bool = False,
    ) -> NodeMapProxyResponse:
        normalized_path = relative_path.lstrip("/")
        local_response = self._local_proxy_response(app, normalized_path)
        if local_response is not None:
            return local_response
        url = f"{self._root_url(app)}{normalized_path}"
        params = parse_qs(raw_query, keep_blank_values=True) if raw_query else None
        should_log_failure = not normalized_path.casefold().endswith(".png")
        try:
            response = requests.get(
                url, params=params, timeout=_SQUAREMAP_REQUEST_TIMEOUT_SECONDS
            )
        except requests.Timeout as xcp:
            cached_response = (
                self._cached_response(app, normalized_path)
                if allow_stale_on_error
                else None
            )
            if should_log_failure:
                self._log.warning(
                    "Squaremap request timed out: app=%s url=%s query=%s stale_cache=%s",
                    app.name,
                    url,
                    raw_query,
                    cached_response is not None,
                )
            if cached_response is not None:
                return cached_response
            raise self._http_exception(
                504, f"Squaremap request timed out: {relative_path}"
            ) from xcp
        except requests.RequestException as xcp:
            cached_response = (
                self._cached_response(app, normalized_path)
                if allow_stale_on_error
                else None
            )
            if should_log_failure:
                self._log.warning(
                    "Squaremap request failed: app=%s url=%s query=%s stale_cache=%s error=%s: %s",
                    app.name,
                    url,
                    raw_query,
                    cached_response is not None,
                    type(xcp).__name__,
                    xcp,
                )
            if cached_response is not None:
                return cached_response
            raise self._http_exception(
                502, f"Squaremap request failed: {type(xcp).__name__}: {xcp}"
            ) from xcp
        if response.status_code >= 400:
            if response.status_code != 404 and allow_stale_on_error:
                cached_response = self._cached_response(app, normalized_path)
                if cached_response is not None:
                    if should_log_failure:
                        self._log.warning(
                            "Squaremap returned HTTP %s: app=%s url=%s query=%s stale_cache=%s",
                            response.status_code,
                            app.name,
                            url,
                            raw_query,
                            True,
                        )
                    return cached_response
            if should_log_failure:
                self._log.warning(
                    "Squaremap returned HTTP %s: app=%s url=%s query=%s stale_cache=%s",
                    response.status_code,
                    app.name,
                    url,
                    raw_query,
                    False,
                )
            status_code = 404 if response.status_code == 404 else 502
            raise self._http_exception(
                status_code,
                f"Squaremap returned HTTP {response.status_code} for {relative_path}.",
            )
        proxy_response = NodeMapProxyResponse(
            content=response.content,
            media_type=response.headers.get("Content-Type"),
            headers=self._passthrough_headers(response),
        )
        self._remember_cache_entry(app, normalized_path, proxy_response)
        return proxy_response

    def _require_map_app(self, app: App) -> str:
        public_map_url = app.public_map_url
        if public_map_url is None:
            raise self._http_exception(
                404, f"{app.friendly} does not expose a public map."
            )
        return public_map_url

    @staticmethod
    def _annotation_store(app: App) -> AppMapAnnotationStore:
        return AppMapAnnotationStore(app.map_annotations_path)

    def _root_url(self, app: App) -> str:
        map_proxy_url = app.map_proxy_url or self._require_map_app(app)
        parsed = urlsplit(map_proxy_url)
        root_path = parsed.path.rstrip("/") + "/"
        return urlunsplit((parsed.scheme, parsed.netloc, root_path, "", ""))

    @staticmethod
    def _initial_world_name(public_map_url: str) -> str | None:
        parsed = urlsplit(public_map_url)
        world_values = parse_qs(parsed.query, keep_blank_values=True).get("world", ())
        for candidate in reversed(world_values):
            world_name = candidate.strip()
            if world_name:
                return world_name
        return None

    @staticmethod
    def _passthrough_headers(
        response: requests.Response,
    ) -> tuple[tuple[str, str], ...]:
        allowed_names = ("Cache-Control", "ETag", "Last-Modified", "Expires")
        return tuple(
            (name, value)
            for name in allowed_names
            if (value := response.headers.get(name))
        )

    def _local_proxy_response(
        self, app: App, relative_path: str
    ) -> NodeMapProxyResponse | None:
        root_path = app.map_proxy_root_path
        if root_path is None:
            return None
        resolved_root = root_path.resolve()
        resolved_path = (resolved_root / relative_path).resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            raise self._http_exception(
                400, f"Invalid Squaremap path: {relative_path}"
            ) from None
        if not resolved_path.is_file():
            return None
        media_type, _ = mimetypes.guess_type(resolved_path.name)
        return NodeMapProxyResponse(
            content=resolved_path.read_bytes(), media_type=media_type
        )

    def _cached_response(
        self, app: App, relative_path: str
    ) -> NodeMapProxyResponse | None:
        cache_entry = self._cache_entry(app, relative_path)
        if cache_entry is None:
            return None
        return NodeMapProxyResponse(
            content=cache_entry.content.encode("utf-8"),
            media_type=cache_entry.media_type,
            headers=cache_entry.header_pairs,
            is_stale=True,
            cache_updated_at_unix_ms=cache_entry.updated_at_unix_ms,
        )

    def _cache_entry(self, app: App, relative_path: str) -> MapJsonCacheEntry | None:
        try:
            return self._json_cache_store(app).load_entry(relative_path)
        except ValueError:
            self._log.exception(
                "Map cache for %s is invalid at %s", app.friendly, app.map_cache_path
            )
            return None

    def _remember_cache_entry(
        self,
        app: App,
        relative_path: str,
        proxy_response: NodeMapProxyResponse,
    ) -> None:
        if not self._should_cache_path(relative_path):
            return
        try:
            content_text = proxy_response.content.decode("utf-8")
        except UnicodeDecodeError:
            self._log.warning(
                "Skipping map cache write for %s because %s was not UTF-8 JSON.",
                app.friendly,
                relative_path,
            )
            return
        try:
            self._json_cache_store(app).save_entry(
                relative_path=relative_path,
                content=content_text,
                media_type=proxy_response.media_type,
                headers=proxy_response.headers,
            )
        except ValueError:
            self._log.exception(
                "Failed to update map cache for %s at %s",
                app.friendly,
                app.map_cache_path,
            )

    @staticmethod
    def _should_cache_path(relative_path: str) -> bool:
        normalized_path = relative_path.lstrip("/")
        if normalized_path == "tiles/settings.json":
            return True
        return normalized_path.startswith("tiles/") and (
            normalized_path.endswith("/settings.json")
            or normalized_path.endswith("/markers.json")
        )

    @staticmethod
    def _json_cache_store(app: App) -> AppMapJsonCacheStore:
        return AppMapJsonCacheStore(app.map_cache_path)

    def _json_object_from_response(
        self,
        proxy_response: NodeMapProxyResponse,
        relative_path: str,
    ) -> Mapping[str, object]:
        try:
            payload = cast(object, json.loads(proxy_response.content.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as xcp:
            raise self._http_exception(
                502, f"Squaremap returned invalid JSON for {relative_path}."
            ) from xcp
        if not isinstance(payload, Mapping):
            raise self._http_exception(
                502, f"Squaremap returned an invalid JSON object for {relative_path}."
            )
        return cast(Mapping[str, object], payload)

    def _world_summaries(
        self, payload: Mapping[str, object]
    ) -> tuple[MapWorldSummary, ...]:
        raw_worlds = payload.get("worlds")
        if isinstance(raw_worlds, (str, bytes)) or not isinstance(raw_worlds, Sequence):
            raise self._http_exception(502, "Squaremap world list is invalid.")
        worlds: list[MapWorldSummary] = []
        for index, raw_world in enumerate(raw_worlds):
            if not isinstance(raw_world, Mapping):
                raise self._http_exception(502, "Squaremap world entry is invalid.")
            name = self._mapping_text(raw_world, ("name",))
            worlds.append(
                MapWorldSummary(
                    name=name,
                    display_name=self._mapping_text(
                        raw_world, ("display_name", "title", "name")
                    ),
                    world_type=self._mapping_text(
                        raw_world, ("type",), default="normal"
                    ),
                    order=self._mapping_int(raw_world, "order", default=index),
                )
            )
        return tuple(
            sorted(
                worlds,
                key=lambda world: (
                    world.order,
                    world.display_name.casefold(),
                    world.name.casefold(),
                ),
            )
        )

    def _mapping_text(
        self,
        payload: Mapping[str, object],
        keys: tuple[str, ...],
        *,
        default: str | None = None,
    ) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and (text := value.strip()):
                return text
        if default is not None:
            return default
        raise self._http_exception(
            502,
            f"Squaremap payload is missing required text fields: {', '.join(keys)}.",
        )

    def _mapping_int(
        self, payload: Mapping[str, object], key: str, *, default: int
    ) -> int:
        value = payload.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._http_exception(502, f"Squaremap field {key!r} is invalid.")
        return value
