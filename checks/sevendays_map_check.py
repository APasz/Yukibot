from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter
from starlette.requests import Request
from starlette.responses import Response

from apps._telnet import TelnetClient
from apps.sevendays import Players, SevenDays
from apps.sevendays.installation import UNDEAD_LEGACY_MANIFEST, UndeadLegacyInstallation
from apps.sevendays.map import MapTileIndex, MapTileRange, SevenDaysMap, SevenDaysMapCache
from map_annotations import MapAnnotationDraft, MapAnnotationShape, MapCoordinate, MapManifest, MapProvider, MapReadiness, MapWorldSettings
from node_api.map_routes import register_map_routes
from node_api.app_state import NodeAppEntry
from node_api.map_service import NodeMapProxyResponse, NodeMapService
from mod_web_auth import ModWebUser
from node_auth import NodeApiScope
from web_dash.types import ModWebNodeLink
from web_dash.page_handlers import ModWebPageHandlersMixin
from web_dash.service import ModWebService
from web_dash.types import ModWebAppTabContext


def _png(color: tuple[int, int, int, int] = (80, 120, 80, 255)) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

    pixels = b"".join(b"\0" + bytes(color) * 128 for _ in range(128))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 6, 0, 0, 0)) + chunk(
        b"IDAT", zlib.compress(pixels)
    ) + chunk(b"IEND", b"")


def _app(root: Path, *, overhaul: bool = False) -> SevenDays:
    root.mkdir(exist_ok=True)
    if overhaul:
        manifest = root / UNDEAD_LEGACY_MANIFEST
        manifest.parent.mkdir()
        manifest.write_text(UndeadLegacyInstallation(
            version="2.7.40", game_version="2.6", core_sha256="0" * 64, mod_names=("UndeadLegacy",),
        ).model_dump_json())
    app = object.__new__(SevenDays)
    app.directory = root
    app.name = "sevendays_alpha"
    app.friendly = "7D2D"
    app._rendered_map_cache = SevenDaysMapCache()
    app._tail_matchers = set()
    app._console_response_matchers = set()
    app._players = Players(app)
    _configure(app)
    return app


def _configure(app: SevenDays, *, enabled: bool = True, save: str = "The Leg") -> None:
    (app.directory / "serverconfig.xml").write_text(
        '<ServerSettings><property name="GameWorld" value="Kotavu County" />'
        f'<property name="GameName" value="{save}" />'
        f'<property name="EnableMapRendering" value="{str(enabled).lower()}" />'
        '<property name="WebDashboardEnabled" value="false" /></ServerSettings>'
    )


def _tiles(app: SevenDays) -> Path:
    save = app._save_directory_path()
    assert save is not None
    root = save / "map"
    (root / "4" / "-2").mkdir(parents=True)
    (root / "mapinfo.json").write_text('\ufeff{"blockSize":128,"maxZoom":4}')
    (root / "4" / "-2" / "3.png").write_bytes(_png())
    return root


def _service() -> NodeMapService:
    return NodeMapService(
        node_name=lambda: "erin", http_exception=lambda status, detail: HTTPException(status, detail),
        logger=logging.getLogger(__name__),
    )


def _portal_map_api(service: ModWebService) -> FastAPI:
    api = FastAPI()
    route_app = Mock()
    route_app.get.side_effect = api.get
    route_app.post.side_effect = api.post
    route_app.put.side_effect = api.put
    route_app.exception_handlers = {}
    with patch.object(service._backend, "register_node_api_routes"):
        service._register_routes(nicegui_app=route_app, ui=Mock())
    return api


@pytest.mark.parametrize("overhaul", [False, True])
def test_rendered_map_requires_setting_metadata_and_real_tiles(tmp_path: Path, overhaul: bool) -> None:
    app = _app(tmp_path, overhaul=overhaul)
    assert app.rendered_map is None

    root = _tiles(app)
    assert app.rendered_map is not None
    assert ("UndeadLegacy" in root.parts) is overhaul
    _configure(app, enabled=False)
    assert app.rendered_map is None
    with pytest.raises(HTTPException) as failure:
        _service().tile_response(app, "unknown", 4, "-2_-4.png")
    assert failure.value.status_code == 404
    _configure(app)
    (root / "4" / "-2" / "3.png").write_bytes(b"incomplete")
    assert app.rendered_map is None
    (root / "4" / "-2" / "3.png").unlink()
    assert app.rendered_map is None


