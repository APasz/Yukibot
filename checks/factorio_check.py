import asyncio
import hashlib
import json
import tarfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from modmux.models import ModID, Provider

from apps._config import (
    App_Config,
    AppVersion,
    FactorioUpdateBranch,
    FactorioUpdateConfig,
    KnownModPageProvider,
    Mod_Config,
)
from apps._console import ConsoleResponseSource, execute_console_action
from apps._mod import Mod_Manager
from apps._updater import AppUpdateOperationKind, AppUpdateProviderKind, AppUpdateState
from apps.factorio import (
    Factorio,
    Factorio_Updater,
    FactorioModPortalCredentials,
    Matchers,
    Mod_Factorio,
    _ensure_factorio_binary_executable,
    _factorio_download_archive_path,
    _factorio_latest_headless_versions,
    _factorio_mod_portal_release_from_mapping,
    _select_factorio_mod_portal_release,
    detect_factorio_version,
    download_factorio_mod_from_portal,
    ensure_factorio_config_files,
    factorio_config_path,
    factorio_mod_portal_credentials_from_server_settings,
    factorio_mod_settings_path,
    parse_factorio_mod_portal_url,
    resolve_factorio_mod_portal_candidates,
)


class _FakeFactorioUpdateApp:
    def __init__(self, temp_path: Path) -> None:
        self.friendly = "Factorio Alpha"
        self.directory = temp_path
        self.dir_log = temp_path / "logs"
        self.dir_log.mkdir(parents=True, exist_ok=True)
        self.mods = None
        self.cfg = App_Config(
            name="factorio_alpha",
            instance_key="alpha",
            friendly_name=self.friendly,
            directory=temp_path,
            apps_dir=temp_path,
            scope="factorio",
        )
        self.persisted = 0
        self.applied_versions: list[AppVersion | str | None] = []
        self.running = False

    def persist_instance_config_overrides(self) -> None:
        self.persisted += 1

    def apply_version(self, version: AppVersion | str | None, *, persist: bool) -> bool:
        self.applied_versions.append(version)
        return bool(persist or version is not None)

    def check_running(self) -> bool:
        return self.running


