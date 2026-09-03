from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import psutil
from pydantic import ValidationError

from apps._app import App
from apps._config import AppVersion
from apps._console import ConsoleResponseSource, execute_console_action
from apps._settings import Setting
from apps.satisfactory import (
    Satisfactory,
    SatisfactoryAdvancedGameSettingsSnapshot,
    Satisfactory_Config,
    SatisfactoryBlueprintOwnershipStore,
    SatisfactoryNetworkQuality,
    SatisfactoryPlayers,
    SatisfactoryPlayerSessionMatcher,
    SatisfactoryBridge,
    SatisfactorySaveHeader,
    SatisfactoryServerOptionsSnapshot,
    SatisfactoryServerState,
    SatisfactorySessionEnumerationSnapshot,
    SatisfactorySessionSaveSnapshot,
    SatisfactorySettings,
    SatisfactorySettingsSnapshot,
    _SATISFACTORY_SERVER_SAVE_ROOT,
    _build_satisfactory_activity_providers,
    _normalise_active_schematic_label,
    _parse_api_endpoint,
    _satisfactory_start_command,
    detect_satisfactory_version,
)
from relay_notices import PlayerSessionAction


class _FakeBridge:
    def __init__(self) -> None:
        self.snapshot = SatisfactorySettingsSnapshot(
            auto_load_session_name="SERVER-SESSION",
            server_options=SatisfactoryServerOptionsSnapshot(
                auto_pause=False,
                auto_save_on_disconnect=True,
                autosave_interval_seconds=300,
                send_gameplay_data=True,
                network_quality=SatisfactoryNetworkQuality.MEDIUM,
            ),
            advanced_game_settings=SatisfactoryAdvancedGameSettingsSnapshot(
                creative_mode_enabled=True,
                no_power=False,
                give_all_tiers=True,
                unlock_all_research_schematics=False,
                set_game_phase=2,
            ),
        )
        self.state = SatisfactoryServerState(
            active_session_name="SERVER-SESSION",
            auto_load_session_name="SERVER-SESSION",
            num_connected_players=2,
            player_limit=8,
            tech_tier=3,
            active_schematic="Schematic_3-2",
            is_game_running=True,
            total_game_duration=172800,
        )
        self.applied: list[SatisfactorySettingsSnapshot] = []
        self.commands: list[str] = []
        self.saved_games: list[str] = []
        self.deleted_save_names: list[str] = []
        self.uploaded_save_names: list[str] = []
        self.shutdown_count = 0
        self.sessions = SatisfactorySessionEnumerationSnapshot(
            sessions=(
                SatisfactorySessionSaveSnapshot(
                    session_name="SERVER-SESSION",
                    save_headers=(
                        SatisfactorySaveHeader(
                            save_name="SERVER-SESSION_autosave_0.sav",
                            session_name="SERVER-SESSION",
                            save_date_time=datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
                        ),
                    ),
                ),
            ),
            current_session_index=0,
        )

    async def read_settings(self) -> SatisfactorySettingsSnapshot:
        return self.snapshot.model_copy(deep=True)

    async def apply_settings(self, settings: SatisfactorySettingsSnapshot) -> None:
        copied = settings.model_copy(deep=True)
        self.applied.append(copied)
        self.snapshot = copied

    async def query_server_state(self) -> SatisfactoryServerState:
        return self.state.model_copy(deep=True)

    async def enumerate_sessions(self) -> SatisfactorySessionEnumerationSnapshot:
        return self.sessions.model_copy(deep=True)

    async def run_command(self, command: str) -> str:
        self.commands.append(command)
        return f"ran {command}"

    async def save_game(self, save_name: str) -> str:
        self.saved_games.append(save_name)
        return f"saved {save_name}"

    async def delete_save_file(self, save_name: str) -> str:
        self.deleted_save_names.append(save_name)
        remaining_sessions: list[SatisfactorySessionSaveSnapshot] = []
        for session in self.sessions.sessions:
            remaining_headers = tuple(header for header in session.save_headers if header.save_name != save_name)
            remaining_sessions.append(
                SatisfactorySessionSaveSnapshot(session_name=session.session_name, save_headers=remaining_headers)
            )
        self.sessions = SatisfactorySessionEnumerationSnapshot(
            sessions=tuple(session for session in remaining_sessions if session.save_headers),
            current_session_index=0 if remaining_sessions else None,
        )
        return f"deleted {save_name}"

    async def download_save_game(self, save_name: str) -> bytes:
        return f"save:{save_name}".encode("utf-8")

    async def upload_save_game(self, *, save_name: str, source_path: Path) -> str:
        self.uploaded_save_names.append(save_name)
        upload_bytes = source_path.read_bytes()
        self.sessions = SatisfactorySessionEnumerationSnapshot(
            sessions=(
                *self.sessions.sessions,
                SatisfactorySessionSaveSnapshot(
                    session_name="UPLOADED-SESSION",
                    save_headers=(
                        SatisfactorySaveHeader(
                            save_name=save_name,
                            session_name="UPLOADED-SESSION",
                            save_date_time=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
                        ),
                    ),
                ),
            ),
            current_session_index=0,
        )
        return f"uploaded {save_name}:{len(upload_bytes)}"

    async def shutdown(self) -> str:
        self.shutdown_count += 1
        return "shutdown requested"


class _DummyProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self._returncode: int | None = None

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


class _DummyApp(App):
    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True


class SatisfactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.temp_path = Path(self._temp_dir.name)
        self.apps_path = self.temp_path / "apps"
        self.apps_path.mkdir()
        self.instances_path = self.apps_path / "instances.json"
        self.instances_path.write_text(
            json.dumps(
                {
                    "alpha": {
                        "friendly_name": "Satisfactory",
                        "directory": "{APPS}/server",
                        "admin_password": "secret",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.cache_path = self.temp_path / "settings.json"
        self.cache_path.write_text(
            """{
    "auto_load_session_name": "ALPHA",
    "auto_pause": true,
    "auto_save_on_disconnect": false,
    "autosave_interval_seconds": 600,
    "send_gameplay_data": false,
    "network_quality": 3,
    "no_power": true,
    "set_game_phase": 4,
    "give_items": "IronPlate 100"
}""",
            encoding="utf-8",
        )
        self.bridge = _FakeBridge()
        self._running = False
        self.cfg = Satisfactory_Config.model_validate(
            {
                "name": "satisfactory_alpha",
                "instance_key": "alpha",
                "friendly_name": "Satisfactory",
                "directory": self.temp_path / "server",
                "apps_dir": self.apps_path,
                "scope": "satisfactory",
                "api_host": "127.0.0.1",
                "join_port": 7777,
                "admin_password": "secret",
            }
        )
        self.settings = SatisfactorySettings(
            self.cache_path,
            self.bridge,
            lambda: self._running,
            self.cfg,
            self.instances_path,
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _setting(self, key: str) -> Setting:
        setting = self.settings.get_setting(key)
        if setting is None:
            raise AssertionError(f"Missing setting: {key}")
        return setting

    def _blueprint_app(self) -> Satisfactory:
        app = object.__new__(Satisfactory)
        app.friendly = "Satisfactory"
        app.dir_log = self.temp_path / "app-log"
        app.dir_log.mkdir(parents=True, exist_ok=True)
        app._blueprint_root_override = self.temp_path / "blueprints"
        app._blueprint_ownership_store = SatisfactoryBlueprintOwnershipStore(
            app.dir_log / "satisfactory-blueprints.json"
        )
        return app

    @staticmethod
    def _player_session_app() -> Satisfactory:
        app = object.__new__(Satisfactory)
        app.name = "satisfactory_alpha"
        app.friendly = "Satisfactory"
        app.scope = "satisfactory"
        app.cfg = SimpleNamespace(relay_notice_player_session=True)
        return app

    def _console_app(self) -> Satisfactory:
        app = object.__new__(Satisfactory)
        app.name = "satisfactory_alpha"
        app.friendly = "Satisfactory"
        app.scope = "satisfactory"
        app.cfg = self.cfg
        app._bridge = self.bridge
        app._players = SimpleNamespace(state=self.bridge.state.model_copy(deep=True))
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        return app

    def _activity_app(self) -> Satisfactory:
        app = self._console_app()
        app.activity_manager = Mock()
        app.set_activity_providers(_build_satisfactory_activity_providers(app))
        return app

    def test_loads_cached_settings_as_typed_values(self) -> None:
        self.assertEqual(self._setting("auto_load_session_name").value, "ALPHA")
        self.assertIs(self._setting("FG.DSAutoPause").value, True)
        self.assertIs(self._setting("FG.DSAutoSaveOnDisconnect").value, False)
        self.assertEqual(self._setting("FG.AutosaveInterval").value, 600)
        self.assertEqual(self._setting("FG.NetworkQuality").value, 3)
        self.assertIs(self._setting("FG.NoPower").value, True)
        self.assertEqual(self._setting("FG.SetGamePhase").value, 4)
        self.assertEqual(self._setting("FG.GiveItems").value, "IronPlate 100")
        self.assertEqual(self._setting("admin_password").value, "secret")

    def test_config_uses_instance_fields_for_connection_settings(self) -> None:
        cfg = Satisfactory_Config.model_validate(
            {
                "name": "satisfactory_alpha",
                "instance_key": "alpha",
                "friendly_name": "Satisfactory",
                "directory": self.temp_path / "server",
                "apps_dir": self.temp_path / "apps",
                "scope": "satisfactory",
                "api_host": "127.0.0.1",
                "join_port": 7777,
                "api_token": " token ",
                "admin_password": " secret ",
                "verify_ssl_chain_path": "{WD}/tls/chain.pem",
            }
        )

        self.assertEqual(cfg.effective_api_host, "127.0.0.1")
        self.assertEqual(cfg.effective_api_port, 7777)
        self.assertEqual(cfg.api_token, "token")
        self.assertEqual(cfg.admin_password, "secret")
        self.assertEqual(cfg.verify_ssl_chain_path, self.temp_path / "server" / "tls" / "chain.pem")

    def test_legacy_address_migrates_to_api_endpoint(self) -> None:
        cfg = Satisfactory_Config.model_validate(
            {
                "name": "satisfactory_alpha",
                "instance_key": "alpha",
                "friendly_name": "Satisfactory",
                "directory": self.temp_path / "server",
                "apps_dir": self.temp_path / "apps",
                "scope": "satisfactory",
                "address": "127.0.0.1",
                "admin_password": "secret",
            }
        )

        self.assertEqual(cfg.effective_api_host, "127.0.0.1")
        self.assertEqual(cfg.effective_api_port, 7777)

    def test_admin_password_is_required(self) -> None:
        with self.assertRaises(ValidationError):
            Satisfactory_Config.model_validate(
                {
                    "name": "satisfactory_alpha",
                    "instance_key": "alpha",
                    "friendly_name": "Satisfactory",
                    "directory": self.temp_path / "server",
                    "apps_dir": self.temp_path / "apps",
                    "scope": "satisfactory",
                }
            )

        with self.assertRaises(ValidationError):
            Satisfactory_Config.model_validate(
                {
                    "name": "satisfactory_alpha",
                    "instance_key": "alpha",
                    "friendly_name": "Satisfactory",
                    "directory": self.temp_path / "server",
                    "apps_dir": self.temp_path / "apps",
                    "scope": "satisfactory",
                    "admin_password": "   ",
                }
            )

    def test_parse_api_endpoint_handles_default_port_and_ipv6(self) -> None:
        self.assertEqual(_parse_api_endpoint("127.0.0.1"), ("127.0.0.1", 7777))
        self.assertEqual(_parse_api_endpoint("[::1]:7778"), ("::1", 7778))

    def test_start_command_uses_the_configured_join_port(self) -> None:
        self.assertEqual(_satisfactory_start_command(None), ["bash", "FactoryServer.sh", "-Port=7777"])
        self.assertEqual(_satisfactory_start_command(7778), ["bash", "FactoryServer.sh", "-Port=7778"])

    def test_server_state_parses_progress_fields_from_api_payload(self) -> None:
        state = SatisfactoryServerState.from_api_payload(
            {
                "serverGameState": {
                    "ActiveSessionName": "SERVER-SESSION",
                    "TechTier": "6",
                    "ActiveSchematic": "/Game/FactoryGame/Schematics/Schematic_6-3.Schematic_6-3_C",
                    "TotalGameDuration": "259200",
                }
            }
        )

        self.assertEqual(state.active_session_name, "SERVER-SESSION")
        self.assertEqual(state.tech_tier, 6)
        self.assertEqual(state.active_schematic, "/Game/FactoryGame/Schematics/Schematic_6-3.Schematic_6-3_C")
        self.assertEqual(state.total_game_duration, 259200)

    def test_detect_satisfactory_version_from_log(self) -> None:
        logs_dir = self.temp_path / "server" / "FactoryGame" / "Saved" / "Logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "FactoryGame.log").write_text(
            "LogInit: Build: ++FactoryGame+rel-main-1.1.0-CL-463028\n",
            encoding="utf-8",
        )
        sml_dir = self.temp_path / "server" / "FactoryGame" / "Mods" / "SML"
        sml_dir.mkdir(parents=True)
        (sml_dir / "SML.uplugin").write_text(
            json.dumps({"VersionName": "3.0.0"}),
            encoding="utf-8",
        )

        version = detect_satisfactory_version(directory=self.temp_path / "server", server_log=None)

        self.assertEqual(version, AppVersion(main="1.1.0", build=463028, framework="3.0.0", loader="sml"))

    def test_blueprint_upload_and_delete_tracks_owner_permissions(self) -> None:
        app = self._blueprint_app()
        upload_path = self.temp_path / "Awesome.sbp"
        upload_path.write_text("module", encoding="utf-8")
        config_upload_path = self.temp_path / "Awesome.sbpcfg"
        config_upload_path.write_text("config", encoding="utf-8")

        uploaded = app.upload_blueprint_file(
            session_name="Session Alpha",
            upload_name="Awesome.sbp",
            source_path=upload_path,
            actor_user_id=101,
            config_upload_name="Awesome.sbpcfg",
            config_source_path=config_upload_path,
        )

        self.assertEqual(uploaded.relative_path, "Shared/Awesome.sbp")
        self.assertEqual(uploaded.session_name, "Shared")
        self.assertIsNotNone(uploaded.config_file)
        listed = app.list_blueprint_files()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].uploaded_by_user_id, 101)
        self.assertIsNotNone(listed[0].config_file)
        self.assertEqual(listed[0].config_file.uploaded_by_user_id, 101)
        self.assertTrue((self.temp_path / "blueprints" / "Session Alpha").is_symlink())
        self.assertTrue((self.temp_path / "blueprints-shared" / "Shared" / "Awesome.sbp").exists())
        self.assertTrue((self.temp_path / "blueprints-shared" / "Shared" / "Awesome.sbpcfg").exists())

        with self.assertRaises(PermissionError):
            app.delete_blueprint_file(file_id=uploaded.id, actor_user_id=202, actor_is_sudo=False)

        deleted = app.delete_blueprint_file(file_id=uploaded.id, actor_user_id=101, actor_is_sudo=False)

        self.assertEqual(deleted.id, uploaded.id)
        self.assertFalse((self.temp_path / "blueprints-shared" / "Shared" / "Awesome.sbp").exists())
        self.assertFalse((self.temp_path / "blueprints-shared" / "Shared" / "Awesome.sbpcfg").exists())
        self.assertEqual(app.list_blueprint_files(), ())

    def test_blueprint_upload_rejects_mismatched_optional_config(self) -> None:
        app = self._blueprint_app()
        upload_path = self.temp_path / "Awesome.sbp"
        upload_path.write_text("module", encoding="utf-8")
        config_upload_path = self.temp_path / "Different.sbpcfg"
        config_upload_path.write_text("config", encoding="utf-8")

        with self.assertRaises(ValueError):
            app.upload_blueprint_file(
                session_name="Session Alpha",
                upload_name="Awesome.sbp",
                source_path=upload_path,
                actor_user_id=101,
                config_upload_name="Different.sbpcfg",
                config_source_path=config_upload_path,
            )

    def test_blueprint_upload_rolls_back_files_when_ownership_write_fails(self) -> None:
        app = self._blueprint_app()
        upload_path = self.temp_path / "Awesome.sbp"
        upload_path.write_text("module", encoding="utf-8")
        config_upload_path = self.temp_path / "Awesome.sbpcfg"
        config_upload_path.write_text("config", encoding="utf-8")

        with (
            patch.object(app._blueprint_ownership_store, "record_upload_batch", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            app.upload_blueprint_file(
                session_name="Session Alpha",
                upload_name="Awesome.sbp",
                source_path=upload_path,
                actor_user_id=101,
                config_upload_name="Awesome.sbpcfg",
                config_source_path=config_upload_path,
            )

        self.assertFalse((self.temp_path / "blueprints-shared" / "Shared" / "Awesome.sbp").exists())
        self.assertFalse((self.temp_path / "blueprints-shared" / "Shared" / "Awesome.sbpcfg").exists())
        self.assertEqual(app.list_blueprint_files(), ())
        self.assertEqual(app._blueprint_ownership_store.uploaded_by_user_id_by_relative_path(), {})

    def test_default_blueprint_session_name_is_shared(self) -> None:
        app = self._blueprint_app()
        app._players = SimpleNamespace(  # type: ignore[attr-defined]
            state=SatisfactoryServerState(
                active_session_name="Active Session",
                auto_load_session_name="Configured Session",
            )
        )
        app._settings = self.settings  # type: ignore[attr-defined]

        self.assertEqual(app.default_blueprint_session_name, "Shared")

    def test_blueprint_delete_requires_sudo_when_owner_is_unknown(self) -> None:
        app = self._blueprint_app()
        blueprint_path = self.temp_path / "blueprints-shared" / "Shared" / "Imported.sbp"
        config_path = self.temp_path / "blueprints-shared" / "Shared" / "Imported.sbpcfg"
        blueprint_path.parent.mkdir(parents=True, exist_ok=True)
        blueprint_path.write_text("module", encoding="utf-8")
        config_path.write_text("config", encoding="utf-8")

        with self.assertRaises(PermissionError):
            app.delete_blueprint_file(
                file_id="Session Beta/Imported.sbpcfg",
                actor_user_id=101,
                actor_is_sudo=False,
            )

        deleted = app.delete_blueprint_file(
            file_id="Session Beta/Imported.sbpcfg",
            actor_user_id=202,
            actor_is_sudo=True,
        )

        self.assertEqual(deleted.relative_path, "Shared/Imported.sbp")
        self.assertTrue(blueprint_path.exists())
        self.assertFalse(config_path.exists())

    def test_blueprint_list_and_delete_tolerate_legacy_filename_rules(self) -> None:
        app = self._blueprint_app()
        blueprint_path = self.temp_path / "blueprints-shared" / "Shared" / "Legacy .sbp"
        blueprint_path.parent.mkdir(parents=True, exist_ok=True)
        blueprint_path.write_text("module", encoding="utf-8")

        listed = app.list_blueprint_files()

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].relative_path, "Shared/Legacy .sbp")
        self.assertIsNone(listed[0].uploaded_by_user_id)

        with self.assertRaises(PermissionError):
            app.delete_blueprint_file(
                file_id="Session Legacy/Legacy .sbp",
                actor_user_id=101,
                actor_is_sudo=False,
            )

        deleted = app.delete_blueprint_file(
            file_id="Session Legacy/Legacy .sbp",
            actor_user_id=202,
            actor_is_sudo=True,
        )

        self.assertEqual(deleted.relative_path, "Shared/Legacy .sbp")
        self.assertFalse(blueprint_path.exists())

    def test_blueprint_migrates_existing_session_directory_into_shared_storage(self) -> None:
        app = self._blueprint_app()
        legacy_module = self.temp_path / "blueprints" / "Session Gamma" / "Migrated.sbp"
        legacy_config = self.temp_path / "blueprints" / "Session Gamma" / "Migrated.sbpcfg"
        legacy_module.parent.mkdir(parents=True, exist_ok=True)
        legacy_module.write_text("module", encoding="utf-8")
        legacy_config.write_text("config", encoding="utf-8")
        app._blueprint_ownership_store.record_upload(relative_path="Session Gamma/Migrated.sbp", actor_user_id=101)
        app._blueprint_ownership_store.record_upload(relative_path="Session Gamma/Migrated.sbpcfg", actor_user_id=101)

        listed = app.list_blueprint_files()

        self.assertEqual([entry.relative_path for entry in listed], ["Shared/Migrated.sbp"])
        self.assertTrue((self.temp_path / "blueprints" / "Session Gamma").is_symlink())
        self.assertTrue((self.temp_path / "blueprints-shared" / "Shared" / "Migrated.sbp").exists())
        self.assertTrue((self.temp_path / "blueprints-shared" / "Shared" / "Migrated.sbpcfg").exists())
        ownership = app._blueprint_ownership_store.uploaded_by_user_id_by_relative_path()
        self.assertEqual(ownership["Shared/Migrated.sbp"], 101)
        self.assertEqual(ownership["Shared/Migrated.sbpcfg"], 101)
        self.assertNotIn("Session Gamma/Migrated.sbp", ownership)

    def test_blueprint_ownership_store_preserves_existing_index_when_replace_fails(self) -> None:
        store_path = self.temp_path / "ownership.json"
        store = SatisfactoryBlueprintOwnershipStore(store_path)
        store.record_upload(relative_path="Shared/Original.sbp", actor_user_id=101)

        original_text = store_path.read_text(encoding="utf-8")

        with patch("pathlib.Path.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            store.record_upload(relative_path="Shared/Second.sbp", actor_user_id=202)

        self.assertEqual(store_path.read_text(encoding="utf-8"), original_text)
        self.assertFalse(store_path.with_name(f"{store_path.name}.tmp").exists())
        self.assertEqual(
            store.uploaded_by_user_id_by_relative_path(),
            {"Shared/Original.sbp": 101},
        )

    def test_blueprint_ownership_store_migrates_legacy_entries_without_clobbering_shared_entries(self) -> None:
        store = SatisfactoryBlueprintOwnershipStore(self.temp_path / "ownership.json")
        store.record_upload(relative_path="Session Gamma/Migrated.sbp", actor_user_id=101)
        store.record_upload(relative_path="Session Gamma/Migrated.sbpcfg", actor_user_id=101)
        store.record_upload(relative_path="Shared/Current.sbp", actor_user_id=202)

        store.migrate_legacy_relative_paths(
            legacy_to_shared_relative_path={
                "Session Gamma/Migrated.sbp": "Shared/Migrated.sbp",
                "Session Gamma/Migrated.sbpcfg": "Shared/Migrated.sbpcfg",
            }
        )

        self.assertEqual(
            store.uploaded_by_user_id_by_relative_path(),
            {
                "Shared/Current.sbp": 202,
                "Shared/Migrated.sbp": 101,
                "Shared/Migrated.sbpcfg": 101,
            },
        )

    async def test_player_session_matcher_emits_join_and_leave_notices(self) -> None:
        matcher = SatisfactoryPlayerSessionMatcher(self._player_session_app())
        login_line = (
            "[2026.06.18-07.58.00:510][638]LogNet: Login request: "
            "?Name=asdblackmea userId: Steam:2 "
            "(ForeignId=[Type=6 Handle=1 RepData=[C92A592D01001001]) platform: NULL"
        )
        join_line = "[2026.06.18-07.58.03:710][719]LogNet: Join succeeded: asdblackmea"
        close_line = (
            "[2026.06.18-08.11.05:233][ 61]LogNet: UNetConnection::Close: "
            "[UNetConnection] RemoteAddr: 124.187.226.10:55421, Name: IpConnection_2147397955, "
            "Driver: Name:GameNetDriver Def:GameNetDriver FGDSIpNetDriver_2147482163, IsServer: YES, "
            "PC: BP_PlayerController_C_2147397296, Owner: BP_PlayerController_C_2147397296, "
            "UniqueId: Steam:2 (ForeignId=[Type=6 Handle=1 RepData=[C92A592D01001001]), "
            "Channels: 352, Time: 2026.06.18-08.11.05"
        )

        with patch("apps.satisfactory.DC_Relay.add") as add_mock:
            await matcher.match(login_line)
            await matcher.match(join_line)

            self.assertEqual(add_mock.call_count, 1)
            join_message = add_mock.call_args_list[0].args[0]
            self.assertEqual(join_message.player, "asdblackmea")
            self.assertEqual(join_message.content, "asdblackmea joined Satisfactory")
            self.assertIsNotNone(join_message.notice)
            self.assertEqual(join_message.notice.action, PlayerSessionAction.JOINED)

            await matcher.match(close_line)

            self.assertEqual(add_mock.call_count, 2)
            leave_message = add_mock.call_args_list[1].args[0]
            self.assertEqual(leave_message.player, "asdblackmea")
            self.assertEqual(leave_message.content, "asdblackmea left Satisfactory")
            self.assertIsNotNone(leave_message.notice)
            self.assertEqual(leave_message.notice.action, PlayerSessionAction.LEFT)

    async def test_player_session_matcher_deduplicates_connection_close_lines(self) -> None:
        matcher = SatisfactoryPlayerSessionMatcher(self._player_session_app())
        login_line = (
            "[2026.06.18-07.58.00:510][638]LogNet: Login request: "
            "?Name=asdblackmea userId: Steam:2 "
            "(ForeignId=[Type=6 Handle=1 RepData=[C92A592D01001001]) platform: NULL"
        )
        join_line = "[2026.06.18-07.58.03:710][719]LogNet: Join succeeded: asdblackmea"
        close_line = (
            "[2026.06.18-08.11.05:233][ 61]LogNet: UNetConnection::Close: "
            "[UNetConnection] RemoteAddr: 124.187.226.10:55421, Name: IpConnection_2147397955, "
            "Driver: Name:GameNetDriver Def:GameNetDriver FGDSIpNetDriver_2147482163, IsServer: YES, "
            "PC: BP_PlayerController_C_2147397296, Owner: BP_PlayerController_C_2147397296, "
            "UniqueId: Steam:2 (ForeignId=[Type=6 Handle=1 RepData=[C92A592D01001001]), "
            "Channels: 352, Time: 2026.06.18-08.11.05"
        )
        remove_line = (
            "[2026.06.18-08.11.05:266][ 62]LogNet: UNetDriver::RemoveClientConnection - "
            "Removed address 124.187.226.10:55421 from MappedClientConnections for: [UNetConnection] "
            "RemoteAddr: 124.187.226.10:55421, Name: IpConnection_2147397955, Driver: "
            "Name:GameNetDriver Def:GameNetDriver FGDSIpNetDriver_2147482163, IsServer: YES, "
            "PC: BP_PlayerController_C_2147397296, Owner: BP_PlayerController_C_2147397296, "
            "UniqueId: Steam:2 (ForeignId=[Type=6 Handle=1 RepData=[C92A592D01001001])"
        )

        with patch("apps.satisfactory.DC_Relay.add") as add_mock:
            await matcher.match(login_line)
            await matcher.match(join_line)
            await matcher.match(close_line)
            await matcher.match(remove_line)

            self.assertEqual(add_mock.call_count, 2)
            leave_message = add_mock.call_args_list[1].args[0]
            self.assertIsNotNone(leave_message.notice)
            self.assertEqual(leave_message.notice.action, PlayerSessionAction.LEFT)

    async def test_player_session_matcher_ignores_close_without_join(self) -> None:
        matcher = SatisfactoryPlayerSessionMatcher(self._player_session_app())
        login_line = (
            "[2026.06.18-07.58.00:510][638]LogNet: Login request: "
            "?Name=asdblackmea userId: Steam:2 "
            "(ForeignId=[Type=6 Handle=1 RepData=[C92A592D01001001]) platform: NULL"
        )
        close_line = (
            "[2026.06.18-08.11.05:233][ 61]LogNet: UNetConnection::Close: "
            "[UNetConnection] RemoteAddr: 124.187.226.10:55421, Name: IpConnection_2147397955, "
            "Driver: Name:GameNetDriver Def:GameNetDriver FGDSIpNetDriver_2147482163, IsServer: YES, "
            "PC: BP_PlayerController_C_2147397296, Owner: BP_PlayerController_C_2147397296, "
            "UniqueId: Steam:2 (ForeignId=[Type=6 Handle=1 RepData=[C92A592D01001001]), "
            "Channels: 352, Time: 2026.06.18-08.11.05"
        )

        with patch("apps.satisfactory.DC_Relay.add") as add_mock:
            await matcher.match(login_line)
            await matcher.match(close_line)

            add_mock.assert_not_called()

    async def test_save_persists_cache_without_bridge_when_stopped(self) -> None:
        self._setting("FG.AutosaveInterval").update("900")
        self._setting("FG.NoPower").update("false")
        self._setting("admin_password").update("new-secret")

        payload = self.settings.save()

        self.assertEqual(payload["server_options"]["autosave_interval_seconds"], 900)
        self.assertIs(payload["advanced_game_settings"]["no_power"], False)
        self.assertNotIn("admin_password", payload)
        self.assertEqual(self.bridge.applied, [])
        self.assertEqual(self.cfg.admin_password, "new-secret")
        instances_payload = json.loads(self.instances_path.read_text(encoding="utf-8"))
        self.assertEqual(instances_payload["alpha"]["admin_password"], "new-secret")
        cache_payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cache_payload["server_options"]["autosave_interval_seconds"], 900)
        self.assertIs(cache_payload["advanced_game_settings"]["no_power"], False)

    async def test_refresh_and_apply_round_trip_through_bridge(self) -> None:
        self._running = True
        await self.settings.refresh_from_server()
        self.assertEqual(self._setting("auto_load_session_name").value, "SERVER-SESSION")
        self.assertIs(self._setting("FG.DSAutoPause").value, False)
        self.assertEqual(self._setting("FG.NetworkQuality").value, 1)
        self.assertIs(self._setting("FG.GiveAllTiers").value, True)
        self.assertEqual(self._setting("FG.SetGamePhase").value, 2)
        self.assertEqual(self._setting("admin_password").value, "secret")

        self._setting("auto_load_session_name").update("BETA")
        self._setting("FG.DSAutoPause").update("true")
        self._setting("FG.AutosaveInterval").update("120")
        self._setting("FG.NetworkQuality").update("3")
        self._setting("FG.GiveAllTiers").update("false")
        self._setting("FG.UnlockAllResearchSchematics").update("true")
        self._setting("FG.SetGamePhase").update("5")

        applied = await self.settings.apply_current_values()

        self.assertTrue(applied)
        self.assertEqual(len(self.bridge.applied), 1)
        snapshot = self.bridge.applied[0]
        self.assertEqual(snapshot.auto_load_session_name, "BETA")
        self.assertIs(snapshot.server_options.auto_pause, True)
        self.assertEqual(snapshot.server_options.autosave_interval_seconds, 120)
        self.assertEqual(snapshot.server_options.network_quality, SatisfactoryNetworkQuality.ULTRA)
        self.assertIs(snapshot.advanced_game_settings.give_all_tiers, False)
        self.assertIs(snapshot.advanced_game_settings.unlock_all_research_schematics, True)
        self.assertEqual(snapshot.advanced_game_settings.set_game_phase, 5)

    async def test_console_actions_use_http_api(self) -> None:
        app = self._console_app()
        actions = {action.key: action for action in app.console_actions}

        self.assertTrue(app.supports_console_actions)
        self.assertEqual(set(actions), {"save_game", "run_command", "shutdown"})

        raw_result = await execute_console_action(
            app=app,
            is_running=app.check_running,
            action=actions["run_command"],
            raw_value="server.GenerateAPIToken",
        )
        self.assertEqual(self.bridge.commands, ["server.GenerateAPIToken"])
        self.assertEqual(raw_result.source, ConsoleResponseSource.API)
        self.assertEqual(raw_result.text, "ran server.GenerateAPIToken")

        save_result = await execute_console_action(
            app=app,
            is_running=app.check_running,
            action=actions["save_game"],
            raw_value=None,
        )
        self.assertEqual(len(self.bridge.saved_games), 1)
        self.assertRegex(self.bridge.saved_games[0], r"^SERVER-SESSION-manual-\d{8}-\d{6}$")
        self.assertEqual(save_result.source, ConsoleResponseSource.API)
        self.assertIn(self.bridge.saved_games[0], save_result.summary)

        shutdown_result = await execute_console_action(
            app=app,
            is_running=app.check_running,
            action=actions["shutdown"],
            raw_value=None,
        )
        self.assertEqual(len(self.bridge.saved_games), 2)
        self.assertEqual(self.bridge.shutdown_count, 1)
        self.assertRegex(self.bridge.saved_games[1], r"^SERVER-SESSION-stop-\d{8}-\d{6}$")
        self.assertIn(self.bridge.saved_games[1], shutdown_result.summary)
        self.assertEqual(shutdown_result.source, ConsoleResponseSource.API)
        self.assertEqual(shutdown_result.text, "shutdown requested")

    async def test_console_actions_raise_runtime_error_when_api_is_unavailable(self) -> None:
        app = self._console_app()
        actions = {action.key: action for action in app.console_actions}
        app._bridge.run_command = AsyncMock(side_effect=OSError("offline"))  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "Satisfactory API is unavailable."):
            await execute_console_action(
                app=app,
                is_running=app.check_running,
                action=actions["run_command"],
                raw_value="server.GenerateAPIToken",
            )

    async def test_refresh_from_server_raises_runtime_error_when_api_is_unavailable(self) -> None:
        self._running = True
        self.bridge.read_settings = AsyncMock(side_effect=OSError("offline"))  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "Satisfactory API is unavailable."):
            await self.settings.refresh_from_server()

    async def test_warm_bridge_raises_when_api_never_becomes_ready(self) -> None:
        class _FailingBridge:
            async def query_server_state(self) -> SatisfactoryServerState:
                raise RuntimeError("offline")

        app = object.__new__(Satisfactory)
        app.friendly = "Satisfactory"
        app._bridge = _FailingBridge()
        app._players = Mock()

        with (
            patch("apps.satisfactory._API_READY_RETRIES", 2),
            patch("apps.satisfactory._API_READY_SLEEP_SECONDS", 0.0),
            self.assertRaisesRegex(TimeoutError, "Satisfactory API did not become ready after startup."),
        ):
            await app._warm_bridge()

        app._players.set_state.assert_not_called()

    async def test_runtime_api_waits_for_in_game_claim_then_activates_management(self) -> None:
        app = cast(Any, object.__new__(Satisfactory))
        app.friendly = "Satisfactory"
        app._running = True
        app._api_claim_pending = False
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        app._warm_bridge = AsyncMock(side_effect=(TimeoutError("unclaimed"), None))  # type: ignore[method-assign]
        app._settings = SimpleNamespace(
            apply_current_values=AsyncMock(),
            refresh_from_server=AsyncMock(),
        )
        app._players = SimpleNamespace(start=AsyncMock())
        app.register_enabled_activity_providers = Mock()  # type: ignore[method-assign]

        with patch("apps.satisfactory._API_RETRY_AFTER_UNAVAILABLE_SECONDS", 0.0):
            await app._wait_for_runtime_api()

        self.assertEqual(app._warm_bridge.await_count, 2)
        app._settings.apply_current_values.assert_awaited_once_with()
        app._settings.refresh_from_server.assert_awaited_once_with()
        app._players.start.assert_awaited_once_with()
        app.register_enabled_activity_providers.assert_called_once_with()
        self.assertFalse(app._api_claim_pending)

    async def test_start_returns_while_the_runtime_api_waits_for_a_claim(self) -> None:
        app = cast(Any, object.__new__(Satisfactory))
        app.name = "satisfactory_alpha"
        app.friendly = "Satisfactory"
        app._runtime_api_task = None
        app._player_session_matcher = SimpleNamespace(reset=Mock())
        app._std_launch = AsyncMock()
        app.check_running = Mock(return_value=True)  # type: ignore[method-assign]
        app.server_log = None
        app.process = SimpleNamespace(stdout=object())
        app.file_stdout = self.temp_path / "stdout.log"
        app._tail_matchers = set()
        app._running = False
        app._api_claim_pending = False
        api_wait_started = asyncio.Event()

        async def wait_for_runtime_api() -> None:
            api_wait_started.set()
            await asyncio.Event().wait()

        app._wait_for_runtime_api = wait_for_runtime_api  # type: ignore[method-assign]
        tailer = SimpleNamespace(start=AsyncMock())
        with patch("apps.satisfactory.Tailer", return_value=tailer):
            result = await Satisfactory.start(app)
            await api_wait_started.wait()

        self.assertTrue(result)
        self.assertTrue(app._running)
        app._std_launch.assert_awaited_once_with()
        tailer.start.assert_awaited_once_with(app._tail_matchers)
        await app._cancel_runtime_api_task()

    async def test_players_stop_cancels_cross_loop_poll_task(self) -> None:
        app = object.__new__(Satisfactory)
        app.name = "satisfactory_alpha"
        app.friendly = "Satisfactory"
        app._bridge = cast(SatisfactoryBridge, self.bridge)
        app._sync_provider_text = Mock()  # type: ignore[method-assign]
        players = SatisfactoryPlayers(app)
        task_ready = threading.Event()
        loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

        def _run_foreign_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop_holder["loop"] = loop
            try:
                loop.run_until_complete(players.start())
                task_ready.set()
                loop.run_forever()
            finally:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

        thread = threading.Thread(target=_run_foreign_loop, daemon=True)
        thread.start()
        self.assertTrue(task_ready.wait(timeout=1.0))
        foreign_loop = loop_holder["loop"]

        try:
            await players.stop()
            self.assertIsNone(players._players_task)
        finally:
            foreign_loop.call_soon_threadsafe(foreign_loop.stop)
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())

    def test_server_options_snapshot_parses_pending_server_options(self) -> None:
        snapshot = SatisfactoryServerOptionsSnapshot.from_api_payload(
            {
                "serverOptions": {
                    "FG.DSAutoPause": "False",
                    "FG.AutosaveInterval": "300.0",
                    "FG.NetworkQuality": "1",
                },
                "pendingServerOptions": {
                    "FG.DSAutoPause": "True",
                    "FG.NetworkQuality": "3",
                },
            },
        )

        self.assertIs(snapshot.auto_pause, True)
        self.assertEqual(snapshot.autosave_interval_seconds, 300)
        self.assertEqual(snapshot.network_quality, SatisfactoryNetworkQuality.ULTRA)

    def test_advanced_game_settings_snapshot_parses_api_payload(self) -> None:
        snapshot = SatisfactoryAdvancedGameSettingsSnapshot.from_api_payload(
            {
                "CreativeModeEnabled": True,
                "AdvancedGameSettings": {
                    "FG.NoPower": "True",
                    "FG.SetGamePhase": "6",
                    "FG.GiveItems": "Cable 200",
                    "FG.UnlockInstantAltRecipes": "False",
                },
            }
        )

        self.assertIs(snapshot.creative_mode_enabled, True)
        self.assertIs(snapshot.no_power, True)
        self.assertEqual(snapshot.set_game_phase, 6)
        self.assertEqual(snapshot.give_items, "Cable 200")
        self.assertIs(snapshot.unlock_instant_alt_recipes, False)

    def test_session_enumeration_snapshot_parses_satisfactory_save_timestamps(self) -> None:
        snapshot = SatisfactorySessionEnumerationSnapshot.from_api_payload(
            {
                "sessions": [
                    {
                        "sessionName": "SERVER-SESSION",
                        "saveHeaders": [
                            {
                                "saveName": "SERVER-SESSION_autosave_0.sav",
                                "sessionName": "SERVER-SESSION",
                                "saveDateTime": "2026.06.23-20.15.54",
                            }
                        ],
                    }
                ],
                "currentSessionIndex": 0,
            }
        )

        self.assertEqual(len(snapshot.sessions), 1)
        self.assertEqual(
            snapshot.sessions[0].save_headers[0].save_date_time,
            datetime(2026, 6, 23, 20, 15, 54, tzinfo=timezone.utc),
        )

    async def test_save_files_are_listed_via_https_api(self) -> None:
        app = self._console_app()

        saves = await app.list_save_files_async()

        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0].id, "saves/SERVER-SESSION/SERVER-SESSION_autosave_0.sav")
        self.assertEqual(saves[0].relative_path, "SERVER-SESSION/SERVER-SESSION_autosave_0.sav")
        self.assertEqual(app.save_file_roots[0].id, "saves")
        self.assertEqual(app.save_file_roots[0].label, "Server Saves")
        self.assertEqual(app.save_file_roots[0].path, _SATISFACTORY_SERVER_SAVE_ROOT)

    async def test_save_files_are_empty_when_server_is_not_running(self) -> None:
        app = self._console_app()
        app.check_running = Mock(return_value=False)  # type: ignore[method-assign]
        app._bridge.enumerate_sessions = AsyncMock(side_effect=AssertionError("bridge should not be used"))  # type: ignore[method-assign]

        saves = await app.list_save_files_async()

        self.assertEqual(saves, ())
        app._bridge.enumerate_sessions.assert_not_awaited()

    async def test_save_download_upload_and_delete_use_https_api(self) -> None:
        app = self._console_app()
        listed = await app.list_save_files_async()

        filename, content = await app.download_save_content(listed[0].id)

        self.assertEqual(filename, "SERVER-SESSION_autosave_0.sav")
        self.assertEqual(content, b"save:SERVER-SESSION_autosave_0.sav")

        upload_source = self.temp_path / "uploaded-save.sav"
        upload_source.write_bytes(b"satisfactory-save")

        uploaded = await app.upload_save_file_async(
            root_id="saves",
            upload_name="NewUpload.sav",
            source_path=upload_source,
        )

        self.assertEqual(self.bridge.uploaded_save_names, ["NewUpload.sav"])
        self.assertEqual(uploaded.relative_path, "UPLOADED-SESSION/NewUpload.sav")

        deleted = await app.delete_save_file_async(file_id=uploaded.id)

        self.assertEqual(self.bridge.deleted_save_names, ["NewUpload.sav"])
        self.assertEqual(deleted.id, uploaded.id)

    async def test_activity_providers_report_day_counter_and_stage(self) -> None:
        app = self._activity_app()
        entries = app.activity_provider_entries

        self.assertEqual(
            [(entry.provider_id, entry.label, entry.enabled) for entry in entries],
            [
                ("day", "Day Counter", True),
                ("stage", "Stage", True),
            ],
        )

        providers_by_id = {provider.provider_id: provider for provider in app.activity_providers}

        day_status = await providers_by_id["day"].get()
        stage_status = await providers_by_id["stage"].get()

        self.assertEqual(day_status, "D2")
        self.assertEqual(stage_status, "T3: Logistics Mk.2")

    def test_active_schematic_label_uses_base_game_display_name(self) -> None:
        self.assertEqual(
            _normalise_active_schematic_label(
                "/Game/FactoryGame/Schematics/Progression/Schematic_8-2.Schematic_8-2_C"
            ),
            "Advanced Aluminum Production",
        )

    def test_active_schematic_label_preserves_unknown_schematic_fallback(self) -> None:
        self.assertEqual(
            _normalise_active_schematic_label("/Example/Modded_Schematic.Modded_Schematic_C"),
            "Modded Schematic",
        )

    async def test_shared_terminate_cancels_stuck_stderr_task(self) -> None:
        app = object.__new__(_DummyApp)
        app.name = "dummy"
        app.proc_name = ""
        app.proc_cmd = []
        app.process = cast(subprocess.Popen[str], _DummyProcess())
        app._stderr_task = cast(asyncio.Task[None], asyncio.create_task(asyncio.Event().wait()))

        await app._terminate()

        self.assertIsNone(app.process)
        stderr_task = app._stderr_task
        self.assertIsNotNone(stderr_task)
        self.assertTrue(stderr_task.cancelled())

    async def test_shared_terminate_handles_process_cleared_during_wait(self) -> None:
        class _ClearingProcess(_DummyProcess):
            def __init__(self, app: _DummyApp) -> None:
                super().__init__()
                self._app = app

            def wait(self, timeout: float | None = None) -> int:
                self._app.process = None
                return super().wait(timeout)

        app = object.__new__(_DummyApp)
        app.name = "dummy"
        app.proc_name = ""
        app.proc_cmd = []
        app.process = cast(subprocess.Popen[str], _ClearingProcess(app))
        app._stderr_task = None

        await app._terminate()

        self.assertIsNone(app.process)

    def test_leftover_process_cleanup_matches_case_insensitively_and_requires_all_command_parts(self) -> None:
        class _Process:
            def __init__(self, *, name: str, pid: int, command_line: list[str]) -> None:
                self.info = {"name": name, "pid": pid, "cmdline": command_line}
                self.terminate_calls = 0
                self.wait_timeouts: list[int] = []
                self.kill_calls = 0

            def terminate(self) -> None:
                self.terminate_calls += 1

            def wait(self, timeout: int) -> None:
                self.wait_timeouts.append(timeout)

            def kill(self) -> None:
                self.kill_calls += 1

        matching_process = _Process(
            name="factoryserver-linux-shipping",
            pid=123,
            command_line=["./FactoryServer-Linux-Shipping", "--server", "FactoryGame"],
        )
        unrelated_process = _Process(
            name="factoryserver-linux-shipping",
            pid=456,
            command_line=["./FactoryServer-Linux-Shipping", "--client"],
        )
        app = object.__new__(_DummyApp)
        app.proc_name = "FactoryServer-Linux-Shipping"
        app.proc_cmd = ["FactoryServer-Linux-Shipping", "--server"]

        with patch(
            "apps._app.psutil.process_iter",
            return_value=(matching_process, unrelated_process),
        ), patch("apps._app.subprocess.run") as run:
            app._terminate_leftover_processes_sync()

        self.assertEqual(matching_process.terminate_calls, 1)
        self.assertEqual(matching_process.wait_timeouts, [10])
        self.assertEqual(matching_process.kill_calls, 0)
        self.assertEqual(unrelated_process.terminate_calls, 0)
        run.assert_not_called()

    def test_leftover_process_cleanup_escalates_after_termination_timeout(self) -> None:
        class _UnresponsiveProcess:
            info = {
                "name": "factoryserver-linux-shipping",
                "pid": 123,
                "cmdline": ["./FactoryServer-Linux-Shipping", "--server"],
            }

            def __init__(self) -> None:
                self.terminate_calls = 0
                self.wait_timeouts: list[int] = []
                self.kill_calls = 0

            def terminate(self) -> None:
                self.terminate_calls += 1

            def wait(self, timeout: int) -> None:
                self.wait_timeouts.append(timeout)
                if len(self.wait_timeouts) == 1:
                    raise psutil.TimeoutExpired(timeout, pid=123)

            def kill(self) -> None:
                self.kill_calls += 1

        process = _UnresponsiveProcess()
        app = object.__new__(_DummyApp)
        app.proc_name = "FactoryServer-Linux-Shipping"
        app.proc_cmd = ["FactoryServer-Linux-Shipping", "--server"]

        with patch("apps._app.psutil.process_iter", return_value=(process,)):
            app._terminate_leftover_processes_sync()

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_timeouts, [10, 5])
        self.assertEqual(process.kill_calls, 1)

    async def test_shared_terminate_cancels_cross_loop_stderr_task(self) -> None:
        task_ready = threading.Event()
        foreign_task_holder: dict[str, asyncio.Task[None]] = {}

        def _run_foreign_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                foreign_task_holder["task"] = loop.create_task(asyncio.Event().wait())
                task_ready.set()
                loop.run_forever()
            finally:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

        thread = threading.Thread(target=_run_foreign_loop, daemon=True)
        thread.start()
        self.assertTrue(task_ready.wait(timeout=1.0))
        foreign_task = foreign_task_holder["task"]

        app = object.__new__(_DummyApp)
        app.name = "dummy"
        app.proc_name = ""
        app.proc_cmd = []
        app.process = cast(subprocess.Popen[str], _DummyProcess())
        app._stderr_task = foreign_task

        try:
            await app._terminate()
            deadline = asyncio.get_running_loop().time() + 1.0
            while not foreign_task.done():
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("cross-loop stderr task was not cancelled")
                await asyncio.sleep(0.05)
            self.assertTrue(foreign_task.cancelled())
        finally:
            foreign_task.get_loop().call_soon_threadsafe(foreign_task.get_loop().stop)
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
