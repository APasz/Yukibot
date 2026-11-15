from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, AsyncIterator
from dataclasses import replace
from io import BytesIO, BufferedReader
import hashlib
import os
from pathlib import Path
import stat
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, MagicMock
import zipfile

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
import py7zr
import pytest
from aiohttp import web

import config
from _file import MOD_UPLOAD_STAGING_PREFIX
from apps._app import App
from apps._config import App_Config, ModPlacement
from apps._mod import Mod, Mod_Manager
from apps._mod_metadata import ModIdentity, ModIdentityKind
from apps.sevendays import Mod_7D2D
from apps.sevendays.mod_upload import prepare_mod_archive
from archive_safety import ZipArchiveLimits
from checks.rar_helpers import RAR_FIXTURES, UNRAR_AVAILABLE, stored_rar
from node_api.mod import NodeModUploadPreview
from node_api.mod_routes import register_mod_routes
from node_api.mod_service import NodeModService
from node_api.upload import NodeApiRequestBodyLimitMiddleware
from web_dash.mod_uploads import mod_upload_error_detail, show_mod_upload_preview
from web_dash.nicegui_protocols import ModWebUi


def _info(name: str) -> str:
    return f'<xml><Name value="{name}"/><DisplayName value="{name}"/><Version value="1.0"/></xml>'


def _zip(names: tuple[str, ...] = ("First", "Second"), *, windows: bool = False) -> BytesIO:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in names:
            prefix = f"Wrapper/Mods/{name}/"
            if windows:
                prefix = prefix.replace("/", "\\")
            archive.writestr(prefix + "ModInfo.xml", _info(name))
            archive.writestr(prefix + "Resources/asset.unity3d", b"asset data")
    stream.seek(0)
    return stream


def _rar(stream: BytesIO | None = None) -> BytesIO:
    with zipfile.ZipFile(_zip() if stream is None else stream) as archive:
        return stored_rar(tuple((member.filename, archive.read(member)) for member in archive.infolist()))


@pytest.fixture
def harness(tmp_path: Path) -> tuple[NodeModService, App, Mod_Manager]:
    app_directory = tmp_path / "app"
    mods_directory = app_directory / "Mods"
    mods_directory.mkdir(parents=True)
    cfg = App_Config(
        name="sevendays_upload", instance_key="upload", friendly_name="7D2D Upload",
        directory=app_directory, apps_dir=tmp_path, mods_dir=mods_directory,
        scope="sevendays", join_host="127.0.0.1",
    )
    manager = Mod_Manager(cfg, mod_cls=Mod_7D2D, db_path=tmp_path / "mods.jsonl")
    app = cast(App, SimpleNamespace(
        name=cfg.name, friendly=cfg.friendly_name, has_mod_manager=manager, mods=manager,
        check_running=lambda: False,
    ))
    service = NodeModService(
        node_name=lambda: "test", require_acl=Mock(), build_runtime_summary=AsyncMock(),
        invalidate_client_pack_content=Mock(), invalidate_mod_inventory=Mock(),
        upload_mod_paths=AsyncMock(), operations=Mock(),
    )
    return service, app, manager


def _mod_api(service: NodeModService, app_model: App) -> FastAPI:
    auth = SimpleNamespace(node_name="test", require_access=lambda *_, **__: object(), require_actor=lambda _: SimpleNamespace(require_actor_user_id=lambda: 42))
    api = FastAPI()
    register_mod_routes(api, auth=auth, resolve_app=lambda _: app_model, mod_service=service, operation_api=Mock(), client_packs=Mock(), api_prefix="/api/node", traffic_log=Mock())  # type: ignore[arg-type]
    return api


def test_bundle_detects_wrappers_and_windows_paths(tmp_path: Path) -> None:
    prepared = prepare_mod_archive(
        _zip(windows=True), "pack.zip", tmp_path, limits=ZipArchiveLimits(),
        cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root,
    )
    assert tuple(component.name for component in prepared.components) == ("First", "Second")
    assert prepared.file_count == 4
    assert all((component.directory / "Resources/asset.unity3d").read_bytes() == b"asset data" for component in prepared.components)


