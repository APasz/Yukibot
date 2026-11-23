from __future__ import annotations

import asyncio
import json
import logging
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps._telnet import TelnetClient
from apps.sevendays import Players, SevenDays
from apps.sevendays.installation import UNDEAD_LEGACY_MANIFEST, UndeadLegacyInstallation
from apps.sevendays.map import SevenDaysMap, SevenDaysMapCache
from map_annotations import MapAnnotationDraft, MapAnnotationShape, MapCoordinate, MapManifest, MapProvider
from node_api.app_state import NodeAppEntry
from node_api.map_service import NodeMapProxyResponse, NodeMapService
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


@pytest.mark.parametrize("overhaul", [False, True])
def test_map_availability_requires_setting_metadata_and_real_tiles(tmp_path: Path, overhaul: bool) -> None:
    app = _app(tmp_path, overhaul=overhaul)
    assert not app.supports_map
    root = _tiles(app)
    assert app.supports_map
    assert ("UndeadLegacy" in root.parts) is overhaul
    _configure(app, enabled=False)
    assert not app.supports_map
    with pytest.raises(HTTPException) as failure:
        _service().tile_response(app, "unknown", 4, "-2_-4.png")
    assert failure.value.status_code == 404
    _configure(app)
    (root / "4" / "-2" / "3.png").write_bytes(b"incomplete")
    assert not app.supports_map
    (root / "4" / "-2" / "3.png").unlink()
    assert not app.supports_map


def test_current_save_controls_tiles_and_annotation_identity(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _tiles(app)
    original = app.rendered_map
    assert original is not None
    _configure(app, save="Another Save")
    assert not app.supports_map
    _tiles(app)
    current = app.rendered_map
    assert current is not None and current.world_id != original.world_id
    with pytest.raises(HTTPException) as failure:
        _service().world_response(app, original.world_id, "settings")
    assert failure.value.status_code == 404


def test_native_manifest_and_tile_orientation_need_no_game_web_server(tmp_path: Path) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    service = _service()
    with patch("node_api.map_service.requests.Session.get", side_effect=AssertionError("No upstream requests")):
        manifest = service.build_manifest(app)
        assert manifest.provider is MapProvider.SEVENDAYS
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
    app._relay = Mock(spec=TelnetClient)
    app._relay.send = AsyncMock(return_value=True)
    app._relay.reader = None
    app._relay.is_connected = True
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
            app._relay.is_connected = False
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


@pytest.mark.parametrize("metadata", ['[]', '{"blockSize":true,"maxZoom":4}', '{"blockSize":128,"maxZoom":-1}', '{'])
def test_invalid_metadata_does_not_enable_the_tab(tmp_path: Path, metadata: str) -> None:
    app = _app(tmp_path)
    root = _tiles(app)
    (root / "mapinfo.json").write_text(metadata)
    with pytest.raises(ValueError):
        SevenDaysMap.load(save=root.parent, world_id="test")
    assert not app.supports_map


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
        assert app.supports_map
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
        assert not app.supports_map
        tile.write_bytes(_png())
        assert not app.supports_map
        assert discover.call_count == 1
        clock.return_value += 3
        assert app.supports_map
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
            assert not app.supports_map
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


@pytest.mark.parametrize("tile_name", ["2147483648_0.png", "0_-2147483649.png", "999999999999999999999_0.png"])
def test_out_of_range_tile_indexes_are_rejected(tmp_path: Path, tile_name: str) -> None:
    app = _app(tmp_path)
    _tiles(app)
    world_id = _service().build_manifest(app).initial_world_name
    with pytest.raises(HTTPException) as failure:
        _service().tile_response(app, world_id, 4, tile_name)
    assert failure.value.status_code == 400
