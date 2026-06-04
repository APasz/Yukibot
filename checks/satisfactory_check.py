from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from pydantic import ValidationError

from apps._app import App
from apps._config import AppVersion
from apps._settings import Setting
from apps.satisfactory import (
    Satisfactory,
    SatisfactoryBlueprintOwnershipStore,
    Satisfactory_Config,
    SatisfactoryNetworkQuality,
    SatisfactoryServerState,
    SatisfactorySettings,
    SatisfactorySettingsSnapshot,
    _parse_api_endpoint,
    detect_satisfactory_version,
)


class _FakeBridge:
    def __init__(self) -> None:
        self.snapshot = SatisfactorySettingsSnapshot(
            auto_load_session_name="SERVER-SESSION",
            auto_pause=False,
            auto_save_on_disconnect=True,
            autosave_interval_seconds=300,
            send_gameplay_data=True,
            network_quality=SatisfactoryNetworkQuality.MEDIUM,
        )
        self.state = SatisfactoryServerState(
            active_session_name="SERVER-SESSION",
            auto_load_session_name="SERVER-SESSION",
            num_connected_players=2,
            player_limit=8,
            is_game_running=True,
        )
        self.applied: list[SatisfactorySettingsSnapshot] = []

    async def read_settings(self) -> SatisfactorySettingsSnapshot:
        return self.snapshot.model_copy(deep=True)

    async def apply_settings(self, settings: SatisfactorySettingsSnapshot) -> None:
        copied = settings.model_copy(deep=True)
        self.applied.append(copied)
        self.snapshot = copied

    async def query_server_state(self) -> SatisfactoryServerState:
        return self.state.model_copy(deep=True)


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
    "network_quality": 3
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

    def test_loads_cached_settings_as_typed_values(self) -> None:
        self.assertEqual(self._setting("auto_load_session_name").value, "ALPHA")
        self.assertIs(self._setting("FG.DSAutoPause").value, True)
        self.assertIs(self._setting("FG.DSAutoSaveOnDisconnect").value, False)
        self.assertEqual(self._setting("FG.AutosaveInterval").value, 600)
        self.assertEqual(self._setting("FG.NetworkQuality").value, 3)
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

        self.assertEqual(version, AppVersion(main="1.1.0", framework="3.0.0", loader="sml"))

    def test_blueprint_upload_and_delete_tracks_owner_permissions(self) -> None:
        app = self._blueprint_app()
        upload_path = self.temp_path / "Awesome.sbp"
        upload_path.write_text("module", encoding="utf-8")

        uploaded = app.upload_blueprint_file(
            session_name="Session Alpha",
            upload_name="Awesome.sbp",
            source_path=upload_path,
            actor_user_id=101,
        )

        self.assertEqual(uploaded.relative_path, "Session Alpha/Awesome.sbp")
        listed = app.list_blueprint_files()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].uploaded_by_user_id, 101)

        with self.assertRaises(PermissionError):
            app.delete_blueprint_file(file_id=uploaded.id, actor_user_id=202, actor_is_sudo=False)

        deleted = app.delete_blueprint_file(file_id=uploaded.id, actor_user_id=101, actor_is_sudo=False)

        self.assertEqual(deleted.id, uploaded.id)
        self.assertFalse((self.temp_path / "blueprints" / "Session Alpha" / "Awesome.sbp").exists())
        self.assertEqual(app.list_blueprint_files(), ())

    def test_blueprint_delete_requires_sudo_when_owner_is_unknown(self) -> None:
        app = self._blueprint_app()
        blueprint_path = self.temp_path / "blueprints" / "Session Beta" / "Imported.sbpcfg"
        blueprint_path.parent.mkdir(parents=True, exist_ok=True)
        blueprint_path.write_text("config", encoding="utf-8")

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

        self.assertEqual(deleted.relative_path, "Session Beta/Imported.sbpcfg")
        self.assertFalse(blueprint_path.exists())

    async def test_save_persists_cache_without_bridge_when_stopped(self) -> None:
        self._setting("FG.AutosaveInterval").update("900")
        self._setting("admin_password").update("new-secret")

        payload = self.settings.save()

        self.assertEqual(payload["autosave_interval_seconds"], 900)
        self.assertNotIn("admin_password", payload)
        self.assertEqual(self.bridge.applied, [])
        self.assertEqual(self.cfg.admin_password, "new-secret")
        instances_payload = json.loads(self.instances_path.read_text(encoding="utf-8"))
        self.assertEqual(instances_payload["alpha"]["admin_password"], "new-secret")
        self.assertIn('"autosave_interval_seconds": 900', self.cache_path.read_text(encoding="utf-8"))

    async def test_refresh_and_apply_round_trip_through_bridge(self) -> None:
        await self.settings.refresh_from_server()
        self.assertEqual(self._setting("auto_load_session_name").value, "SERVER-SESSION")
        self.assertIs(self._setting("FG.DSAutoPause").value, False)
        self.assertEqual(self._setting("FG.NetworkQuality").value, 1)
        self.assertEqual(self._setting("admin_password").value, "secret")

        self._running = True
        self._setting("auto_load_session_name").update("BETA")
        self._setting("FG.DSAutoPause").update("true")
        self._setting("FG.AutosaveInterval").update("120")
        self._setting("FG.NetworkQuality").update("3")

        applied = await self.settings.apply_current_values()

        self.assertTrue(applied)
        self.assertEqual(len(self.bridge.applied), 1)
        snapshot = self.bridge.applied[0]
        self.assertEqual(snapshot.auto_load_session_name, "BETA")
        self.assertIs(snapshot.auto_pause, True)
        self.assertEqual(snapshot.autosave_interval_seconds, 120)
        self.assertEqual(snapshot.network_quality, SatisfactoryNetworkQuality.ULTRA)

    def test_snapshot_parses_pending_server_options(self) -> None:
        state = SatisfactoryServerState(auto_load_session_name="GAMMA")
        snapshot = SatisfactorySettingsSnapshot.from_api_payloads(
            state,
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

        self.assertEqual(snapshot.auto_load_session_name, "GAMMA")
        self.assertIs(snapshot.auto_pause, True)
        self.assertEqual(snapshot.autosave_interval_seconds, 300)
        self.assertEqual(snapshot.network_quality, SatisfactoryNetworkQuality.ULTRA)

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