@pytest.mark.parametrize("member", ["../outside", "C:\\outside", "Wrapper/Mods/First/modinfo.XML", "Wrapper/Mods/First/Resources"])
def test_archive_rejects_unsafe_and_conflicting_paths(tmp_path: Path, member: str) -> None:
    stream = _zip()
    with zipfile.ZipFile(stream, "a") as archive:
        archive.writestr(member, b"bad")
    with pytest.raises(ValueError):
        prepare_mod_archive(stream, "pack.zip", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert not list(tmp_path.iterdir())


def test_symlinks_and_nested_components_are_rejected(tmp_path: Path) -> None:
    stream = _zip()
    with zipfile.ZipFile(stream, "a") as archive:
        member = zipfile.ZipInfo("Wrapper/Mods/First/link")
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, "../../outside")
    with pytest.raises(ValueError, match="symbolic links"):
        prepare_mod_archive(stream, "pack.zip", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)


def test_encrypted_zip_is_rejected_before_extraction(tmp_path: Path) -> None:
    contents = bytearray(_zip().getvalue())
    central_header = contents.index(b"PK\x01\x02")
    contents[central_header + 8] |= 1
    with pytest.raises(ValueError, match="encrypted"):
        prepare_mod_archive(BytesIO(contents), "pack.zip", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("compression", [zipfile.ZIP_DEFLATED, 99])
def test_corrupt_or_unsupported_zip_returns_a_readable_error(tmp_path: Path, compression: int) -> None:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("First/ModInfo.xml", _info("First"))
    contents = bytearray(stream.getvalue())
    if compression == zipfile.ZIP_DEFLATED:
        data_offset = 30 + int.from_bytes(contents[26:28], "little") + int.from_bytes(contents[28:30], "little")
        contents[data_offset] = 7  # Reserved DEFLATE block type.
    else:
        central_header = contents.index(b"PK\x01\x02")
        contents[8:10] = compression.to_bytes(2, "little")
        contents[central_header + 10:central_header + 12] = compression.to_bytes(2, "little")
    with pytest.raises(ValueError, match="damaged|unsupported"):
        prepare_mod_archive(BytesIO(contents), "pack.zip", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)


def test_7z_bundle_has_the_same_component_validation(tmp_path: Path) -> None:
    stream = BytesIO()
    with py7zr.SevenZipFile(stream, "w") as archive:
        archive.writestr(_info("First"), "Mods/First/ModInfo.xml")
        archive.writestr(b"asset", "Mods/First/Resources/asset.unity3d")
    stream.seek(0)
    prepared = prepare_mod_archive(stream, "pack.7z", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert tuple(component.name for component in prepared.components) == ("First",)
    assert (prepared.components[0].directory / "Resources/asset.unity3d").read_bytes() == b"asset"


def test_rar_bundle_detects_wrappers_and_windows_paths(tmp_path: Path) -> None:
    prepared = prepare_mod_archive(_rar(_zip(windows=True)), "pack.RAR", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert tuple(component.name for component in prepared.components) == ("First", "Second")
    assert prepared.file_count == 4
    assert all((component.directory / "Resources/asset.unity3d").read_bytes() == b"asset data" for component in prepared.components)
    assert not list(tmp_path.glob("yukibot-rar-*"))


@pytest.mark.skipif(not UNRAR_AVAILABLE, reason="Compressed RAR checks require unrar")
@pytest.mark.parametrize("filename", ["bundle-rar4.rar", "bundle-rar5.rar", "bundle-solid-rar5.rar"])
def test_compressed_rar_bundles_extract_all_components(tmp_path: Path, filename: str) -> None:
    prepared = prepare_mod_archive(BytesIO((RAR_FIXTURES / filename).read_bytes()), filename, tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert tuple(component.name for component in prepared.components) == ("First", "Second")
    assert prepared.file_count == 4
    assert all((component.directory / "Resources/asset.unity3d").read_bytes() == b"asset data" * 64 for component in prepared.components)


@pytest.mark.parametrize("member", ["../outside", "C:\\outside", "Wrapper/Mods/First/modinfo.XML", "Wrapper/Mods/First/Resources", "Wrapper/Mods/First/Nested/ModInfo.xml"])
def test_rar_bundle_rejects_unsafe_or_conflicting_layouts(tmp_path: Path, member: str) -> None:
    stream = _zip()
    with zipfile.ZipFile(stream, "a") as archive:
        archive.writestr(member, _info("Nested"))
    with pytest.raises(ValueError):
        prepare_mod_archive(_rar(stream), "pack.rar", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("filename", ["encrypted-data.rar", "encrypted-headers.rar", "symlink-rar5.rar", "hardlink-rar5.rar", "split.part1.rar"])
def test_rar_bundle_rejects_unsupported_archives_before_extraction(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError):
        prepare_mod_archive(BytesIO((RAR_FIXTURES / filename).read_bytes()), filename, tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert not list(tmp_path.iterdir())


def test_rar_bundle_preview_and_selected_install(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_rar(), filename="pack.rar")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        assert tuple(component.name for component in preview.components) == ("First", "Second")
        await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("Second",))
        assert not (manager.folder / "First").exists()
        assert (manager.folder / "Second/Resources/asset.unity3d").read_bytes() == b"asset data"
        assert not service._staged_mod_uploads

    asyncio.run(run())


@pytest.mark.skipif(not UNRAR_AVAILABLE, reason="Compressed RAR checks require unrar")
def test_solid_rar_multipart_upload_is_prepared_from_its_spool(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness
    with TestClient(_mod_api(service, app)) as client:
        response = client.post(f"/api/node/apps/{app.name}/mods/upload/inspect", files={"upload": ("pack.rar", (RAR_FIXTURES / "bundle-solid-rar5.rar").read_bytes())})
        assert response.status_code == 200, response.text
        preview = NodeModUploadPreview.model_validate_json(response.content)
        assert tuple(component.name for component in preview.components) == ("First", "Second")
        discarded = client.post(f"/api/node/apps/{app.name}/mods/upload/{preview.upload_id}/discard")
        assert discarded.status_code == 200
    assert not manager.index
    assert not service._staged_mod_uploads


def test_existing_and_builtin_components_are_skipped_and_selected_new_mods_install(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness
    existing = manager.folder / "First"
    existing.mkdir()
    (existing / "ModInfo.xml").write_text(_info("First"))

    async def run() -> None:
        preview = await service.inspect_mod_uploads(
            app=app, uploads=[UploadFile(_zip(("First", "Second", "0_TFP_Harmony")), filename="pack.zip")],
            upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED,
        )
        assert {component.name: component.status for component in preview.components} == {"First": "existing", "Second": "new", "0_TFP_Harmony": "protected"}
        assert not (manager.folder / "Second").exists()
        with pytest.raises(HTTPException, match="already installed"):
            await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("First",))
        result = await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("Second",))
        assert tuple(mod.name for mod in result.mods) == ("Second",)
        assert (manager.folder / "Second/Resources/asset.unity3d").read_bytes() == b"asset data"
        assert not list(manager.folder.glob(f"{MOD_UPLOAD_STAGING_PREFIX}*"))

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failure_after_registration_restores_files_index_and_database(
    harness: tuple[NodeModService, App, Mod_Manager], monkeypatch: pytest.MonkeyPatch, cancelled: bool,
) -> None:
    service, app, manager = harness

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        original_database = manager.db_path.read_bytes()
        metadata = manager.shared_metadata.snapshot()
        register = manager._register_uploaded_mods

        async def fail_after_registration(mods: tuple[Mod, ...]) -> None:
            await register(mods)
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("late registration failure")

        monkeypatch.setattr(manager, "_register_uploaded_mods", fail_after_registration)
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("First", "Second"))
        assert not (manager.folder / "First").exists()
        assert not (manager.folder / "Second").exists()
        assert not manager.index
        assert manager.db_path.read_bytes() == original_database
        assert manager.shared_metadata.snapshot() == metadata
        staged = service._staged_mod_uploads[preview.upload_id]
        assert all(component.directory.exists() for component in staged.components)
        await service.discard_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id)

    asyncio.run(run())


def test_preview_is_bound_to_actor_and_expires(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        with pytest.raises(HTTPException) as error:
            await service.apply_mod_upload(app=app, actor_user_id=43, upload_id=preview.upload_id, names=("First",))
        assert error.value.status_code == 404
        staged = service._staged_mod_uploads[preview.upload_id]
        service._staged_mod_uploads[preview.upload_id] = replace(staged, expires_at=0)
        await service.cleanup_mod_uploads((app,))
        assert not staged.directory.exists()
        assert not manager.index

    asyncio.run(run())


def test_mod_and_save_request_limits_are_separate() -> None:
    app = FastAPI()
    app.add_middleware(NodeApiRequestBodyLimitMiddleware, max_bytes=3, mod_max_bytes=8, api_prefix="/api/node")

    @app.post("/api/node/apps/example/mods/upload")
    @app.post("/api/node/apps/example/saves/upload")
    async def receive() -> dict[str, bool]:
        return {"accepted": True}

    with TestClient(app) as client:
        assert client.post("/api/node/apps/example/mods/upload", content=b"12345678").status_code == 200
        assert client.post("/api/node/apps/example/saves/upload", content=b"1234").status_code == 413
        assert client.post("/api/node/apps/example/mods/upload", headers={"Content-Length": str(8 + 1024**2 + 1)}).status_code == 413


def test_authentication_runs_before_multipart_parsing(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app_model, _manager = harness
    authenticated = False

    def require_access(*args: object, **kwargs: object) -> object:
        nonlocal authenticated
        authenticated = True
        return object()

    auth = SimpleNamespace(node_name="test", require_access=require_access, require_actor=lambda _: SimpleNamespace(require_actor_user_id=lambda: 42))
    app = FastAPI()
    register_mod_routes(app, auth=auth, resolve_app=lambda _: app_model, mod_service=service, operation_api=Mock(), client_packs=Mock(), api_prefix="/api/node", traffic_log=Mock())  # type: ignore[arg-type]

    # A streaming ASGI client avoids a client-side multipart encoder reading the file first.
    async def run() -> None:
        from httpx import ASGITransport, AsyncClient
        boundary = "audit-boundary"
        payload = _zip().getvalue()

        async def body() -> AsyncIterator[bytes]:
            assert authenticated
            yield f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; filename="pack.zip"\r\nContent-Type: application/zip\r\n\r\n'.encode()
            yield payload
            yield f"\r\n--{boundary}--\r\n".encode()

        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/node/apps/sevendays_upload/mods/upload/inspect", content=body(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            assert response.status_code == 200, response.text
            preview = response.json()
            await service.discard_mod_upload(app=app_model, actor_user_id=42, upload_id=preview["upload_id"])

    asyncio.run(run())


@pytest.mark.parametrize("running", [False, True])
def test_unavailable_uploads_are_rejected_without_receiving_files(
    harness: tuple[NodeModService, App, Mod_Manager], monkeypatch: pytest.MonkeyPatch, running: bool,
) -> None:
    from httpx import ASGITransport, AsyncClient

    service, app_model, _manager = harness
    if running:
        monkeypatch.setattr(app_model, "check_running", lambda: True)
    else:
        monkeypatch.setattr(app_model, "mods", None)

    async def run() -> None:
        received = False

        async def body() -> AsyncIterator[bytes]:
            nonlocal received
            received = True
            yield b"This transfer should not start"

        async with AsyncClient(transport=ASGITransport(_mod_api(service, app_model)), base_url="http://test") as client:
            response = await client.post(f"/api/node/apps/{app_model.name}/mods/upload/inspect", content=body())
        assert response.status_code == 409, response.text
        assert not received

    asyncio.run(run())


def test_archive_limits_apply_across_the_entire_batch(harness: tuple[NodeModService, App, Mod_Manager], monkeypatch: pytest.MonkeyPatch) -> None:
    service, app, manager = harness
    monkeypatch.setattr(config, "NODE_API_MOD_ARCHIVE_LIMITS", ZipArchiveLimits(file_count=3))

    async def run() -> None:
        with pytest.raises(ValueError, match="file count"):
            await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(("First",)), filename="first.zip"), UploadFile(_zip(("Second",)), filename="second.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        assert not service._staged_mod_uploads
        assert not list(manager.folder.glob(f"{MOD_UPLOAD_STAGING_PREFIX}*"))

    asyncio.run(run())


def test_conflicts_added_after_preview_block_apply(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.CLIENT_ONLY)
        conflict = manager.folder / "First"
        conflict.mkdir()
        (conflict / "ModInfo.xml").write_text(_info("First"))
        with pytest.raises(HTTPException) as error:
            await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("First", "Second"))
        assert error.value.status_code == 409
        assert not (manager.folder / "Second").exists()
        await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("Second",))
        assert (manager.folder / "Second/ModInfo.xml.client").is_file()

    asyncio.run(run())


def test_expired_orphans_are_cleaned_without_touching_active_uploads(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness
    staging = service._mod_upload_staging_root(manager, create=True)
    orphan = staging / f"{MOD_UPLOAD_STAGING_PREFIX}orphan"
    orphan.mkdir()
    os.utime(orphan, (0, 0))
    unrelated = manager.folder / "unrelated"
    unrelated.mkdir()
    os.utime(unrelated, (0, 0))
    asyncio.run(service.cleanup_mod_uploads((app,)))
    assert not orphan.exists()
    assert unrelated.exists()


def test_native_identity_detects_an_installed_mod_with_a_different_folder_name(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness
    existing = manager.folder / "old-host-filename"
    existing.mkdir()
    (existing / "ModInfo.xml").write_text(_info("First"))

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        assert next(component for component in preview.components if component.name == "First").status == "existing"
        await service.discard_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id)

    asyncio.run(run())


def test_insufficient_extraction_space_rejects_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("_file.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="free disk space"):
        prepare_mod_archive(_zip(), "pack.zip", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert not list(tmp_path.iterdir())


def test_insufficient_extraction_space_returns_507(
    harness: tuple[NodeModService, App, Mod_Manager], monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, app, _manager = harness
    monkeypatch.setattr("_file.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with TestClient(_mod_api(service, app)) as client:
        response = client.post(f"/api/node/apps/{app.name}/mods/upload/inspect", files={"upload": ("pack.zip", _zip())})
    assert response.status_code == 507, response.text
    assert "free disk space" in response.json()["detail"]
    assert not service._staged_mod_uploads


def test_modinfo_size_is_bounded_before_parsing(tmp_path: Path) -> None:
    from apps.sevendays import MOD_INFO_MAX_BYTES
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("ModInfo.xml", b"x" * (MOD_INFO_MAX_BYTES + 1))
    with pytest.raises(ValueError, match="metadata exceeds"):
        prepare_mod_archive(stream, "pack.zip", tmp_path, limits=ZipArchiveLimits(), cancelled=Event(), validate_root=Mod_7D2D._validate_archive_root)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("response,expected", [('{"detail":"Too large"}', "Too large"), ("No space", "No space"), ("<html>Error</html>", ""), ('{"detail":[{"error":"bad"}]}', "")])
def test_upload_rejections_have_readable_details(response: str, expected: str) -> None:
    assert mod_upload_error_detail(response) == expected


def test_preview_selects_only_new_components(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness
    existing = manager.folder / "First"
    existing.mkdir()
    (existing / "ModInfo.xml").write_text(_info("First"))

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(("First", "Second", "0_TFP_Harmony")), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        ui = MagicMock()
        buttons: dict[str, Callable[[], Awaitable[None]]] = {}
        checkboxes: dict[str, Mock] = {}

        def button(label: str, *, on_click: Callable[[], Awaitable[None]]) -> MagicMock:
            buttons[label] = on_click
            return MagicMock()

        def checkbox(label: str, *, value: bool) -> Mock:
            result = Mock(value=value)
            checkboxes[label] = result
            return result

        ui.button.side_effect = button
        ui.checkbox.side_effect = checkbox
        apply = AsyncMock()
        discard = AsyncMock()
        show_mod_upload_preview(ui=cast(ModWebUi, ui), preview=preview, apply=apply, discard=discard)
        checkboxes["First"].disable.assert_called_once()
        checkboxes["0_TFP_Harmony"].disable.assert_called_once()
        await buttons["Install selected"]()
        apply.assert_awaited_once_with(("Second",))
        await service.discard_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id)

    asyncio.run(run())


def test_single_component_api_rejects_bundles_before_installation(harness: tuple[NodeModService, App, Mod_Manager]) -> None:
    service, app, manager = harness

    async def run() -> None:
        with pytest.raises(HTTPException) as error:
            await service.upload_mod_file(app=app, upload=UploadFile(_zip(), filename="pack.zip"), upload_name=None, actor_user_id=42)
        assert error.value.status_code == 400
        assert not manager.index
        assert not list(manager.folder.iterdir())
        assert not service._staged_mod_uploads

    asyncio.run(run())


def test_post_commit_cleanup_failure_does_not_report_installation_failure(
    harness: tuple[NodeModService, App, Mod_Manager], monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, app, manager = harness

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(("First",)), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        staged = service._staged_mod_uploads[preview.upload_id]
        with monkeypatch.context() as patch:
            patch.setattr("node_api.mod_service.shutil.rmtree", Mock(side_effect=OSError("cleanup failed")))
            result = await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("First",))
        assert result.mods[0].name == "First"
        assert (manager.folder / "First/ModInfo.xml").is_file()
        os.utime(staged.directory, (0, 0))
        await service.cleanup_mod_uploads((app,))
        assert not staged.directory.exists()

    asyncio.run(run())


def test_cleanup_failure_does_not_mask_invalid_archive_or_block_new_previews(
    harness: tuple[NodeModService, App, Mod_Manager], monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, app, _manager = harness

    async def run() -> None:
        with monkeypatch.context() as patch:
            patch.setattr("node_api.mod_service.shutil.rmtree", Mock(side_effect=PermissionError("cleanup failed")))
            with pytest.raises(ValueError, match="ZIP, 7z, or RAR"):
                await service.inspect_mod_uploads(app=app, uploads=[UploadFile(BytesIO(b"invalid"), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
            preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
            expired = service._staged_mod_uploads[preview.upload_id]
            service._staged_mod_uploads[preview.upload_id] = replace(expired, expires_at=0)
            replacement = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
            assert replacement.upload_id != preview.upload_id
        await service.discard_mod_upload(app=app, actor_user_id=42, upload_id=replacement.upload_id)

    asyncio.run(run())


def test_failed_batch_preserves_unrelated_shared_metadata_edits(
    harness: tuple[NodeModService, App, Mod_Manager], monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, app, manager = harness
    first_identity = ModIdentity(kind=ModIdentityKind.NATIVE, namespace=manager.scope, value="First")
    other_identity = ModIdentity(kind=ModIdentityKind.NATIVE, namespace=manager.scope, value="Unrelated")
    original = manager.shared_metadata.set(identities=(first_identity,), friendly_name="Original", classification_override=None, mod_pages=())
    manager.shared_metadata.save_if_dirty()

    async def run() -> None:
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        register = manager._register_uploaded_mods

        async def fail_after_another_instance_edits_metadata(mods: tuple[Mod, ...]) -> None:
            await register(mods)
            manager.shared_metadata.set(identities=(other_identity,), friendly_name="Keep this change", classification_override=None, mod_pages=())
            manager.shared_metadata.save_if_dirty()
            raise RuntimeError("late registration failure")

        monkeypatch.setattr(manager, "_register_uploaded_mods", fail_after_another_instance_edits_metadata)
        with pytest.raises(RuntimeError, match="late registration failure"):
            await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("First", "Second"))
        assert manager.shared_metadata.resolve((first_identity,)) == original
        other = manager.shared_metadata.resolve((other_identity,))
        assert other is not None and other.friendly_name == "Keep this change"
        assert "Keep this change" in manager.shared_metadata.path.read_text()
        assert not manager.index
        await service.discard_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id)

    asyncio.run(run())


def test_portal_forwarding_streams_files_without_whole_file_reads(harness: tuple[NodeModService, App, Mod_Manager], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from web_dash.service import ModWebService
    from web_dash.types import ModWebNodeLink
    from mod_web_auth import ModWebUser

    service, app, _manager = harness
    upload_path = tmp_path / "large.jar"
    payload = bytes(range(256)) * 20_000
    upload_path.write_bytes(payload)
    expected_hash = hashlib.sha256(payload).hexdigest()
    original_open = Path.open
    chunks_received = 0

    class GuardedReader(BufferedReader):
        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 1024**2, "The forwarding client tried to buffer the whole file"
            return super().read(size)

    def open_guarded(pointer: Path, mode: str = "r", *args: object, **kwargs: object):
        if pointer == upload_path and mode == "rb":
            return GuardedReader(original_open(pointer, "rb", buffering=0))
        return original_open(pointer, mode, *args, **kwargs)

    async def run() -> None:
        nonlocal chunks_received
        preview = await service.inspect_mod_uploads(app=app, uploads=[UploadFile(_zip(("First",)), filename="pack.zip")], upload_names=None, actor_user_id=42, placement=ModPlacement.SERVER_ENABLED)
        installed = await service.apply_mod_upload(app=app, actor_user_id=42, upload_id=preview.upload_id, names=("First",))

        async def receive(request: web.Request) -> web.Response:
            nonlocal chunks_received
            assert request.headers["Authorization"] == "Bearer upload-test"
            reader = await request.multipart()
            digest = hashlib.sha256()
            async for part in reader:
                if part.name == "upload":
                    while chunk := await part.read_chunk(size=64 * 1024):
                        chunks_received += 1
                        digest.update(chunk)
            assert digest.hexdigest() == expected_hash
            return web.json_response(installed.to_mapping())

        server = web.Application()
        server.router.add_post("/apps/sevendays_upload/mods/upload", receive)
        runner = web.AppRunner(server)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        portal = ModWebService()
        monkeypatch.setattr(portal, "_remote_token", lambda **_: "upload-test")
        monkeypatch.setattr(Path, "open", open_guarded)
        try:
            result = await portal._remote_mod_uploads_async(
                cast(ModWebNodeLink, SimpleNamespace(api_base_url=f"http://127.0.0.1:{port}")),
                app.name, ((upload_path.name, upload_path),),
                ModWebUser(discord_id=42, username="tester", global_name=None, avatar_hash=None),
            )
            assert result.mods[0].name == "First"
            assert chunks_received > 1
        finally:
            if portal._remote_http_session is not None:
                await portal._remote_http_session.close()
            await runner.cleanup()

    asyncio.run(run())