class FactorioVersionDetectionTests(unittest.TestCase):
    def test_detect_description_reads_info_json_from_factorio_archive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            mods_dir = Path(temp_dir)
            archive_path = mods_dir / "example_1.0.0.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "example_1.0.0/info.json",
                    json.dumps(
                        {
                            "name": "example",
                            "version": "1.0.0",
                            "title": "Example Mod",
                            "description": "Adds example logistics helpers.",
                        }
                    ),
                )
            mod = Mod_Factorio(Mod_Config(name=archive_path.name, directory=mods_dir))

            mod.sync_metadata()

        self.assertEqual(mod.description, "Adds example logistics helpers.")

    def test_delete_save_file_removes_save_archive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            saves_dir = root / "saves"
            save_path = saves_dir / "alpha.zip"
            saves_dir.mkdir()
            save_path.write_bytes(b"save-data")
            app = cast(Any, object.__new__(Factorio))
            app.directory = root
            app.check_running = lambda: False

            deleted = app.delete_save_file(file_id="saves/alpha.zip")

            self.assertEqual(deleted.id, "saves/alpha.zip")
            self.assertFalse(save_path.exists())

    def test_parse_factorio_mod_portal_url_accepts_canonical_mod_page(self) -> None:
        mod_id = parse_factorio_mod_portal_url("https://mods.factorio.com/mod/invincible-construction-bots")

        self.assertEqual(mod_id, "invincible-construction-bots")

    def test_parse_factorio_mod_portal_url_rejects_non_portal_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "mods.factorio.com"):
            parse_factorio_mod_portal_url("https://example.com/mod/invincible-construction-bots")

    def test_select_factorio_mod_portal_release_prefers_matching_major_minor(self) -> None:
        old_release = _factorio_mod_portal_release_from_mapping(
            {
                "download_url": "/download/example/1.0.0",
                "file_name": "example_1.0.0.zip",
                "version": "1.0.0",
                "sha1": "0" * 40,
                "released_at": "2026-01-01T00:00:00.000000Z",
                "info_json": {"factorio_version": "1.1"},
            }
        )
        matching_release = _factorio_mod_portal_release_from_mapping(
            {
                "download_url": "/download/example/2.0.0",
                "file_name": "example_2.0.0.zip",
                "version": "2.0.0",
                "sha1": "1" * 40,
                "released_at": "2025-01-01T00:00:00.000000Z",
                "info_json": {"factorio_version": "2.0"},
            }
        )

        selected = _select_factorio_mod_portal_release(
            (old_release, matching_release),
            factorio_version=AppVersion(main="2.0.68"),
        )

        self.assertEqual(selected.file_name, "example_2.0.0.zip")

    def test_factorio_mod_portal_release_extracts_required_dependencies(self) -> None:
        release = _factorio_mod_portal_release_from_mapping(
            {
                "download_url": "/download/example/1.0.0",
                "file_name": "example_1.0.0.zip",
                "version": "1.0.0",
                "sha1": "0" * 40,
                "released_at": "2026-01-01T00:00:00.000000Z",
                "info_json": {
                    "factorio_version": "2.0",
                    "dependencies": [
                        "base >= 2.0.0",
                        "required-lib >= 1.0.0",
                        "? optional-lib",
                        "! incompatible-lib",
                        "~ hidden-required-lib >= 1.0.0",
                        "required-lib >= 1.0.0",
                    ],
                },
            }
        )

        self.assertEqual(release.dependencies, ("required-lib", "hidden-required-lib"))

    def test_factorio_mod_portal_resolution_includes_required_dependencies(self) -> None:
        def release_payload(mod_id: str, dependencies: list[str]) -> dict[str, object]:
            return {
                "download_url": f"/download/{mod_id}/1.0.0",
                "file_name": f"{mod_id}_1.0.0.zip",
                "version": "1.0.0",
                "sha1": "0" * 40,
                "released_at": "2026-01-01T00:00:00.000000Z",
                "info_json": {"factorio_version": "2.0", "dependencies": dependencies},
            }

        class _FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.status = 200
                self._payload = payload

            async def __aenter__(self) -> "_FakeResponse":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def json(self) -> dict[str, object]:
                return self._payload

        class _FakeSession:
            async def __aenter__(self) -> "_FakeSession":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def get(self, url: str, **_kwargs: object) -> _FakeResponse:
                mod_id = url.split("/api/mods/", maxsplit=1)[1].removesuffix("/full")
                root_payload: dict[str, object] = {
                    "name": "root",
                    "title": "Root Mod",
                    "releases": [release_payload("root", ["dep-one"])],
                }
                dep_one_payload: dict[str, object] = {
                    "name": "dep-one",
                    "title": "Dependency One",
                    "releases": [release_payload("dep-one", ["dep-two"])],
                }
                dep_two_payload: dict[str, object] = {
                    "name": "dep-two",
                    "title": "Dependency Two",
                    "releases": [release_payload("dep-two", [])],
                }
                payloads: dict[str, dict[str, object]] = {
                    "root": root_payload,
                    "dep-one": dep_one_payload,
                    "dep-two": dep_two_payload,
                }
                return _FakeResponse(payloads[mod_id])

        with patch("apps.factorio.aiohttp.ClientSession", return_value=_FakeSession()):
            resolution = asyncio.run(
                resolve_factorio_mod_portal_candidates(
                    page_url="https://mods.factorio.com/mod/root",
                    factorio_version=AppVersion(main="2.0.68"),
                )
            )

        self.assertEqual(tuple(candidate.mod_id for candidate in resolution.candidates), ("root", "dep-one", "dep-two"))
        self.assertEqual(resolution.candidates[1].required_by, ("root",))
        self.assertEqual(resolution.candidates[2].required_by, ("dep-one",))

    def test_factorio_mod_portal_credentials_read_server_settings(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "server-settings.json"
            settings_path.write_text(
                json.dumps({"username": "user", "token": "token"}),
                encoding="utf-8",
            )

            credentials = factorio_mod_portal_credentials_from_server_settings(settings_path)

        self.assertEqual(credentials.username, "user")
        self.assertEqual(credentials.token, "token")

    def test_ensure_factorio_config_files_copies_missing_examples(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            data_dir = directory / "data"
            data_dir.mkdir()
            for filename in ("server-settings", "map-settings", "map-gen-settings"):
                (data_dir / f"{filename}.example.json").write_text(
                    json.dumps({"source": filename}),
                    encoding="utf-8",
                )

            copied = ensure_factorio_config_files(directory)

            self.assertEqual(
                tuple(path.name for path in copied),
                ("server-settings.json", "map-settings.json", "map-gen-settings.json"),
            )
            self.assertEqual(
                json.loads(factorio_config_path(directory, "map-gen-settings.json").read_text(encoding="utf-8")),
                {"source": "map-gen-settings"},
            )

    def test_ensure_factorio_config_files_does_not_overwrite_existing_config(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            data_dir = directory / "data"
            data_dir.mkdir()
            config_path = data_dir / "server-settings.json"
            config_path.write_text(json.dumps({"source": "custom"}), encoding="utf-8")
            (data_dir / "server-settings.example.json").write_text(
                json.dumps({"source": "example"}),
                encoding="utf-8",
            )

            copied = ensure_factorio_config_files(directory)

            self.assertNotIn(config_path, copied)
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), {"source": "custom"})

    def test_factorio_mod_settings_path_uses_mods_directory(self) -> None:
        self.assertEqual(
            factorio_mod_settings_path(Path("/srv/factorio")),
            Path("/srv/factorio/mods/mod-settings.dat"),
        )

    def test_factorio_exposes_raw_console_command_action(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.cfg = App_Config(
            name="factorio_alpha",
            instance_key="alpha",
            friendly_name="Factorio",
            directory=Path("."),
            apps_dir=Path("."),
            scope="factorio",
        )

        actions = app.console_actions

        self.assertEqual(tuple(action.key for action in actions), ("raw_command",))
        self.assertEqual(actions[0].label, "Run Command")
        self.assertEqual(actions[0].power_level.name, "sudo")

    def test_factorio_raw_console_command_sends_rcon_command(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.friendly = "Factorio"
        app.cfg = App_Config(
            name="factorio_alpha",
            instance_key="alpha",
            friendly_name="Factorio",
            directory=Path("."),
            apps_dir=Path("."),
            scope="factorio",
        )
        app.check_running = lambda: True
        app._relay = SimpleNamespace(send=AsyncMock(return_value="command output"))
        action = next(action for action in app.console_actions if action.key == "raw_command")

        result = asyncio.run(
            execute_console_action(
                app=app,
                is_running=app.check_running,
                action=action,
                raw_value="/help",
            )
        )

        app._relay.send.assert_awaited_once_with("/help")
        self.assertEqual(result.source, ConsoleResponseSource.RCON)
        self.assertEqual(result.text, "command output")

    def test_factorio_mod_portal_download_follows_archive_redirects(self) -> None:
        archive_bytes = b"factorio-mod-archive"
        expected_sha1 = hashlib.sha1(archive_bytes).hexdigest()
        get_calls: list[dict[str, object]] = []

        class _FakeContent:
            async def iter_chunked(self, _chunk_size: int):
                yield archive_bytes

        class _FakeResponse:
            def __init__(self, *, status: int, payload: dict[str, object] | None = None) -> None:
                self.status = status
                self._payload = payload
                self.content = _FakeContent()

            async def __aenter__(self) -> "_FakeResponse":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def json(self) -> dict[str, object]:
                if self._payload is None:
                    raise ValueError("No JSON payload")
                return self._payload

        class _FakeSession:
            async def __aenter__(self) -> "_FakeSession":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def get(self, url: str, **kwargs: object) -> _FakeResponse:
                get_calls.append({"url": url, **kwargs})
                if url.endswith("/api/mods/example/full"):
                    return _FakeResponse(
                        status=200,
                        payload={
                            "name": "example",
                            "releases": [
                                {
                                    "download_url": "/download/example/1.0.0",
                                    "file_name": "example_1.0.0.zip",
                                    "version": "1.0.0",
                                    "sha1": expected_sha1,
                                    "released_at": "2026-01-01T00:00:00.000000Z",
                                    "info_json": {"factorio_version": "2.0"},
                                },
                            ],
                        },
                    )
                return _FakeResponse(status=200)

        with TemporaryDirectory() as temp_dir:
            with patch("apps.factorio.aiohttp.ClientSession", return_value=_FakeSession()):
                result = asyncio.run(
                    download_factorio_mod_from_portal(
                        page_url="https://mods.factorio.com/mod/example",
                        destination_dir=Path(temp_dir),
                        factorio_version=AppVersion(main="2.0.68"),
                        credentials=FactorioModPortalCredentials(username="user", token="token"),
                    )
                )

        self.assertEqual(result.file_name, "example_1.0.0.zip")
        self.assertTrue(get_calls[-1]["allow_redirects"])

    def test_detect_factorio_version_from_local_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "factorio-current.log").write_text(
                "   0.000 2025-01-01 00:00:00; Factorio 1.1.107 (build 12345, linux64, headless)\n",
                encoding="utf-8",
            )

            version = detect_factorio_version(directory=root)

        self.assertEqual(version, AppVersion(main="1.1.107"))

    def test_manager_ignores_factorio_metadata_files_and_uses_mod_list_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_dir = root / "apps"
            app_dir = apps_dir / "factorio"
            mods_dir = app_dir / "mods"
            mods_dir.mkdir(parents=True)
            self._write_factorio_mod_archive(mods_dir / "example_1.0.0.zip", mod_name="example")
            (mods_dir / "mod-settings.dat").write_bytes(b"\x00")
            (mods_dir / "mod-list.json").write_text(
                json.dumps({"mods": [{"name": "example", "enabled": False}]}, indent=4),
                encoding="utf-8",
            )
            app_cfg = App_Config(
                name="factorio_alpha",
                instance_key="alpha",
                friendly_name="Factorio",
                directory=app_dir,
                apps_dir=apps_dir,
                mods_dir=mods_dir,
                join_host="127.0.0.1",
                scope="factorio",
            )
            Mod_Manager._instances.clear()
            manager = Mod_Manager(app_cfg, mod_cls=Mod_Factorio, db_path=root / "mods.jsonl")

            try:
                asyncio.run(manager.reload_mods())
            finally:
                Mod_Manager._instances.clear()

            self.assertEqual(manager.list_names(), ["example_1.0.0.zip"])
            self.assertFalse(manager.get("example_1.0.0.zip").cfg.enabled)
            self.assertEqual(manager.get("example_1.0.0.zip").cfg.version, "1.0.0")

    def test_toggle_updates_factorio_mod_list_without_renaming_archive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mods_dir = root / "mods"
            mods_dir.mkdir()
            archive_path = mods_dir / "example_1.0.0.zip"
            self._write_factorio_mod_archive(archive_path, mod_name="example")
            mod_list_path = mods_dir / "mod-list.json"
            mod_list_path.write_text(
                json.dumps({"mods": [{"name": "example", "enabled": True}]}, indent=4),
                encoding="utf-8",
            )
            mod = Mod_Factorio(Mod_Config(name=archive_path.name, directory=mods_dir))

            asyncio.run(mod.disable())

            payload = json.loads(mod_list_path.read_text(encoding="utf-8"))
            self.assertTrue(archive_path.exists())
            self.assertFalse((mods_dir / "example_1.0.0.disabled").exists())
            self.assertEqual(payload["mods"], [{"name": "example", "enabled": False}])
            self.assertFalse(mod.cfg.enabled)
            self.assertEqual(mod.detect_version(), "1.0.0")

    def test_detects_human_friendly_name_from_mod_id(self) -> None:
        mod = Mod_Factorio(Mod_Config(name="circuit-network-selector-wire-icons_1.0.0.zip", directory=Path(".")))

        mod.sync_metadata()

        self.assertEqual(mod.friendly, "Circuit Network Selector Wire Icons")

    def test_detects_title_version_and_mod_page_from_info_json(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp)
            archive_path = mods_dir / "example_9.9.9.zip"
            self._write_factorio_mod_archive(
                archive_path,
                mod_name="example",
                version="2.3.4",
                title="Example Mod",
                homepage="https://mods.factorio.com/mod/example?from=info",
            )
            mod = Mod_Factorio(Mod_Config(name=archive_path.name, directory=mods_dir))

            mod.sync_metadata()

        self.assertEqual(mod.version, "2.3.4")
        self.assertEqual(mod.friendly, "Example Mod")
        self.assertEqual(len(mod.cfg.mod_pages), 1)
        self.assertEqual(mod.cfg.mod_pages[0].name, KnownModPageProvider.FACTORIO_MODS.value)
        self.assertEqual(mod.cfg.mod_pages[0].url, "https://mods.factorio.com/mod/example")

    def test_uses_modmux_when_info_homepage_is_not_a_factorio_mod_page(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp)
            archive_path = mods_dir / "example_1.0.0.zip"
            self._write_factorio_mod_archive(
                archive_path,
                mod_name="example",
                homepage="https://example.com/example",
            )
            mod = Mod_Factorio(Mod_Config(name=archive_path.name, directory=mods_dir))
            mod.sync_metadata()
            client = MagicMock()
            client.get_mod = AsyncMock(
                return_value=SimpleNamespace(
                    slug="resolved-example",
                    id=ModID(provider=Provider.WUBE, id="resolved-example"),
                )
            )
            muxer_context = MagicMock()
            muxer_context.__aenter__ = AsyncMock(return_value=client)
            muxer_context.__aexit__ = AsyncMock(return_value=None)

            with patch("apps.factorio.Muxer", return_value=muxer_context):
                asyncio.run(Mod_Factorio.sync_external_metadata_batch((mod,)))

        client.get_mod.assert_awaited_once_with(
            Provider.WUBE,
            ModID(provider=Provider.WUBE, id="example"),
            author_resolution=False,
        )
        self.assertEqual(
            mod.cfg.mod_pages[0].url,
            "https://mods.factorio.com/mod/resolved-example",
        )

    def test_reload_removes_stale_factorio_metadata_entries_from_db(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_dir = root / "apps"
            app_dir = apps_dir / "factorio"
            mods_dir = app_dir / "mods"
            mods_dir.mkdir(parents=True)
            self._write_factorio_mod_archive(mods_dir / "example_1.0.0.zip", mod_name="example")
            (mods_dir / "mod-settings.dat").write_bytes(b"\x00")
            (mods_dir / "mod-list.json").write_text(
                json.dumps({"mods": [{"name": "example", "enabled": True}]}, indent=4),
                encoding="utf-8",
            )
            db_path = root / "mods.jsonl"
            db_path.write_text(
                "\n".join(
                    (
                        json.dumps(
                            {
                                "name": "example_1.0.0.zip",
                                "directory": str(mods_dir),
                                "enabled": True,
                                "version": None,
                                "origin": "manual",
                                "coremod": False,
                                "download_block_reason": None,
                            }
                        ),
                        json.dumps(
                            {
                                "name": "mod-list.json",
                                "directory": str(mods_dir),
                                "enabled": True,
                                "version": None,
                                "origin": "manual",
                                "coremod": False,
                                "download_block_reason": None,
                            }
                        ),
                    )
                ),
                encoding="utf-8",
            )
            app_cfg = App_Config(
                name="factorio_alpha",
                instance_key="alpha",
                friendly_name="Factorio",
                directory=app_dir,
                apps_dir=apps_dir,
                mods_dir=mods_dir,
                join_host="127.0.0.1",
                scope="factorio",
            )
            Mod_Manager._instances.clear()
            manager = Mod_Manager(app_cfg, mod_cls=Mod_Factorio, db_path=db_path)

            try:
                asyncio.run(manager.reload_mods())
            finally:
                Mod_Manager._instances.clear()

            self.assertEqual(manager.list_names(), ["example_1.0.0.zip"])
            self.assertNotIn("mod-list.json", db_path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_factorio_mod_archive(
        pointer: Path,
        *,
        mod_name: str,
        version: str = "1.0.0",
        title: str | None = None,
        homepage: str | None = None,
    ) -> None:
        payload: dict[str, str] = {
            "name": mod_name,
            "version": version,
            "homepage": homepage or f"https://mods.factorio.com/mod/{mod_name}",
        }
        if title is not None:
            payload["title"] = title
        with zipfile.ZipFile(pointer, "w") as archive:
            archive.writestr(f"{mod_name}_{version}/info.json", json.dumps(payload))


class FactorioUpdaterTests(unittest.TestCase):
    def test_factorio_download_archive_path_uses_tmp_root_without_subdirectory(self) -> None:
        archive_path = _factorio_download_archive_path(
            tmp_dir=Path("/tmp/erinbot"),
            branch=FactorioUpdateBranch.EXPERIMENTAL,
            version_text="2.0.68",
        )

        self.assertEqual(archive_path, Path("/tmp/erinbot/factorio-experimental-2.0.68.tar.xz"))

    def test_factorio_latest_release_parser_reads_stable_and_experimental_versions(self) -> None:
        versions = _factorio_latest_headless_versions(
            {
                "stable": {"headless": "2.0.67"},
                "experimental": {"headless": "2.0.68"},
            }
        )

        self.assertEqual(versions[FactorioUpdateBranch.STABLE], (2, 0, 67))
        self.assertEqual(versions[FactorioUpdateBranch.EXPERIMENTAL], (2, 0, 68))

    def test_factorio_updater_info_defaults_to_stable_branch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeFactorioUpdateApp(Path(temp_dir))
            updater = Factorio_Updater(app, base=True)

            update_info = updater.info()

        self.assertEqual(update_info.provider_kind, AppUpdateProviderKind.FACTORIO)
        self.assertEqual(update_info.selected_branch_id, "stable")
        self.assertEqual(update_info.selected_branch_label, "Stable")
        self.assertEqual([branch.branch_id for branch in update_info.branches], ["stable", "experimental"])
        self.assertTrue(update_info.supports_verify)

    def test_factorio_updater_select_branch_persists_choice(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app = _FakeFactorioUpdateApp(Path(temp_dir))
            updater = Factorio_Updater(app, base=True)

            update_info = updater.select_branch("experimental")

        self.assertEqual(app.cfg.factorio_update, FactorioUpdateConfig(selected_branch=FactorioUpdateBranch.EXPERIMENTAL))
        self.assertEqual(app.persisted, 1)
        self.assertEqual(update_info.selected_branch_id, "experimental")
        self.assertEqual(update_info.selected_branch_label, "Experimental")

    def test_factorio_updater_update_selected_persists_installed_branch_and_version(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                app = _FakeFactorioUpdateApp(Path(temp_dir))
                app.cfg.factorio_update = FactorioUpdateConfig(selected_branch=FactorioUpdateBranch.EXPERIMENTAL)
                updater = Factorio_Updater(app, base=True)
                archive_path = app.directory / "factorio-experimental-2.0.68.tar.xz"
                archive_path.write_bytes(b"archive")
                with (
                    patch.object(
                        Factorio_Updater,
                        "fetch_latest_versions",
                        new=AsyncMock(
                            return_value={
                                FactorioUpdateBranch.STABLE: (2, 0, 67),
                                FactorioUpdateBranch.EXPERIMENTAL: (2, 0, 68),
                            }
                        ),
                    ),
                    patch.object(
                        Factorio_Updater,
                        "download_release",
                        new=AsyncMock(return_value=archive_path),
                    ),
                    patch.object(updater, "_install_release_archive") as install_mock,
                ):
                    result = await updater.update_selected()

                self.assertEqual(result.version_text, "2.0.68")
                self.assertEqual(
                    app.cfg.factorio_update,
                    FactorioUpdateConfig(
                        selected_branch=FactorioUpdateBranch.EXPERIMENTAL,
                        installed_branch=FactorioUpdateBranch.EXPERIMENTAL,
                    ),
                )
                self.assertEqual(app.applied_versions[-1], AppVersion(main="2.0.68"))
                self.assertEqual(app.persisted, 1)
                install_mock.assert_called_once_with(archive_path)
                self.assertEqual(updater.status().state, AppUpdateState.SUCCEEDED)

        asyncio.run(_run())

    def test_factorio_verify_selected_reinstalls_same_version(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temp_dir:
                app = _FakeFactorioUpdateApp(Path(temp_dir))
                app.cfg.factorio_update = FactorioUpdateConfig(selected_branch=FactorioUpdateBranch.EXPERIMENTAL)
                updater = Factorio_Updater(app, base=True)
                updater.version = (2, 0, 68)
                archive_path = app.directory / "factorio-experimental-2.0.68.tar.xz"
                archive_path.write_bytes(b"archive")
                with (
                    patch.object(
                        Factorio_Updater,
                        "fetch_latest_versions",
                        new=AsyncMock(
                            return_value={
                                FactorioUpdateBranch.STABLE: (2, 0, 67),
                                FactorioUpdateBranch.EXPERIMENTAL: (2, 0, 68),
                            }
                        ),
                    ),
                    patch.object(
                        Factorio_Updater,
                        "download_release",
                        new=AsyncMock(return_value=archive_path),
                    ) as download_mock,
                    patch.object(updater, "_install_release_archive") as install_mock,
                ):
                    result = await updater.verify_selected()

                self.assertEqual(result.kind, AppUpdateOperationKind.VERIFY)
                self.assertEqual(result.version_text, "2.0.68")
                self.assertIn("Verified", result.message)
                download_mock.assert_awaited_once_with(
                    branch=FactorioUpdateBranch.EXPERIMENTAL,
                    version_text="2.0.68",
                )
                install_mock.assert_called_once_with(archive_path)
                self.assertEqual(
                    app.cfg.factorio_update,
                    FactorioUpdateConfig(
                        selected_branch=FactorioUpdateBranch.EXPERIMENTAL,
                        installed_branch=FactorioUpdateBranch.EXPERIMENTAL,
                    ),
                )
                self.assertEqual(updater.status().state, AppUpdateState.SUCCEEDED)
                self.assertEqual(updater.status().operation_kind, AppUpdateOperationKind.VERIFY)

        asyncio.run(_run())

    def test_factorio_updater_reapplies_directory_ownership_after_install(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "bin").mkdir()
            (root / "bin" / "factorio").write_text("binary", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "server-settings.json").write_text("{}", encoding="utf-8")

            with (
                patch("apps.factorio._owner_group", return_value=("yuki", "games")),
                patch("apps.factorio.shutil.chown") as chown_mock,
            ):
                Factorio_Updater._apply_directory_ownership(root)

        expected_paths = Counter(
            {
                str(root): 1,
                str(root / "bin"): 1,
                str(root / "bin" / "factorio"): 1,
                str(root / "data"): 1,
                str(root / "data" / "server-settings.json"): 1,
            }
        )
        actual_paths = Counter(str(call.args[0]) for call in chown_mock.call_args_list)
        self.assertEqual(actual_paths, expected_paths)
        for call in chown_mock.call_args_list:
            self.assertEqual(call.kwargs["user"], "yuki")
            self.assertEqual(call.kwargs["group"], "games")

    def test_factorio_binary_permission_repair_restores_execute_bits(self) -> None:
        with TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "factorio"
            binary_path.write_text("binary", encoding="utf-8")
            binary_path.chmod(0o644)

            _ensure_factorio_binary_executable(binary_path)

            self.assertEqual(binary_path.stat().st_mode & 0o777, 0o755)

    def test_extract_archive_root_preserves_executable_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "factorio.tar.xz"
            source_root = root / "source"
            binary_path = source_root / "factorio" / "bin" / "x64" / "factorio"
            binary_path.parent.mkdir(parents=True)
            binary_path.write_text("binary", encoding="utf-8")
            binary_path.chmod(0o755)
            with tarfile.open(archive_path, mode="w:xz") as archive:
                archive.add(source_root / "factorio", arcname="factorio")
            staging_dir = root / "staging"
            staging_dir.mkdir()

            extracted_root = Factorio_Updater._extract_archive_root(archive_path, staging_dir)
            extracted_binary = extracted_root / "bin" / "x64" / "factorio"
            self.assertEqual(extracted_binary.stat().st_mode & 0o777, 0o755)

    def test_install_release_archive_repairs_binary_permissions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = _FakeFactorioUpdateApp(root)
            updater = Factorio_Updater(app, base=True)
            archive_path = root / "factorio.tar.xz"
            source_root = root / "source"
            source_binary = source_root / "factorio" / "bin" / "x64" / "factorio"
            source_binary.parent.mkdir(parents=True)
            source_binary.write_text("binary", encoding="utf-8")
            source_binary.chmod(0o644)
            with tarfile.open(archive_path, mode="w:xz") as archive:
                archive.add(source_root / "factorio", arcname="factorio")
            with (
                patch("apps.factorio._owner_group", return_value=("yuki", "games")),
                patch("apps.factorio.shutil.chown"),
            ):
                updater._install_release_archive(archive_path)

            installed_binary = root / "bin" / "x64" / "factorio"
            self.assertEqual(installed_binary.stat().st_mode & 0o777, 0o755)


class FactorioRelayMatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_error_records_factorio_startup_failure(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app._tail_machers = set()
        app._startup_error = None
        matcher = Matchers(app)

        await matcher.match_error(
            "   0.392 Error Util.cpp:81: Failed to load mod \"quality\": "
            "__quality__/prototypes/recycling.lua:48: attempt to perform arithmetic on field "
            "'default_icon_size' (a nil value)"
        )

        self.assertEqual(
            app._startup_error,
            "Util.cpp:81: Failed to load mod \"quality\": __quality__/prototypes/recycling.lua:48: "
            "attempt to perform arithmetic on field 'default_icon_size' (a nil value)",
        )

    async def test_match_error_records_factorio_command_line_failure(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app._tail_machers = set()
        app._startup_error = None
        matcher = Matchers(app)

        await matcher.match_error(
            "   0.250 Error CommandLineMultiplayer.cpp:183: require_user_verification must be enabled for public games."
        )

        self.assertEqual(
            app._startup_error,
            "CommandLineMultiplayer.cpp:183: require_user_verification must be enabled for public games.",
        )

    async def test_wait_for_startup_ready_raises_recorded_factorio_error(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app._startup_error = "CommandLineMultiplayer.cpp:183: require_user_verification must be enabled for public games."
        app.check_running = lambda: True
        tail = SimpleNamespace(stop=AsyncMock())
        app._tail = tail

        async def setup() -> None:
            await asyncio.sleep(60)

        app._relay = SimpleNamespace(setup=setup)

        with self.assertRaisesRegex(RuntimeError, "require_user_verification must be enabled"):
            await app._wait_for_startup_ready()

        tail.stop.assert_awaited_once()
        self.assertIsNone(app._tail)

    async def test_match_research_relays_finished_notice(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.factorio.DC_Relay.add") as add_mock:
            await matcher.match_research(
                "891.725 Script @__events-logger__/events/research.lua:23: [RESEARCH FINISHED] electronics 1"
            )

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Research: Electronics 1")
        self.assertIsNotNone(relayed_message.relay_embed)
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Research")
        self.assertEqual(relayed_message.relay_embed.description, "Electronics 1")


if __name__ == "__main__":
    unittest.main()
