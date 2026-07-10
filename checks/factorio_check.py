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
from unittest.mock import AsyncMock, MagicMock, call, patch

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
    FactorioActivities,
    FactorioActivitySnapshot,
    FactorioEvolution,
    FactorioMapAge,
    FactorioModMetadata,
    FactorioModPortalCredentials,
    FactorioSurfaceEvolution,
    Matchers,
    Mod_Factorio,
    Players,
    Provider_FactorioEvolution,
    Provider_FactorioMapAge,
    Receiver,
    _ensure_factorio_binary_executable,
    _factorio_download_archive_path,
    _factorio_latest_headless_versions,
    _factorio_mod_portal_release_from_mapping,
    _format_factorio_bridge_say_command,
    _format_factorio_console_message,
    _parse_factorio_bridge_evolution_snapshot,
    _parse_factorio_evolution,
    _parse_factorio_map_age,
    _parse_factorio_surface_evolutions,
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

    def test_parse_factorio_mod_portal_url_accepts_mod_subpage(self) -> None:
        mod_id = parse_factorio_mod_portal_url("https://mods.factorio.com/mod/space-exploration/dependencies")

        self.assertEqual(mod_id, "space-exploration")

    def test_parse_factorio_mod_portal_url_accepts_query_and_fragment(self) -> None:
        mod_id = parse_factorio_mod_portal_url(
            "https://mods.factorio.com/mod/even-distribution?from=downloaded#downloads"
        )

        self.assertEqual(mod_id, "even-distribution")

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
                        "+ recommended-lib",
                        "! incompatible-lib",
                        "~ hidden-required-lib >= 1.0.0",
                        "required-lib >= 1.0.0",
                    ],
                },
            }
        )

        self.assertEqual(release.dependencies, ("required-lib", "hidden-required-lib"))

    def test_factorio_mod_portal_resolution_ignores_recommended_dependencies(self) -> None:
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
            def __init__(self) -> None:
                self.requested_mod_ids: list[str] = []

            async def __aenter__(self) -> "_FakeSession":
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def get(self, url: str, **_kwargs: object) -> _FakeResponse:
                mod_id = url.split("/api/mods/", maxsplit=1)[1].removesuffix("/full")
                self.requested_mod_ids.append(mod_id)
                payloads: dict[str, dict[str, object]] = {
                    "root": {
                        "name": "root",
                        "title": "Root Mod",
                        "releases": [release_payload("root", ["+ recommended-lib"])],
                    },
                    "recommended-lib": {
                        "name": "recommended-lib",
                        "title": "Recommended Library",
                        "releases": [release_payload("recommended-lib", [])],
                    },
                }
                return _FakeResponse(payloads[mod_id])

        session = _FakeSession()
        with patch("apps.factorio.aiohttp.ClientSession", return_value=session):
            resolution = asyncio.run(
                resolve_factorio_mod_portal_candidates(
                    page_url="https://mods.factorio.com/mod/root",
                    factorio_version=AppVersion(main="2.0.68"),
                )
            )

        self.assertEqual(tuple(candidate.mod_id for candidate in resolution.candidates), ("root",))
        self.assertEqual(session.requested_mod_ids, ["root"])

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
        self.assertEqual(resolution.candidates[0].dependency_ids, ("dep-one",))
        self.assertEqual(resolution.candidates[1].required_by, ("root",))
        self.assertEqual(resolution.candidates[1].dependency_ids, ("dep-two",))
        self.assertEqual(resolution.candidates[2].required_by, ("dep-one",))

    def test_factorio_mod_portal_resolution_tracks_shared_dependency_parents(self) -> None:
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
                payloads: dict[str, dict[str, object]] = {
                    "root": {
                        "name": "root",
                        "title": "Root Mod",
                        "releases": [release_payload("root", ["dep-one", "dep-two"])],
                    },
                    "dep-one": {
                        "name": "dep-one",
                        "title": "Dependency One",
                        "releases": [release_payload("dep-one", ["shared"])],
                    },
                    "dep-two": {
                        "name": "dep-two",
                        "title": "Dependency Two",
                        "releases": [release_payload("dep-two", ["shared"])],
                    },
                    "shared": {
                        "name": "shared",
                        "title": "Shared Dependency",
                        "releases": [release_payload("shared", [])],
                    },
                }
                return _FakeResponse(payloads[mod_id])

        with patch("apps.factorio.aiohttp.ClientSession", return_value=_FakeSession()):
            resolution = asyncio.run(
                resolve_factorio_mod_portal_candidates(
                    page_url="https://mods.factorio.com/mod/root",
                    factorio_version=AppVersion(main="2.0.68"),
                )
            )

        shared = next(candidate for candidate in resolution.candidates if candidate.mod_id == "shared")
        self.assertEqual(shared.required_by, ("dep-one", "dep-two"))

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

    def test_factorio_exposes_curated_console_actions(self) -> None:
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

        self.assertEqual(
            tuple(action.key for action in actions),
            (
                "admins",
                "seed",
                "time",
                "perf_avg_frames",
                "shout",
                "raw_command",
                "server_save",
                "promote",
                "demote",
                "kick",
                "cheat",
                "command",
                "silent_command",
            ),
        )
        self.assertTrue(app.rcon_requires_online_players_enabled)
        self.assertEqual(actions[0].label, "List Admins")
        self.assertEqual(actions[0].power_level.name, "user")
        self.assertEqual(actions[5].label, "Run Command")
        self.assertEqual(actions[5].power_level.name, "sudo")

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
        app.player_count = AsyncMock(return_value=(1, 20))
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
        app.player_count.assert_not_awaited()
        self.assertEqual(result.source, ConsoleResponseSource.RCON)
        self.assertEqual(result.text, "command output")

    def test_factorio_raw_console_command_skips_player_gate_for_manual_console(self) -> None:
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
        app.player_count = AsyncMock(return_value=(0, 20))
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
        app.player_count.assert_not_awaited()
        self.assertEqual(result.source, ConsoleResponseSource.RCON)

    def test_factorio_admins_console_action_requests_admin_list(self) -> None:
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
        app._relay = SimpleNamespace(send=AsyncMock(return_value="Admins: Alice, Bob"))
        app.player_count = AsyncMock(return_value=(1, 20))
        action = next(action for action in app.console_actions if action.key == "admins")

        result = asyncio.run(
            execute_console_action(
                app=app,
                is_running=app.check_running,
                action=action,
                raw_value=None,
            )
        )

        app._relay.send.assert_awaited_once_with("/admins")
        app.player_count.assert_not_awaited()
        self.assertEqual(result.source, ConsoleResponseSource.RCON)
        self.assertEqual(result.text, "Admins: Alice, Bob")

    def test_factorio_promote_console_action_sends_rcon_command(self) -> None:
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
        app._relay = SimpleNamespace(send=AsyncMock(return_value="Promoted Alice"))
        app.player_count = AsyncMock(return_value=(1, 20))
        action = next(action for action in app.console_actions if action.key == "promote")

        result = asyncio.run(
            execute_console_action(
                app=app,
                is_running=app.check_running,
                action=action,
                raw_value="Alice",
            )
        )

        app._relay.send.assert_awaited_once_with("/promote Alice")
        app.player_count.assert_not_awaited()
        self.assertEqual(result.source, ConsoleResponseSource.RCON)
        self.assertEqual(result.text, "Promoted Alice")

    def test_factorio_demote_console_action_sends_rcon_command(self) -> None:
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
        app._relay = SimpleNamespace(send=AsyncMock(return_value="Demoted Alice"))
        app.player_count = AsyncMock(return_value=(1, 20))
        action = next(action for action in app.console_actions if action.key == "demote")

        result = asyncio.run(
            execute_console_action(
                app=app,
                is_running=app.check_running,
                action=action,
                raw_value="Alice",
            )
        )

        app._relay.send.assert_awaited_once_with("/demote Alice")
        app.player_count.assert_not_awaited()
        self.assertEqual(result.source, ConsoleResponseSource.RCON)
        self.assertEqual(result.text, "Demoted Alice")

    def test_factorio_added_console_actions_send_wiki_commands(self) -> None:
        cases: tuple[tuple[str, str | None, str], ...] = (
            ("seed", None, "/seed"),
            ("time", None, "/time"),
            ("perf_avg_frames", "10", "/perf-avg-frames 10"),
            ("server_save", None, "/server-save"),
            ("shout", "Hello factory", "/shout Hello factory"),
            ("cheat", "off", "/cheat off"),
            ("command", "game.print('hi')", "/command game.print('hi')"),
            ("silent_command", "game.print('hi')", "/silent-command game.print('hi')"),
        )

        for action_key, raw_value, expected_command in cases:
            with self.subTest(action_key=action_key):
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
                app._relay = SimpleNamespace(send=AsyncMock(return_value="ok"))
                app.player_count = AsyncMock(return_value=(0, 20))
                action = next(action for action in app.console_actions if action.key == action_key)

                result = asyncio.run(
                    execute_console_action(
                        app=app,
                        is_running=app.check_running,
                        action=action,
                        raw_value=raw_value,
                    )
                )

                app._relay.send.assert_awaited_once_with(expected_command)
                app.player_count.assert_not_awaited()
                self.assertEqual(result.source, ConsoleResponseSource.RCON)

    def test_factorio_kick_console_action_parses_player_and_reason(self) -> None:
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
        app._relay = SimpleNamespace(send=AsyncMock(return_value="Kicked Alice"))
        app.player_count = AsyncMock(return_value=(0, 20))
        action = next(action for action in app.console_actions if action.key == "kick")

        result = asyncio.run(
            execute_console_action(
                app=app,
                is_running=app.check_running,
                action=action,
                raw_value="Alice griefing",
            )
        )

        app._relay.send.assert_awaited_once_with("/kick Alice griefing")
        app.player_count.assert_not_awaited()
        self.assertEqual(result.text, "Kicked Alice")

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

    def test_factorio_updater_rejects_concurrent_scope_operation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = Factorio_Updater(_FakeFactorioUpdateApp(root / "first"), base=True)
            second = Factorio_Updater(_FakeFactorioUpdateApp(root / "second"), base=True)

            first._begin_selected_operation(AppUpdateOperationKind.UPDATE)
            try:
                with self.assertRaisesRegex(RuntimeError, "scope `factorio`"):
                    second._begin_selected_operation(AppUpdateOperationKind.VERIFY)
            finally:
                first._operation_running = False
                first._release_scope_update_lock()


class FactorioActivityTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_factorio_map_age_from_human_readable_output(self) -> None:
        map_age = _parse_factorio_map_age("Map age: 3 days 5 hours 17 minutes 42 seconds.")

        self.assertEqual(map_age, FactorioMapAge(total_seconds=(3 * 86_400) + (5 * 3_600) + (17 * 60) + 42))
        assert map_age is not None
        self.assertEqual(map_age.activity_text(), "D3/H05")

    def test_parse_factorio_evolution_from_console_output(self) -> None:
        evolution = _parse_factorio_evolution(
            "Evolution factor: 0.9066. (Time 20%) (Pollution 45%) (Spawner kills: 33%)"
        )

        self.assertEqual(evolution, FactorioEvolution(factor=0.9066))
        assert evolution is not None
        self.assertEqual(evolution.activity_text(), "90.7%")

    def test_parse_factorio_surface_evolutions_from_full_list_output(self) -> None:
        surface_evolutions = _parse_factorio_surface_evolutions(
            "\n".join(
                (
                    "Gleba:",
                    "Evolution factor: 0.245. (Time 10%) (Pollution 15%) (Spawner kills: 0%)",
                    "Nauvis:",
                    "Evolution factor: 0.9066. (Time 20%) (Pollution 45%) (Spawner kills: 33%)",
                    "Vulcanus: Evolution factor: 0",
                )
            )
        )

        self.assertEqual(
            surface_evolutions,
            (
                FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.9066)),
                FactorioSurfaceEvolution("Gleba", FactorioEvolution(factor=0.245)),
                FactorioSurfaceEvolution("Vulcanus", FactorioEvolution(factor=0.0)),
            ),
        )

    def test_parse_factorio_bridge_evolution_from_json_output(self) -> None:
        snapshot = _parse_factorio_bridge_evolution_snapshot(
            json.dumps(
                {
                    "kind": "evolution",
                    "tick": 120,
                    "force": "player",
                    "surfaces": [
                        {
                            "surface": {"name": "gleba", "planet": "gleba"},
                            "evolution": {"total": 0.125, "pollution": 0.0, "time": 0.125, "spawner_kills": 0.0},
                        },
                        {
                            "surface": {"name": "nauvis", "planet": "nauvis"},
                            "evolution": {"total": 0.375, "pollution": 0.1, "time": 0.2, "spawner_kills": 0.075},
                        },
                    ],
                }
            )
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.primary_evolution, FactorioEvolution(factor=0.375))
        self.assertEqual(
            snapshot.surface_evolutions,
            (
                FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.375)),
                FactorioSurfaceEvolution("Gleba", FactorioEvolution(factor=0.125)),
            ),
        )

    async def test_activity_providers_read_cached_factorio_snapshot(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app._factorio_yuki_bridge_enabled = True
        app._activities = SimpleNamespace(
            snapshot=SimpleNamespace(
                map_age=FactorioMapAge(total_seconds=(2 * 86_400) + (7 * 3_600)),
                primary_evolution=FactorioEvolution(factor=0.375),
                surface_evolutions=(
                    FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.375)),
                    FactorioSurfaceEvolution("Gleba", FactorioEvolution(factor=0.125)),
                ),
            )
        )

        self.assertEqual(await Provider_FactorioMapAge(app).get(), "D2/H07")
        self.assertEqual(await Provider_FactorioEvolution(app).get(), "37.5%")
        self.assertEqual(
            await Provider_FactorioEvolution(app).detail(),
            "Nauvis: 37.5%\nGleba: 12.5%",
        )

    async def test_factorio_evolution_provider_requires_yuki_bridge(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app._factorio_yuki_bridge_enabled = False
        app._activities = SimpleNamespace(
            snapshot=SimpleNamespace(
                primary_evolution=FactorioEvolution(factor=0.375),
                surface_evolutions=(FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.375)),),
            )
        )

        self.assertIsNone(await Provider_FactorioEvolution(app).get())
        self.assertIsNone(await Provider_FactorioEvolution(app).detail())

    def test_yuki_bridge_enabled_detects_factorio_mod_by_native_mod_id(self) -> None:
        class _BridgeMod(Mod_Factorio):
            @property
            def server_loadable(self) -> bool:
                return True

        bridge_mod = object.__new__(_BridgeMod)
        bridge_mod.cfg = SimpleNamespace(enabled=True)
        bridge_mod._detected_metadata = FactorioModMetadata(name="yuki-bridge")
        bridge_mod.name = "yuki-bridge_1.2.3.zip"
        bridge_mod.friendly = "Yuki Bridge"
        app = cast(Any, object.__new__(Factorio))
        app.mods = SimpleNamespace(list_mods=lambda state=None: [bridge_mod])

        self.assertTrue(app.yuki_bridge_enabled)

    async def test_receiver_prefers_shout_without_script_fallback(self) -> None:
        relay = SimpleNamespace(send=AsyncMock(return_value=None))
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app._factorio_yuki_bridge_enabled = False
        receiver = Receiver(app)
        payload = SimpleNamespace(
            alias="DiscordUser",
            content="Hello \"factory\"",
            urls=(),
            files=(),
            content_for_app=lambda _app: "Hello \"factory\"",
        )

        await receiver.send(payload)

        relay.send.assert_awaited_once_with("/shout DiscordUser: Hello 'factory'")

    async def test_receiver_prefers_yuki_bridge_say_without_shout_fallback(self) -> None:
        relay = SimpleNamespace(send=AsyncMock(return_value=None))
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app._factorio_yuki_bridge_enabled = True
        receiver = Receiver(app)
        payload = SimpleNamespace(
            alias="DiscordUser",
            content="Hello \"factory\"",
            urls=(),
            files=(),
            content_for_app=lambda _app: "Hello \"factory\"",
        )

        await receiver.send(payload)

        relay.send.assert_awaited_once_with('/yuki say DiscordUser|Hello "factory"')

    async def test_receiver_falls_back_to_silent_command_for_failed_shout(self) -> None:
        relay = SimpleNamespace(send=AsyncMock(side_effect=("Unknown command: /shout", None)))
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app.cfg = SimpleNamespace(factorio_chat_relay_use_shout=True, rcon_requires_online_players=False)
        app._factorio_yuki_bridge_enabled = False
        receiver = Receiver(app)
        payload = SimpleNamespace(
            alias="DiscordUser",
            content="Line one\nLine two",
            urls=(),
            files=(),
            content_for_app=lambda _app: "Line one\nLine two",
        )

        await receiver.send(payload)

        self.assertEqual(relay.send.await_count, 2)
        self.assertEqual(relay.send.await_args_list[0].args, ("/shout DiscordUser: Line one Line two",))
        self.assertEqual(
            relay.send.await_args_list[1].args,
            ('/silent-command game.print("DiscordUser: Line one Line two")',),
        )

    async def test_receiver_falls_back_to_shout_for_failed_yuki_bridge_say(self) -> None:
        relay = SimpleNamespace(send=AsyncMock(side_effect=("Usage: /yuki say Speaker|message", None)))
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app.cfg = SimpleNamespace(factorio_chat_relay_use_shout=True, rcon_requires_online_players=False)
        app._factorio_yuki_bridge_enabled = True
        receiver = Receiver(app)
        payload = SimpleNamespace(
            alias="DiscordUser",
            content="Line one\nLine two",
            urls=(),
            files=(),
            content_for_app=lambda _app: "Line one\nLine two",
        )

        await receiver.send(payload)

        self.assertEqual(relay.send.await_count, 2)
        self.assertEqual(relay.send.await_args_list[0].args, ("/yuki say DiscordUser|Line one Line two",))
        self.assertEqual(relay.send.await_args_list[1].args, ("/shout DiscordUser: Line one Line two",))

    async def test_receiver_uses_silent_command_when_shout_disabled(self) -> None:
        relay = SimpleNamespace(send=AsyncMock(return_value=None))
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app.cfg = SimpleNamespace(factorio_chat_relay_use_shout=False, rcon_requires_online_players=False)
        receiver = Receiver(app)
        payload = SimpleNamespace(
            alias="DiscordUser",
            content="Line one\nLine two",
            urls=(),
            files=(),
            content_for_app=lambda _app: "Line one\nLine two",
        )

        await receiver.send(payload)

        relay.send.assert_awaited_once_with('/silent-command game.print("DiscordUser: Line one Line two")')

    async def test_receiver_skips_rcon_when_player_gate_has_no_online_players(self) -> None:
        relay = SimpleNamespace(send=AsyncMock(return_value=None))
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_alpha"
        app.friendly = "Factorio"
        app.cfg = App_Config(
            name="factorio_alpha",
            instance_key="alpha",
            friendly_name="Factorio",
            directory=Path("."),
            apps_dir=Path("."),
            scope="factorio",
        )
        app._relay = relay
        app.player_count = AsyncMock(return_value=(0, 20))
        app._factorio_yuki_bridge_enabled = False
        receiver = Receiver(app)
        payload = SimpleNamespace(
            alias="DiscordUser",
            content="Hello",
            urls=(),
            files=(),
            content_for_app=lambda _app: "Hello",
        )

        await receiver.send(payload)

        relay.send.assert_not_awaited()
        app.player_count.assert_awaited_once()

    async def test_match_player_session_updates_players_from_join_game_stdout_event(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app._tail_machers = set()
        app._players = SimpleNamespace(note_player_join_signal=MagicMock())
        matcher = Matchers(app)

        await matcher.match_player_session(
            '1278.355 Script @__yuki-bridge__/control.lua:57: [Yuki] '
            '{"event":"PlayerJoinGame"}'
        )

        app._players.note_player_join_signal.assert_called_once_with()

    async def test_match_player_session_ignores_nonexistent_leave_game_stdout_event(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app._tail_machers = set()
        app._players = SimpleNamespace(note_player_join_signal=MagicMock())
        matcher = Matchers(app)

        await matcher.match_player_session("PlayerLeaveGame player=Alice")

        app._players.note_player_join_signal.assert_not_called()

    async def test_players_poll_fetches_max_players_once_per_session(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.friendly = "Factorio"
        app.cfg = SimpleNamespace(relay_notice_player_session=False)
        app._relay = SimpleNamespace(
            send=AsyncMock(
                side_effect=(
                    "Value of option max-players is 20",
                    "Online players (1):\nAlice (online)",
                    "Online players (1):\nAlice (online)",
                )
            )
        )
        players = Players(app)
        players._running = True

        first_count = await players._poll_player_snapshot()
        second_count = await players._poll_player_snapshot()

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1)
        self.assertEqual(await players.count(), (1, 20))
        self.assertEqual(
            app._relay.send.await_args_list,
            [
                call("/config get max-players"),
                call("/players online"),
                call("/players online"),
            ],
        )

    async def test_players_poll_returns_to_idle_after_three_empty_snapshots(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.friendly = "Factorio"
        app.cfg = SimpleNamespace(relay_notice_player_session=False)
        app.check_running = lambda: True
        app._relay = SimpleNamespace(
            send=AsyncMock(
                side_effect=(
                    "Value of option max-players is 20",
                    "Online players (0):",
                    "Online players (0):",
                    "Online players (0):",
                )
            )
        )
        players = Players(app)
        players._running = True
        players.note_player_join_signal()

        with patch("apps.factorio.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            await players._poll_until_idle()

        self.assertEqual(players._online, 0)
        self.assertEqual(players._empty_poll_count, 0)
        self.assertEqual(sleep_mock.await_count, 2)
        self.assertEqual(
            app._relay.send.await_args_list,
            [
                call("/config get max-players"),
                call("/players online"),
                call("/players online"),
                call("/players online"),
            ],
        )

    async def test_factorio_activities_poll_updates_snapshot(self) -> None:
        relay = SimpleNamespace(
            send=AsyncMock(
                return_value={
                    "time": "Map age: 4 days 2 hours 0 minutes 0 seconds.",
                    "evolution": json.dumps(
                        {
                            "kind": "evolution",
                            "tick": 120,
                            "force": "player",
                            "surfaces": [
                                {"surface": {"name": "Nauvis"}, "evolution": {"total": 0.245}},
                                {"surface": {"name": "Gleba"}, "evolution": {"total": 0.125}},
                            ],
                        }
                    ),
                }
            )
        )
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app.check_running = lambda: True
        app.name = "factorio_demo"
        app._factorio_yuki_bridge_enabled = True
        app.register_enabled_activity_providers = MagicMock()
        app.deregister_activity_providers = MagicMock()
        app.set_activity_providers = MagicMock()
        app._cancel_background_task = AsyncMock()

        activities = FactorioActivities(app)
        self.assertEqual(len(app.set_activity_providers.call_args.args[0]), 2)

        with patch("apps.factorio.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await activities._poll()

        self.assertEqual(activities.snapshot.map_age, FactorioMapAge(total_seconds=(4 * 86_400) + (2 * 3_600)))
        self.assertEqual(activities.snapshot.primary_evolution, FactorioEvolution(factor=0.245))
        self.assertEqual(
            activities.snapshot.surface_evolutions,
            (
                FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.245)),
                FactorioSurfaceEvolution("Gleba", FactorioEvolution(factor=0.125)),
            ),
        )
        relay.send.assert_awaited_once_with({"time": "/time", "evolution": "/yuki evolution player"})

    async def test_factorio_activities_poll_preserves_ndjson_evolution_when_bridge_tail_is_active(self) -> None:
        relay = SimpleNamespace(
            send=AsyncMock(
                return_value={
                    "time": "Map age: 4 days 2 hours 0 minutes 0 seconds.",
                    "evolution": json.dumps(
                        {
                            "kind": "evolution",
                            "tick": 120,
                            "force": "player",
                            "surfaces": [
                                {"surface": {"name": "Nauvis"}, "evolution": {"total": 0.999}},
                            ],
                        }
                    ),
                }
            )
        )
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app.check_running = lambda: True
        app.name = "factorio_demo"
        app._factorio_yuki_bridge_enabled = True
        app._bridge_events_tail = object()
        app.register_enabled_activity_providers = MagicMock()
        app.deregister_activity_providers = MagicMock()
        app.set_activity_providers = MagicMock()
        app._cancel_background_task = AsyncMock()

        activities = FactorioActivities(app)
        activities.snapshot = FactorioActivitySnapshot(
            primary_evolution=FactorioEvolution(factor=0.123),
            surface_evolutions=(FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.123)),),
        )

        with patch("apps.factorio.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await activities._poll()

        self.assertEqual(activities.snapshot.map_age, FactorioMapAge(total_seconds=(4 * 86_400) + (2 * 3_600)))
        self.assertEqual(activities.snapshot.primary_evolution, FactorioEvolution(factor=0.123))
        self.assertEqual(
            activities.snapshot.surface_evolutions,
            (FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.123)),),
        )
        relay.send.assert_awaited_once_with({"time": "/time", "evolution": "/yuki evolution player"})

    async def test_factorio_activities_skip_evolution_without_yuki_bridge(self) -> None:
        relay = SimpleNamespace(send=AsyncMock(return_value={"time": "Map age: 4 days 2 hours 0 minutes 0 seconds."}))
        app = cast(Any, object.__new__(Factorio))
        app._relay = relay
        app.check_running = lambda: True
        app.name = "factorio_demo"
        app._factorio_yuki_bridge_enabled = False
        app.register_enabled_activity_providers = MagicMock()
        app.deregister_activity_providers = MagicMock()
        app.set_activity_providers = MagicMock()
        app._cancel_background_task = AsyncMock()

        activities = FactorioActivities(app)
        self.assertEqual(len(app.set_activity_providers.call_args.args[0]), 1)

        with patch("apps.factorio.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await activities._poll()

        self.assertEqual(activities.snapshot.map_age, FactorioMapAge(total_seconds=(4 * 86_400) + (2 * 3_600)))
        self.assertIsNone(activities.snapshot.primary_evolution)
        self.assertEqual(activities.snapshot.surface_evolutions, ())
        relay.send.assert_awaited_once_with({"time": "/time"})

    def test_format_factorio_console_message_sanitises_quotes_and_newlines(self) -> None:
        message = _format_factorio_console_message(alias="Yuki\nBot", content='One "two"\nthree')

        self.assertEqual(message, "Yuki Bot: One 'two' three")

    def test_format_factorio_bridge_say_command_uses_speaker_message_separator(self) -> None:
        command = _format_factorio_bridge_say_command(alias="Yuki|Bot", content='One "two"\nthree')

        self.assertEqual(command, '/yuki say Yuki/Bot|One "two" three')


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
        app._factorio_yuki_bridge_enabled = True
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

    async def test_match_research_relays_yuki_bridge_finished_notice(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._factorio_yuki_bridge_enabled = True
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.factorio.DC_Relay.add") as add_mock:
            await matcher.match_research(
                '1278.355 Script @__yuki-bridge__/control.lua:57: [Yuki] '
                '{"technology":"bulk-inserter","localised_name":["technology-name.bulk-inserter"],'
                '"level":1,"force":"player","by_script":false,"kind":"research_finished",'
                '"tick":2840520,"mod":"yuki-bridge"}'
            )

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Research: Bulk Inserter")
        self.assertIsNotNone(relayed_message.relay_embed)
        assert relayed_message.relay_embed is not None
        self.assertEqual(relayed_message.relay_embed.title, "Research")
        self.assertEqual(relayed_message.relay_embed.description, "Bulk Inserter")

    async def test_match_research_relays_raw_yuki_bridge_event(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._factorio_yuki_bridge_enabled = True
        app._tail_machers = set()
        app._bridge_tail_matchers = set()
        app._bridge_events_tail = object()
        matcher = Matchers(app)

        with patch("apps.factorio.DC_Relay.add") as add_mock:
            await matcher.match_bridge_event(
                '{"technology":"bulk-inserter","localised_name":["technology-name.bulk-inserter"],'
                '"level":1,"force":"player","by_script":false,"kind":"research_finished",'
                '"tick":2840520,"mod":"yuki-bridge"}'
            )

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "System")
        self.assertEqual(relayed_message.content, "Research: Bulk Inserter")

    async def test_match_research_skips_wrapped_yuki_bridge_event_when_ndjson_tail_is_active(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._factorio_yuki_bridge_enabled = True
        app._tail_machers = set()
        app._bridge_tail_matchers = set()
        app._bridge_events_tail = object()
        matcher = Matchers(app)

        with patch("apps.factorio.DC_Relay.add") as add_mock:
            await matcher.match_research(
                '1278.355 Script @__yuki-bridge__/control.lua:57: [Yuki] '
                '{"technology":"bulk-inserter","localised_name":["technology-name.bulk-inserter"],'
                '"level":1,"force":"player","by_script":false,"kind":"research_finished",'
                '"tick":2840520,"mod":"yuki-bridge"}'
            )

        add_mock.assert_not_called()

    async def test_match_bridge_event_updates_evolution_snapshot_from_raw_event(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._factorio_yuki_bridge_enabled = True
        app._tail_machers = set()
        app._bridge_tail_matchers = set()
        app._bridge_events_tail = object()
        app._activities = FactorioActivities.__new__(FactorioActivities)
        app._activities.snapshot = FactorioActivitySnapshot(map_age=FactorioMapAge(total_seconds=90_000))
        matcher = Matchers(app)

        await matcher.match_bridge_event(
            '{"kind":"evolution","tick":1082092,"force":"player","surfaces":['
            '{"surface":{"name":"nauvis","index":1,"planet":"nauvis"},'
            '"evolution":{"total":0.06728584193305588,"pollution":0,"time":0,"spawner_kills":0}},'
            '{"surface":{"name":"vulcanus","index":2,"planet":"vulcanus"},'
            '"evolution":{"total":0,"pollution":0,"time":0,"spawner_kills":0}}'
            '],"by":"rcon","mod":"yuki-bridge"}'
        )

        self.assertEqual(app._activities.snapshot.map_age, FactorioMapAge(total_seconds=90_000))
        self.assertEqual(app._activities.snapshot.primary_evolution, FactorioEvolution(factor=0.06728584193305588))
        self.assertEqual(
            app._activities.snapshot.surface_evolutions,
            (
                FactorioSurfaceEvolution("Nauvis", FactorioEvolution(factor=0.06728584193305588)),
                FactorioSurfaceEvolution("Vulcanus", FactorioEvolution(factor=0.0)),
            ),
        )

    async def test_match_research_requires_yuki_bridge(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._factorio_yuki_bridge_enabled = False
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.factorio.DC_Relay.add") as add_mock:
            await matcher.match_research(
                "891.725 Script @__events-logger__/events/research.lua:23: [RESEARCH FINISHED] electronics 1"
            )

        add_mock.assert_not_called()

    async def test_match_death_requires_yuki_bridge(self) -> None:
        app = cast(Any, object.__new__(Factorio))
        app.name = "factorio_demo"
        app.scope = "factorio"
        app.manage_embed_color = 0xDC6B0F
        app._factorio_yuki_bridge_enabled = False
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.factorio.DC_Relay.add") as add_mock:
            await matcher.match_death("[DIED] PVE:Yuki biter")

        add_mock.assert_not_called()


class FactorioStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_tears_down_relay_and_process(self) -> None:
        app = cast(Factorio, object.__new__(Factorio))
        app.name = "factorio_demo"
        app._startup_error = "Util.cpp:81: failed"
        app.check_running = lambda: True
        tail = SimpleNamespace(stop=AsyncMock())
        app._tail = tail
        app._stop_bridge_events_tail = AsyncMock()
        app._terminate = AsyncMock()
        app._relay = SimpleNamespace(setup=AsyncMock(), teardown=AsyncMock())

        with self.assertRaisesRegex(RuntimeError, "failed"):
            await app._wait_for_startup_ready()

        tail.stop.assert_awaited_once()
        self.assertIsNone(app._tail)
        app._stop_bridge_events_tail.assert_awaited_once()
        app._relay.teardown.assert_awaited_once()
        app._terminate.assert_awaited_once()

    async def test_stop_waits_after_save_request_before_terminating(self) -> None:
        app = cast(Factorio, object.__new__(Factorio))
        app._running = True
        app._activities = SimpleNamespace(stop=AsyncMock())
        app._players = SimpleNamespace(stop=AsyncMock())
        app._relay = SimpleNamespace(is_connected=True, send=AsyncMock(return_value="saved"), teardown=AsyncMock())
        app._tail = None
        app._bridge_events_tail = None
        app.process = None
        app._stop_bridge_events_tail = AsyncMock()
        app._terminate = AsyncMock()
        app._lock = Path("/tmp/factorio-test.lock")

        with patch("apps.factorio.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await Factorio.stop(app)

        self.assertTrue(result)
        app._relay.send.assert_awaited_once_with("/server-save", reconnect_on_failure=False)
        self.assertEqual(
            sleep_mock.await_args_list,
            [
                call(1.0),
                call(0.5),
            ],
        )
        app._relay.teardown.assert_awaited_once()
        app._terminate.assert_awaited_once()

    async def test_stop_continues_cleanup_when_stdin_save_fallback_fails(self) -> None:
        class _BrokenStdin:
            def write(self, _value: str) -> None:
                raise BrokenPipeError("closed")

            def flush(self) -> None:
                raise AssertionError("flush should not be called after failed write")

        app = cast(Factorio, object.__new__(Factorio))
        app.name = "factorio_demo"
        app._running = True
        app._activities = SimpleNamespace(stop=AsyncMock())
        app._players = SimpleNamespace(stop=AsyncMock())
        app._relay = SimpleNamespace(is_connected=False, teardown=AsyncMock())
        tail = SimpleNamespace(stop=AsyncMock())
        app._tail = tail
        app._bridge_events_tail = None
        app.process = SimpleNamespace(stdin=_BrokenStdin())
        app._stop_bridge_events_tail = AsyncMock()
        app._terminate = AsyncMock()
        app._lock = Path("/tmp/factorio-test.lock")

        with patch("apps.factorio.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await Factorio.stop(app)

        self.assertTrue(result)
        tail.stop.assert_awaited_once()
        app._stop_bridge_events_tail.assert_awaited_once()
        app._relay.teardown.assert_awaited_once()
        app._terminate.assert_awaited_once()
        sleep_mock.assert_awaited_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