def test_map_capability_and_readiness_survive_enabling_rendering(tmp_path: Path) -> None:
    app = _app(tmp_path)
    service = _service()
    assert app.supports_map
    _configure(app, enabled=False)
    manifest = service.build_manifest(app)
    assert manifest.readiness is MapReadiness.DISABLED and not manifest.worlds
    assert MapManifest.from_mapping(manifest.to_mapping()) == manifest
    _configure(app)
    assert service.build_manifest(app).readiness is MapReadiness.WAITING_FOR_TILES
    _tiles(app)
    assert service.build_manifest(app).readiness is MapReadiness.READY
    app._map_rendering_at_start = False
    app.process = Mock()
    app.process.poll.return_value = None
    assert service.build_manifest(app).readiness is MapReadiness.RESTART_REQUIRED


def test_current_save_controls_tiles_and_annotation_identity(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    original = app.rendered_map
    assert original is not None
    _configure(app, save="Another Save")
    assert app.rendered_map is None
    _tiles(app)
    current = app.rendered_map
    assert current is not None and current.world_id != original.world_id
    with pytest.raises(HTTPException) as failure:
        _service().world_response(app, original.world_id, "settings")
    assert failure.value.status_code == 404


@pytest.mark.parametrize("overhaul", [False, True])
def test_native_manifest_and_tile_orientation_need_no_game_web_server(tmp_path: Path, overhaul: bool) -> None:
    app = _app(tmp_path, overhaul=overhaul)
    app.process = Mock()
    app.process.poll.return_value = 0
    app._map_rendering_at_start = False
    assert not app.check_running()
    root = _tiles(app)
    service = _service()
    with patch("node_api.map_service.requests.Session.get", side_effect=AssertionError("No upstream requests")):
        manifest = service.build_manifest(app)
        assert manifest.provider is MapProvider.SEVENDAYS
        assert manifest.readiness is MapReadiness.READY
        assert manifest.supports_tile_index
        assert manifest.public_map_url is None
        assert MapManifest.from_mapping(manifest.to_mapping()) == manifest
        world = manifest.initial_world_name
        settings = json.loads(service.world_response(app, world, "settings").content)
        assert settings["tile_size"] == 128
        assert settings["zoom"]["def"] == 4
        assert settings["north_positive"] is True
        assert settings["spawn"] == {"x": -192.0, "z": 448.0}
        assert settings["player_tracker"]["enabled"] is True
        assert settings["player_tracker"]["max_age_seconds"] == 15
        assert json.loads(service.world_response(app, world, "markers").content) == []
        assert json.loads(service.players_response(app).content) == {"players": [], "max_age_seconds": 0.0}
        response = service.tile_response(app, world, 4, "-2_-4.png")
        assert response.media_type == "image/png"
        assert response.content == (root / "4" / "-2" / "3.png").read_bytes()
        with pytest.raises(HTTPException) as failure:
            service.tile_response(app, world, 4, "-2_3.png")
        assert failure.value.status_code == 404


@pytest.mark.parametrize("nested_map", [False, True])
def test_map_api_exposes_only_fresh_positions_for_the_selected_running_save(tmp_path: Path, nested_map: bool) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    if nested_map:
        destination = root.parent / "cache" / "rendered"
        destination.parent.mkdir()
        root.rename(destination)
        root.symlink_to(destination, target_is_directory=True)
    app.process = Mock()
    app.process.poll.return_value = None
    relay = Mock(spec=TelnetClient)
    relay.send = AsyncMock(return_value=True)
    relay.reader = None
    relay.is_connected = True
    app._relay = relay
    service = _service()
    world_id = service.build_manifest(app).initial_world_name

    async def check() -> None:
        try:
            await app._players.start(save=app._save_directory_path())
            await app._players.match_players("0. id=177, Alice <3, pos=(-10.5, 72, 20.25), rot=(0, 0, 0), ip=private")
            await app._players.match_players("Total of 1 in the game")
            response = json.loads(service.players_response(app).content)
            assert response["players"] == [{"uuid": "177", "name": "Alice <3", "world": world_id, "x": -10.5, "z": 20.25}]
            assert 0 < response["max_age_seconds"] <= 15
            _configure(app, save="Another Save")
            _tiles(app)
            assert json.loads(service.players_response(app).content)["players"] == []
            _configure(app)
            relay.is_connected = False
            assert json.loads(service.players_response(app).content)["players"] == []
        finally:
            await app._players.stop()

    asyncio.run(check())


@pytest.mark.parametrize("tile_name, zoom", [("../../serverconfig.xml", 4), ("0_0.png", -1), ("0_0.png", 5)])
def test_invalid_tile_requests_are_rejected(tmp_path: Path, tile_name: str, zoom: int) -> None:
    app = _app(tmp_path)
    _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    with pytest.raises(HTTPException) as failure:
        _service().tile_response(app, rendered.world_id, zoom, tile_name)
    assert failure.value.status_code == 400


def test_tiles_and_metadata_cannot_follow_links_outside_the_map(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    outside = tmp_path / "private.png"
    outside.write_bytes(_png())
    tile = root / "4" / "-2" / "3.png"
    tile.unlink()
    tile.symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        rendered.read_tile(zoom=4, tile_name="-2_-4.png")
    with pytest.raises(ValueError, match="outside"):
        SevenDaysMap.load(save=root.parent, world_id="test")
    tile.unlink()
    tile.write_bytes(_png())
    metadata = root / "mapinfo.json"
    outside_metadata = tmp_path / "private.json"
    metadata.rename(outside_metadata)
    metadata.symlink_to(outside_metadata)
    with pytest.raises(ValueError, match="outside"):
        SevenDaysMap.load(save=root.parent, world_id="test")


@pytest.mark.parametrize("relative_path", ["4/-2/3.png", "4/-2", "4"])
def test_tile_index_rejects_links_outside_the_map(tmp_path: Path, relative_path: str) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    source = root / relative_path
    outside = tmp_path / "outside"
    source.rename(outside)
    source.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(ValueError, match="outside"):
        rendered.tile_index()


def test_tile_discovery_and_index_share_path_rules_and_allow_internal_links(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    tile = root / "4" / "-2" / "3.png"
    target = root / "sample.png"
    tile.rename(target)
    tile.symlink_to(target)
    (root / "0").write_text("Not a zoom directory")
    (root / "4" / "99").write_text("Not a column directory")
    (root / "4" / "-2" / "5.png").mkdir()
    rendered = SevenDaysMap.load(save=root.parent, world_id="test")
    assert rendered is not None
    assert set(rendered.tile_index().tiles) == {(4, -2, -4)}
    assert rendered.read_tile(zoom=4, tile_name="-2_-4.png") == _png()


@pytest.mark.parametrize("metadata", ['[]', '{"blockSize":true,"maxZoom":4}', '{"blockSize":128,"maxZoom":-1}', '{'])
def test_invalid_metadata_leaves_the_map_waiting(tmp_path: Path, metadata: str) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    (root / "mapinfo.json").write_text(metadata)
    with pytest.raises(ValueError):
        SevenDaysMap.load(save=root.parent, world_id="test")
    assert app.rendered_map is None


def test_internal_map_capability_survives_node_transport_and_selects_tab() -> None:
    entry = NodeAppEntry(
        name="sevendays_alpha", friendly="7D2D", node="erin", running=False, enabled=True,
        supports_mods=False, supports_configs=False, supports_map=True,
    )
    restored = NodeAppEntry.from_mapping(entry.to_mapping())
    assert restored.supports_map and restored.map_url is None
    request = Request({"type": "http", "query_string": b"tab=map"})
    assert ModWebPageHandlersMixin._requested_app_tab_id(
        request=request, app_entry=restored, can_manage_app=False,
    ) == "map"
    context = ModWebAppTabContext(app_name=restored.name, app_scope="sevendays", supports_map=True)
    tabs = ModWebService()._additional_app_tab_definitions(context=context, is_detail_page=True)
    assert "map" in {tab.tab_id for tab in tabs}
    assert not replace(restored, supports_map=False).supports_map


def test_legacy_public_maps_still_advertise_map_support() -> None:
    entry = NodeAppEntry(
        name="minecraft_alpha", friendly="Minecraft", node="yuki", running=False, enabled=True,
        supports_mods=False, supports_configs=False, map_url="https://example.test/map",
    )
    payload = entry.to_mapping()
    payload.pop("supports_map")
    assert NodeAppEntry.from_mapping(payload).supports_map
    payload["supports_map"] = "true"
    with pytest.raises(ValueError, match="supports_map"):
        NodeAppEntry.from_mapping(payload)


def test_concurrent_tile_requests_share_discovery_and_read_fresh_bytes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    service = _service()
    with patch.object(SevenDaysMap, "load", wraps=SevenDaysMap.load) as discover:
        manifest = service.build_manifest(app)
        with ThreadPoolExecutor(max_workers=8) as workers:
            responses = list(workers.map(
                lambda _: service.tile_response(app, manifest.initial_world_name, 4, "-2_-4.png"), range(64),
            ))
        assert discover.call_count == 1
        assert all(response.content == _png() for response in responses)
        replacement = _png((15, 50, 170, 255))
        (root / "4" / "-2" / "3.png").write_bytes(replacement)
        assert service.tile_response(app, manifest.initial_world_name, 4, "-2_-4.png").content == replacement
        assert discover.call_count == 2


def test_map_selection_uses_one_consistent_configuration_snapshot(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    with patch.object(app, "_load_serverconfig_snapshot", wraps=app._load_serverconfig_snapshot) as snapshot:
        assert app.rendered_map is not None
    assert snapshot.call_count == 1


def test_map_discovery_retries_after_a_short_negative_cache(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    tile = root / "4" / "-2" / "3.png"
    tile.write_bytes(_png()[:-12])
    with (
        patch("apps.sevendays.map.time.monotonic", return_value=100.0) as clock,
        patch.object(SevenDaysMap, "load", wraps=SevenDaysMap.load) as discover,
    ):
        assert app.rendered_map is None
        tile.write_bytes(_png())
        assert app.rendered_map is None
        assert discover.call_count == 1
        clock.return_value += 3
        assert app.rendered_map is not None
        assert discover.call_count == 2


def test_invalid_metadata_does_not_cause_repeated_discovery_or_log_spam(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    (root / "mapinfo.json").write_text('{')
    with (
        patch("apps.sevendays.map.time.monotonic", return_value=100.0),
        patch.object(SevenDaysMap, "load", wraps=SevenDaysMap.load) as discover,
        patch("apps.sevendays.map.log.warning") as warning,
    ):
        for _ in range(10):
            assert app.rendered_map is None
        assert discover.call_count == warning.call_count == 1


def test_partial_tiles_are_not_discovered_or_returned_as_valid_pngs(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    (root / "4" / "-2" / "4.png").write_bytes(_png())
    (root / "4" / "-2" / "3.png").write_bytes(_png()[:-12])
    rendered = app.rendered_map
    assert rendered is not None and rendered.sample_tile.name == "4.png"
    with pytest.raises(HTTPException) as failure:
        _service().tile_response(app, rendered.world_id, 4, "-2_-4.png")
    assert failure.value.status_code == 503


def test_cached_map_cannot_follow_a_replaced_root_link(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    outside = tmp_path / "outside"
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        rendered.read_tile(zoom=4, tile_name="-2_-4.png")


def test_tile_index_is_bounded_shared_and_reuses_validated_pngs(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    with (
        patch("apps.sevendays.map.time.monotonic", return_value=100.0) as clock,
        patch.object(SevenDaysMap, "tile_index", wraps=rendered.tile_index) as scan,
    ):
        with ThreadPoolExecutor(max_workers=8) as workers:
            indexes = list(workers.map(lambda _: app.rendered_tile_index(rendered), range(32)))
        assert scan.call_count == 1
        assert all(index is indexes[0] for index in indexes)
        content, etag = indexes[0].viewport_response(MapTileRange(zoom=4, min_x=-2, max_x=-2, min_y=-4, max_y=-4))
        assert len(json.loads(content)["tiles"]) == 1
        empty, _ = indexes[0].viewport_response(MapTileRange(zoom=4, min_x=0, max_x=0, min_y=0, max_y=0))
        assert json.loads(empty)["tiles"] == []
        (root / "4" / "-2" / "5.png").write_bytes(_png())
        assert len(app.rendered_tile_index(rendered).tiles) == 1
        clock.return_value = 116.0
        updated = app.rendered_tile_index(rendered)
        assert len(updated.tiles) == 2
        assert updated.viewport_response(MapTileRange(zoom=4, min_x=-2, max_x=-2, min_y=-4, max_y=-4))[1] != etag
    with pytest.raises(ValueError, match="4096"):
        MapTileRange(zoom=4, min_x=0, max_x=100, min_y=0, max_y=100)


def test_incomplete_tile_updates_retain_the_previous_index_revision(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    old = rendered.tile_index()
    tile = root / "4" / "-2" / "3.png"
    tile.write_bytes(_png()[:-12])
    assert rendered.tile_index(old).tiles == old.tiles
    tile.write_bytes(_png((5, 15, 25, 255)))
    assert rendered.tile_index(old).tiles != old.tiles
    tile.unlink()
    assert not rendered.tile_index(old).tiles


def test_tile_index_scan_does_not_block_tile_reads(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    scanning = threading.Event()
    release = threading.Event()

    def scan(previous: MapTileIndex | None = None) -> MapTileIndex:
        scanning.set()
        assert release.wait(timeout=5)
        return MapTileIndex()

    with patch.object(SevenDaysMap, "tile_index", side_effect=scan), ThreadPoolExecutor(max_workers=2) as workers:
        pending_index = workers.submit(app.rendered_tile_index, rendered)
        try:
            assert scanning.wait(timeout=2)
            tile = workers.submit(_service().tile_response, app, rendered.world_id, 4, "-2_-4.png")
            assert tile.result(timeout=2).content == _png()
        finally:
            release.set()
        pending_index.result(timeout=2)


def test_tile_index_cache_lifetime_starts_after_scan_finishes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    rendered = app.rendered_map
    assert rendered is not None
    with (
        patch("apps.sevendays.map.time.monotonic", side_effect=[100.0, 120.0, 121.0]),
        patch.object(SevenDaysMap, "tile_index", wraps=rendered.tile_index) as scan,
    ):
        assert app.rendered_tile_index(rendered) is app.rendered_tile_index(rendered)
        assert scan.call_count == 1


def test_map_http_revalidates_tiles_and_indexes_after_authentication(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    maps = _service()
    world = maps.build_manifest(app).initial_world_name
    auth = Mock()
    auth.node_name = "erin"
    api = FastAPI()
    register_map_routes(
        api, auth=auth, resolve_app=lambda _: app, maps=maps, api_prefix="/api/node",
        map_annotation_creator_name=lambda *_: None, traffic_log=logging.getLogger(__name__),
    )
    base = f"/api/node/apps/{app.name}/map/worlds/{world}"
    async def check() -> None:
        async with AsyncClient(transport=ASGITransport(app=api), base_url="http://map.test") as client:
            settings = await client.get(f"{base}/settings")
            TypeAdapter(MapWorldSettings).validate_json(settings.content)
            for path in (f"{base}/tiles/4/-2_-4.png", f"{base}/tiles/index?zoom=4&min_x=-2&max_x=-2&min_y=-4&max_y=-4"):
                response = await client.get(path)
                assert response.status_code == 200
                assert response.headers["cache-control"] == "private, no-cache"
                etag = response.headers["etag"]
                unchanged = await client.get(path, headers={"If-None-Match": f"W/{etag}"})
                assert unchanged.status_code == 304 and unchanged.content == b""
                auth.require_access.side_effect = HTTPException(403, "Access revoked")
                assert (await client.get(path, headers={"If-None-Match": etag})).status_code == 403
                auth.require_access.side_effect = None
            assert (await client.get(f"{base}/tiles/index?zoom=4&min_x=0&max_x=100&min_y=0&max_y=100")).status_code == 400

    asyncio.run(check())


@pytest.mark.parametrize("status", [200, 304, 403, 404, 500, 503])
@pytest.mark.parametrize("resource", ["players", "annotations"])
def test_portal_map_proxy_preserves_validators_freshness_and_status(status: int, resource: str) -> None:
    service = ModWebService()
    api = _portal_map_api(service)
    node = ModWebNodeLink(
        node_name="erin", label="Erin", url="/mod-web/nodes/erin",
        api_base_url="https://erin.example/api/node", api_url="/api/node-proxy/erin/apps", is_current=False,
    )
    user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
    context = AsyncMock()

    async def chunks(_: int):
        yield b"map response"

    response = Mock()
    response.status = status
    response.headers = {
        "Content-Type": "application/json", "Cache-Control": "private, no-cache", "ETag": '"tile-one"',
        "X-Yukibot-Map-Source": "stale", "X-Yukibot-Map-Cache-Updated-At": "1234",
    }
    response.content_length = 12
    response.content.iter_chunked = chunks
    context.__aenter__.return_value = response
    session = Mock()
    session.get.return_value = context

    async def check() -> None:
        with (
            patch.object(service, "_require_http_user", return_value=user),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(service, "_remote_http_client", new=AsyncMock(return_value=session)),
            patch.object(service, "_remote_token", return_value="read-only-token"),
        ):
            async with AsyncClient(transport=ASGITransport(app=api), base_url="http://map.test") as client:
                result = await client.get(
                    f"/api/node-proxy/erin/apps/sevendays/map/{resource}",
                    headers={"If-None-Match": '"tile-one"'},
                )
            assert result.status_code == status
            assert result.headers["etag"] == '"tile-one"'
            assert result.headers["x-yukibot-map-source"] == "stale"
            assert session.get.call_args.kwargs["headers"]["If-None-Match"] == '"tile-one"'
            assert result.content == (b"" if status == 304 else b"map response")
            context.__aexit__.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("suffix", [
    "/manifest", "/annotations", "/players", "/worlds/save-one/settings",
    "/worlds/save-one/tiles/index?zoom=4&min_x=0&max_x=1&min_y=0&max_y=1",
])
@pytest.mark.parametrize("error, status, detail", [
    (aiohttp.ClientConnectionError("Connection refused"), 503, "is temporarily unavailable"),
    (aiohttp.ConnectionTimeoutError("Connection timed out"), 504, "did not respond in time"),
])
def test_portal_map_reads_explain_node_outages(suffix: str, error: Exception, status: int, detail: str) -> None:
    service = ModWebService()
    api = _portal_map_api(service)
    node = ModWebNodeLink(
        node_name="erin", label="Erin", url="/mod-web/nodes/erin",
        api_base_url="https://erin.example/api/node", api_url="/api/node-proxy/erin/apps", is_current=False,
    )
    user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
    context = AsyncMock()
    context.__aenter__.side_effect = error
    session = Mock()
    session.get.return_value = context
    service._record_remote_node_failure(node)

    async def check() -> None:
        with (
            patch.object(service, "_require_http_user", return_value=user),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(service, "_remote_token", return_value="read-only-token"),
            patch.object(service, "_remote_http_client", new=AsyncMock(return_value=session)),
        ):
            async with AsyncClient(transport=ASGITransport(app=api), base_url="http://map.test") as client:
                response = await client.get(f"/api/node-proxy/erin/apps/sevendays/map{suffix}")
        assert response.status_code == status
        assert response.json() == {"detail": f"Erin {detail}. The map will retry automatically."}
        session.get.assert_called_once()

    asyncio.run(check())


def test_portal_tile_route_forwards_revisions_without_forwarding_url_credentials() -> None:
    service = ModWebService()
    api = _portal_map_api(service)
    node = ModWebNodeLink(
        node_name="erin", label="Erin", url="/mod-web/nodes/erin",
        api_base_url="https://erin.example/api/node", api_url="/api/node-proxy/erin/apps", is_current=False,
    )
    user = ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None)
    stream = AsyncMock(return_value=Response(b"tile", media_type="image/png"))

    async def check() -> None:
        with (
            patch.object(service, "_require_http_user", return_value=user),
            patch.object(service, "_remote_node_link", return_value=node),
            patch.object(service, "_remote_stream_response_async", stream),
        ):
            async with AsyncClient(transport=ASGITransport(app=api), base_url="http://map.test") as client:
                response = await client.get(
                    "/api/node-proxy/erin/apps/minecraft/map/worlds/world/tiles/4/0_0.png"
                    "?revision=current&tile_rev=legacy&access_token=untrusted&extra=ignored"
                )
                assert response.status_code == 200 and response.content == b"tile"
        assert stream.await_args is not None
        assert stream.await_args.kwargs["query"] == {"revision": "current", "tile_rev": "legacy"}
        assert stream.await_args.kwargs["scopes"] == (NodeApiScope.MAP_READ,)

    asyncio.run(check())


def _draft(world_id: str, label: str = "Marker") -> MapAnnotationDraft:
    return MapAnnotationDraft(
        world_name=world_id, shape=MapAnnotationShape.MARKER, label=label, color_hex="#123456",
        points=(MapCoordinate(x=1, z=2),),
    )


def test_concurrent_annotation_writes_are_retained(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    service = _service()
    world_id = service.build_manifest(app).initial_world_name
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda index: service.create_annotation(
            app=app, draft=_draft(world_id, f"Marker {index}"), created_by_user_id=index, created_by_name=None,
        ), range(32)))
    annotations = service.build_annotation_list(app).annotations
    assert {annotation.label for annotation in annotations} == {f"Marker {index}" for index in range(32)}


def test_concurrent_squaremap_cache_updates_are_retained(tmp_path: Path) -> None:
    app = _app(tmp_path)
    service = _service()
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda index: service._json_cache_store(app).save_entry(
            relative_path=f"tiles/world-{index}/settings.json", content="{}", media_type="application/json", headers=(),
        ), range(32)))
    assert all(
        service._json_cache_store(app).load_entry(f"tiles/world-{index}/settings.json") is not None
        for index in range(32)
    )


def test_optional_map_cache_io_failures_leave_live_data_usable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    service = _service()
    response = NodeMapProxyResponse(content=b"{}", media_type="application/json")
    store = service._json_cache_store(app)
    with patch.object(store, "save_entry", side_effect=OSError("Disk full")):
        service._remember_cache_entry(app, "tiles/settings.json", response)
    with patch.object(store, "load_entry", side_effect=PermissionError("Not readable")):
        assert service._cache_entry(app, "tiles/settings.json") is None


def test_stale_clients_cannot_modify_annotations_in_a_different_save(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    service = _service()
    original_world = service.build_manifest(app).initial_world_name
    created = service.create_annotation(app=app, draft=_draft(original_world), created_by_user_id=1, created_by_name=None)
    assert created.annotation is not None
    _configure(app, save="Different Save")
    _tiles(app)
    assert service.build_annotation_list(app).annotations == ()
    with pytest.raises(HTTPException) as failure:
        service.create_annotation(app=app, draft=_draft(original_world), created_by_user_id=1, created_by_name=None)
    assert failure.value.status_code == 404
    with pytest.raises(HTTPException) as failure:
        service.delete_annotation(app=app, annotation_id=created.annotation.annotation_id)
    assert failure.value.status_code == 404
    _configure(app)
    assert service.build_annotation_list(app).annotations == (created.annotation,)


@pytest.mark.parametrize("provider", [None, "", " ", True, "other"])
def test_map_manifest_rejects_invalid_explicit_provider(tmp_path: Path, provider: object) -> None:
    app = _app(tmp_path)
    _tiles(app)
    payload = _service().build_manifest(app).to_mapping()
    payload["provider"] = provider
    with pytest.raises(ValueError):
        MapManifest.from_mapping(payload)


def test_legacy_manifest_defaults_to_unindexed_tiles(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    payload = _service().build_manifest(app).to_mapping()
    del payload["supports_tile_index"]
    assert not MapManifest.from_mapping(payload).supports_tile_index


@pytest.mark.parametrize("capability", [None, 0, 1, "true"])
def test_map_manifest_requires_a_boolean_tile_index_capability(tmp_path: Path, capability: object) -> None:
    app = _app(tmp_path)
    _tiles(app)
    payload = _service().build_manifest(app).to_mapping()
    payload["supports_tile_index"] = capability
    with pytest.raises(ValueError, match="supports_tile_index"):
        MapManifest.from_mapping(payload)


@pytest.mark.parametrize("tile_name", ["2147483648_0.png", "0_-2147483649.png", "999999999999999999999_0.png"])
def test_out_of_range_tile_indexes_are_rejected(tmp_path: Path, tile_name: str) -> None:
    app = _app(tmp_path)
    _tiles(app)
    world_id = _service().build_manifest(app).initial_world_name
    with pytest.raises(HTTPException) as failure:
        _service().tile_response(app, world_id, 4, tile_name)
    assert failure.value.status_code == 400
