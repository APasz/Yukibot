from __future__ import annotations

import asyncio
import io
import json
import math
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, cast
from unittest.mock import AsyncMock, Mock, patch

import hikari

from _manager import AppInstallInput, App_Manager
from apps._app import AppPortClaim, AppRuntimeFault, AppRuntimeFaultCode, AppRuntimeFaultKind, NetworkProtocol
from apps._config import App_Config, AppVersion, SteamUpdateBranch, SteamUpdateConfig
from apps._mod_catalog import ModSourceKind
from apps._updater import SteamCmd_Update_Manager
from apps._steam import STEAM_GAME_SERVER_LOGIN_TOKEN_MANAGEMENT_URL
from apps.gmod import (
    GMOD_DEFAULT_GAMEMODE,
    GMOD_DEFAULT_MAX_PLAYERS,
    GMOD_DEFAULT_PORT,
    GMOD_DEFAULT_STARTUP_MAP,
    GMOD_DEFAULT_WORKSHOP_AUTO_UPDATE,
    GMOD_DEFAULT_INSTALL_SUBFOLDER,
    GMOD_MANAGE_EMBED_COLOR,
    GMOD_X64_STEAM_BRANCH,
    STEAM_APP_ID,
    STEAM_GAME_APP_ID,
    STEAM_UPDATE_PRESET,
    Gmod,
    Gmod_Settings,
    _GmodStartupStatusPhase,
    _advance_gmod_startup_status_phase,
    _read_gmod_game_server_login_token,
    ensure_gmod_managed_files,
    gmod_game_server_login_token_path,
    gmod_server_info_responds,
    gmod_server_config_path,
    gmod_settings_path,
    gmod_start_command,
    gmod_workshop_manifest_path,
    render_gmod_workshop_manifest,
    resolve_gmod_game_port,
    sync_gmod_workshop_manifest,
)
from cmd_app_manage import AppManageMode, AppManageState
from cmd_app_manage_render import build_settings_view
from node_api.app_installer import NodeAppInstallInputKind, NodeAppInstallRequest, NodeAppInstallerService
from node_api.service import NodeApiService


def _write_gmod_steam_manifest(
    directory: Path,
    *,
    build_id: int,
    branch_id: str = GMOD_X64_STEAM_BRANCH,
) -> None:
    manifest_path = directory / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    user_config: tuple[str, ...] = ()
    if branch_id != "public":
        user_config = (
            '    "UserConfig"',
            "    {",
            f'        "betakey" "{branch_id}"',
            "    }",
        )
    manifest_path.write_text(
        "\n".join(
            (
                '"AppState"',
                "{",
                f'    "appid" "{STEAM_APP_ID}"',
                f'    "buildid" "{build_id}"',
                *user_config,
                "}",
            )
        ),
        encoding="utf-8",
    )


def _write_gmod_x64_runtime(directory: Path) -> None:
    (directory / "srcds_run_x64").write_text("#!/bin/sh\n", encoding="utf-8")
    binary = directory / "bin" / "linux64" / "srcds"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("binary\n", encoding="utf-8")


_SourceQueryResponder = Callable[[bytes, int], bytes | None]


class _SourceQueryProtocol(asyncio.DatagramProtocol):
    def __init__(self, responder: _SourceQueryResponder) -> None:
        self._responder = responder
        self.requests: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.requests.append(data)
        response = self._responder(data, len(self.requests))
        if response is not None and self.transport is not None:
            self.transport.sendto(response, addr)


async def _create_source_query_responder(
    responder: _SourceQueryResponder,
) -> tuple[asyncio.DatagramTransport, _SourceQueryProtocol, int]:
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _SourceQueryProtocol(responder),
        local_addr=("127.0.0.1", 0),
    )
    server_address = cast(tuple[str, int], transport.get_extra_info("sockname"))
    return cast(asyncio.DatagramTransport, transport), cast(_SourceQueryProtocol, protocol), server_address[1]


class GmodSettingsTests(unittest.TestCase):
    def test_default_launch_settings_are_persisted_with_sensible_values(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)

            settings = Gmod_Settings(gmod_settings_path(directory))

            self.assertEqual(settings.startup_map, GMOD_DEFAULT_STARTUP_MAP)
            self.assertEqual(settings.gamemode, GMOD_DEFAULT_GAMEMODE)
            self.assertEqual(settings.max_players, GMOD_DEFAULT_MAX_PLAYERS)
            self.assertIsNone(settings.workshop_collection_id)
            self.assertEqual(settings.workshop_auto_update, GMOD_DEFAULT_WORKSHOP_AUTO_UPDATE)
            self.assertEqual(settings.client_content_workshop_ids, ())
            self.assertTrue(gmod_server_config_path(directory).is_file())

    def test_custom_launch_settings_drive_the_generated_command(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)
            settings = Gmod_Settings(gmod_settings_path(directory))
            settings_by_key = {setting.key: setting for setting in settings.options}
            settings_by_key["startup_map"].update("gm_flatgrass")
            settings_by_key["gamemode"].update("darkrp")
            settings_by_key["max_players"].update("32")
            settings.save()

            reloaded = Gmod_Settings(gmod_settings_path(directory))
            command = gmod_start_command(
                port=27030,
                max_players=reloaded.max_players,
                gamemode=reloaded.gamemode,
                startup_map=reloaded.startup_map,
                game_server_login_token=token,
            )

        self.assertIn("-norestart", command)
        self.assertEqual(
            command,
            [
                "./srcds_run_x64",
                "-norestart",
                "-console",
                "-game",
                "garrysmod",
                "-ip",
                "0.0.0.0",
                "-port",
                "27030",
                "+maxplayers",
                "32",
                "+gamemode",
                "darkrp",
                "+map",
                "gm_flatgrass",
                "+sv_setsteamaccount",
                token,
            ],
        )

    def test_launch_settings_reject_unsafe_map_and_gamemode_names(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)
            settings = Gmod_Settings(gmod_settings_path(directory))
            settings_by_key = {setting.key: setting for setting in settings.options}

            with self.assertRaisesRegex(ValueError, "not valid"):
                settings_by_key["startup_map"].update("gm construct")
            with self.assertRaisesRegex(ValueError, "not valid"):
                settings_by_key["gamemode"].update("sandbox; quit")

    def test_existing_launch_settings_load_workshop_defaults(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)
            gmod_settings_path(directory).write_text(
                json.dumps(
                    {
                        "startup_map": "gm_flatgrass",
                        "gamemode": "darkrp",
                        "max_players": 32,
                    }
                ),
                encoding="utf-8",
            )

            settings = Gmod_Settings(gmod_settings_path(directory))
            settings.save()
            persisted = cast(
                dict[str, object],
                json.loads(gmod_settings_path(directory).read_text(encoding="utf-8")),
            )

        self.assertEqual(settings.startup_map, "gm_flatgrass")
        self.assertEqual(settings.gamemode, "darkrp")
        self.assertEqual(settings.max_players, 32)
        self.assertIsNone(settings.workshop_collection_id)
        self.assertTrue(settings.workshop_auto_update)
        self.assertEqual(settings.client_content_workshop_ids, ())
        self.assertEqual(persisted["workshop_collection_id"], "")
        self.assertTrue(persisted["workshop_auto_update"])
        self.assertEqual(persisted["client_content_workshop_ids"], [])

    def test_workshop_settings_generate_safe_launch_and_client_download_manifest(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)
            settings = Gmod_Settings(gmod_settings_path(directory))
            settings_by_key = {setting.key: setting for setting in settings.options}
            settings_by_key["workshop_collection_id"].update(" 123456789 ")
            settings_by_key["workshop_auto_update"].update("false")
            settings_by_key["client_content_workshop_ids"].update("234567890, 345678901\n456789012")
            settings.save()

            persisted = cast(
                dict[str, object],
                json.loads(gmod_settings_path(directory).read_text(encoding="utf-8")),
            )
            reloaded = Gmod_Settings(gmod_settings_path(directory))
            command = gmod_start_command(
                port=27030,
                max_players=reloaded.max_players,
                gamemode=reloaded.gamemode,
                startup_map=reloaded.startup_map,
                game_server_login_token=token,
                workshop_collection_id=reloaded.workshop_collection_id,
                workshop_auto_update=reloaded.workshop_auto_update,
            )
            manifest_path = sync_gmod_workshop_manifest(directory, reloaded.client_content_workshop_ids)
            manifest = manifest_path.read_text(encoding="utf-8")

        self.assertEqual(persisted["workshop_collection_id"], "123456789")
        self.assertFalse(persisted["workshop_auto_update"])
        self.assertEqual(
            persisted["client_content_workshop_ids"],
            ["234567890", "345678901", "456789012"],
        )
        self.assertEqual(reloaded.workshop_collection_id, "123456789")
        self.assertFalse(reloaded.workshop_auto_update)
        self.assertEqual(reloaded.client_content_workshop_ids, ("234567890", "345678901", "456789012"))
        self.assertIn("-norestart", command)
        self.assertEqual(
            command,
            [
                "./srcds_run_x64",
                "-norestart",
                "-console",
                "-game",
                "garrysmod",
                "-ip",
                "0.0.0.0",
                "-port",
                "27030",
                "+maxplayers",
                "16",
                "+host_workshop_collection",
                "123456789",
                "+host_workshop_autoupdate",
                "0",
                "+gamemode",
                "sandbox",
                "+map",
                "gm_construct",
                "+sv_setsteamaccount",
                token,
            ],
        )
        self.assertEqual(manifest_path, gmod_workshop_manifest_path(directory))
        self.assertEqual(
            manifest,
            "-- Generated by Yukibot. Configure client content Workshop IDs in Yukibot; do not edit this file.\n"
            "if not SERVER then return end\n"
            "\n"
            'resource.AddWorkshop("234567890")\n'
            'resource.AddWorkshop("345678901")\n'
            'resource.AddWorkshop("456789012")\n',
        )

    def test_workshop_ids_reject_injection_and_duplicates(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ensure_gmod_managed_files(directory)
            settings = Gmod_Settings(gmod_settings_path(directory))
            settings_by_key = {setting.key: setting for setting in settings.options}

            with self.assertRaisesRegex(ValueError, "Workshop ID"):
                settings_by_key["workshop_collection_id"].update("123; quit")
            with self.assertRaisesRegex(ValueError, "duplicates"):
                settings_by_key["client_content_workshop_ids"].update("123456789, 123456789")
            with self.assertRaisesRegex(ValueError, "Workshop ID"):
                render_gmod_workshop_manifest(('123456789"); RunString("bad")',))

    def test_workshop_manifest_excludes_its_configured_collection_id(self) -> None:
        manifest = render_gmod_workshop_manifest(
            ("100", "200"),
            collection_id="100",
        )

        self.assertNotIn('resource.AddWorkshop("100")', manifest)
        self.assertIn('resource.AddWorkshop("200")', manifest)


class GmodReadinessTests(unittest.TestCase):
    def test_console_status_probe_requires_a_complete_dedicated_server_status(self) -> None:
        phase = _GmodStartupStatusPhase.WAITING_FOR_HOSTNAME
        phase = _advance_gmod_startup_status_phase(phase, "players : 0 humans, 0 bots (16 max)\n")
        self.assertIs(phase, _GmodStartupStatusPhase.WAITING_FOR_HOSTNAME)

        for line in (
            "\x1b[32mhostname: GMod Alpha\x1b[0m\n",
            "version : 2026.09 secure\n",
            "udp/ip  : 0.0.0.0:27015 (public ip: 51.79.162.4)\n",
            "map     : gm_construct at: 0 x, 0 y, 0 z\n",
            "players : 0 humans, 0 bots (16 max)\n",
        ):
            phase = _advance_gmod_startup_status_phase(phase, line)

        self.assertIs(phase, _GmodStartupStatusPhase.READY)

    def test_source_info_probe_handles_a_challenge_response(self) -> None:
        challenge = b"\x01\x02\x03\x04"

        def respond(_request: bytes, request_count: int) -> bytes:
            if request_count == 1:
                return b"\xff\xff\xff\xffA" + challenge
            return b"\xff\xff\xff\xffI\x11"

        async def query() -> tuple[bool, _SourceQueryProtocol]:
            transport, protocol, port = await _create_source_query_responder(respond)
            try:
                responding = await gmod_server_info_responds(port=port, timeout_seconds=0.5)
                return responding, protocol
            finally:
                transport.close()

        responding, protocol = asyncio.run(query())

        self.assertTrue(responding)
        self.assertEqual(len(protocol.requests), 2)
        self.assertTrue(protocol.requests[1].endswith(challenge))

    def test_source_info_probe_rejects_a_truncated_response(self) -> None:
        def respond(_request: bytes, _request_count: int) -> bytes:
            return b"\xff\xff\xff\xffI"

        async def query() -> bool:
            transport, _, port = await _create_source_query_responder(respond)
            try:
                return await gmod_server_info_responds(port=port, timeout_seconds=0.5)
            finally:
                transport.close()

        self.assertFalse(asyncio.run(query()))

    def test_source_info_probe_rejects_non_finite_timeouts(self) -> None:
        async def query() -> None:
            for timeout_seconds in (math.nan, math.inf, -math.inf):
                with self.assertRaisesRegex(ValueError, "finite"):
                    await gmod_server_info_responds(port=GMOD_DEFAULT_PORT, timeout_seconds=timeout_seconds)

        asyncio.run(query())


class GmodIntegrationTests(unittest.TestCase):
    @staticmethod
    def _config(
        directory: Path,
        *,
        port: int | None = None,
        steam_update: bool = True,
        version: AppVersion | None = None,
    ) -> App_Config:
        return App_Config(
            name="gmod_alpha",
            instance_key="alpha",
            friendly_name="GMod Alpha",
            directory=directory,
            apps_dir=directory / "apps" / "gmod",
            join_port=port,
            scope="gmod",
            version=version,
            steam_update=STEAM_UPDATE_PRESET.build_config() if steam_update else None,
        )

    @classmethod
    def _app(
        cls,
        directory: Path,
        *,
        port: int | None = None,
        version: AppVersion | None = None,
    ) -> Gmod:
        with patch("apps._updater.resolve_steamcmd_command_prefix", return_value=("steamcmd",)):
            return Gmod(Mock(), Mock(), cls._config(directory, port=port, version=version))

    def test_fresh_install_loads_and_displays_the_steam_manifest_build(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        request = NodeAppInstallerService._create_request(
            NodeAppInstallRequest(
                scope="gmod",
                instance_key="alpha",
                friendly_name="GMod Alpha",
                subfolder="gmod-alpha",
                steam_branch_id=GMOD_X64_STEAM_BRANCH,
                inputs={AppInstallInput.GAME_SERVER_LOGIN_TOKEN: token},
            )
        )
        self.assertIsNone(request.initial_version)
        self.assertTrue(request.clear_template_version)

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_gmod_steam_manifest(directory, build_id=1234567)

            app = self._app(directory)
            with patch("apps._updater.steam_update_branch_cache_is_fresh", return_value=True):
                update_info = app.update_info

        self.assertEqual(app.cfg.version, AppVersion(steam_branch=GMOD_X64_STEAM_BRANCH, steam_build=1234567))
        self.assertEqual(app.version_display, f"Steam {GMOD_X64_STEAM_BRANCH} build 1234567")
        self.assertNotIn("0.0", app.version_display)
        assert update_info is not None
        self.assertEqual(update_info.installed_build_id, 1234567)
        self.assertEqual(update_info.installed_branch_id, GMOD_X64_STEAM_BRANCH)

    def test_legacy_placeholder_is_not_reported_without_a_manifest(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(
                Path(temporary_directory),
                version=AppVersion(main="0.0", steam_branch="public"),
            )

        self.assertEqual(app.version_display, "none")

    def test_update_and_verify_refresh_the_displayed_steam_build(self) -> None:
        async def _run() -> None:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_steam_manifest(directory, build_id=100)
                app = self._app(directory, version=AppVersion(main="0.0"))
                assert isinstance(app.updater, SteamCmd_Update_Manager)
                self.assertEqual(app.version_display, f"Steam {GMOD_X64_STEAM_BRANCH} build 100")
                builds = iter((200, 300))

                async def _run_steamcmd(*, branch: SteamUpdateBranch, validate: bool) -> bool:
                    del branch, validate
                    _write_gmod_steam_manifest(directory, build_id=next(builds))
                    return True

                with patch.object(app.updater, "_run_steamcmd", new=_run_steamcmd):
                    update_result = await app.updater.update_selected()
                    self.assertEqual(app.version_display, f"Steam {GMOD_X64_STEAM_BRANCH} build 200")
                    verify_result = await app.updater.verify_selected()

                with patch("apps._updater.steam_update_branch_cache_is_fresh", return_value=True):
                    update_info = app.update_info
                self.assertEqual(update_result.version_text, f"Steam {GMOD_X64_STEAM_BRANCH} build 200")
                self.assertEqual(verify_result.version_text, f"Steam {GMOD_X64_STEAM_BRANCH} build 300")
                self.assertEqual(app.cfg.version, AppVersion(steam_branch=GMOD_X64_STEAM_BRANCH, steam_build=300))
                self.assertEqual(app.version_display, f"Steam {GMOD_X64_STEAM_BRANCH} build 300")
                self.assertNotIn("0.0", app.version_display)
                assert update_info is not None
                self.assertEqual(update_info.installed_build_id, 300)
                self.assertEqual(update_info.installed_branch_id, GMOD_X64_STEAM_BRANCH)

        asyncio.run(_run())

    def test_port_claims_reserve_only_the_configured_udp_game_port(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory), port=27031)

            claims = app.listening_port_claims

        self.assertEqual(
            claims,
            (
                AppPortClaim(
                    protocol=NetworkProtocol.UDP,
                    port=27031,
                    purpose="game server",
                ),
            ),
        )
        self.assertEqual(resolve_gmod_game_port(None), GMOD_DEFAULT_PORT)
        with self.assertRaisesRegex(TypeError, "integer"):
            resolve_gmod_game_port(True)

    def test_manage_embed_color_uses_gmod_blue(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory))

        self.assertEqual(GMOD_MANAGE_EMBED_COLOR, 0x1194F0)
        self.assertEqual(app.manage_embed_color, GMOD_MANAGE_EMBED_COLOR)

    def test_workshop_ids_use_a_valid_settings_input_representation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory))
            settings_manager = app.settings
            assert settings_manager is not None
            setting = settings_manager.app.get_setting("client_content_workshop_ids")
            assert setting is not None

            settings_manager.update_setting(
                actor_user_id=42,
                setting=setting,
                value="234567890 345678901",
            )

            current_input = settings_manager.current_input_value(setting, actor_user_id=42)

        self.assertEqual(current_input, "234567890, 345678901")

    def test_gmod_registers_the_read_only_steam_workshop_mod_source(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory))

        catalog = app.has_mod_catalog
        self.assertEqual(tuple(source.kind for source in catalog.sources), (ModSourceKind.STEAM_WORKSHOP,))

    def test_workshop_settings_are_not_exposed_on_the_ordinary_settings_surface(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory))
            settings_manager = app.settings
            assert settings_manager is not None

            setting_keys = tuple(setting.key for setting in settings_manager.app.settings_page_options)
            management_view = build_settings_view(
                app=app,
                state=AppManageState(mode=AppManageMode.SETTINGS, page=0, app_name=app.name),
                acl=cast(Any, Mock()),
                actor_user_id=42,
            )
            management_setting_keys = tuple(setting.key for setting in management_view.settings.visible)

        self.assertNotIn("workshop_collection_id", setting_keys)
        self.assertNotIn("workshop_auto_update", setting_keys)
        self.assertNotIn("client_content_workshop_ids", setting_keys)
        self.assertNotIn("workshop_collection_id", management_setting_keys)
        self.assertNotIn("workshop_auto_update", management_setting_keys)
        self.assertNotIn("client_content_workshop_ids", management_setting_keys)

    def test_workshop_source_mutations_persist_and_regenerate_client_manifest(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            with patch.object(app.workshop_source, "invalidate", wraps=app.workshop_source.invalidate) as invalidate:
                app.update_workshop_collection("123456789")
                app.update_workshop_auto_update(False)
                manifest_path = app.update_client_content_workshop_ids(
                    (" 234567890 ", "345678901", "234567890")
                )

            persisted = cast(
                dict[str, object],
                json.loads(gmod_settings_path(directory).read_text(encoding="utf-8")),
            )
            manifest = manifest_path.read_text(encoding="utf-8")
            app.update_workshop_collection(None)
            app.update_client_content_workshop_ids(("345678901",))

            updated = cast(
                dict[str, object],
                json.loads(gmod_settings_path(directory).read_text(encoding="utf-8")),
            )
            updated_manifest = gmod_workshop_manifest_path(directory).read_text(encoding="utf-8")

        self.assertEqual(persisted["workshop_collection_id"], "123456789")
        self.assertFalse(persisted["workshop_auto_update"])
        self.assertEqual(persisted["client_content_workshop_ids"], ["234567890", "345678901"])
        self.assertIn('resource.AddWorkshop("234567890")', manifest)
        self.assertIn('resource.AddWorkshop("345678901")', manifest)
        self.assertEqual(updated["workshop_collection_id"], "")
        self.assertEqual(updated["client_content_workshop_ids"], ["345678901"])
        self.assertNotIn('resource.AddWorkshop("234567890")', updated_manifest)
        self.assertIn('resource.AddWorkshop("345678901")', updated_manifest)
        self.assertEqual(invalidate.call_count, 2)

    def test_client_content_rejects_the_configured_collection_id_before_persisting(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            app.update_workshop_collection("100")

            with self.assertRaisesRegex(ValueError, "configured Workshop collection ID"):
                app.update_client_content_workshop_ids(("100", "200"))

            persisted = cast(
                dict[str, object],
                json.loads(gmod_settings_path(directory).read_text(encoding="utf-8")),
            )

        self.assertEqual(persisted["client_content_workshop_ids"], [])

    def test_client_content_allows_a_collection_member_id(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            app.update_workshop_collection("100")
            manifest_path = app.update_client_content_workshop_ids(("200",))
            manifest = manifest_path.read_text(encoding="utf-8")

        self.assertIn('resource.AddWorkshop("200")', manifest)

    def test_client_content_manifest_failure_restores_persisted_settings_and_manifest(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            manifest_path = app.update_client_content_workshop_ids(("200",))
            previous_manifest = manifest_path.read_text(encoding="utf-8")

            with (
                patch.object(app, "_sync_workshop_manifest", side_effect=OSError("manifest is read-only")),
                self.assertRaisesRegex(OSError, "manifest is read-only"),
            ):
                app.update_client_content_workshop_ids(("300",))

            persisted = cast(
                dict[str, object],
                json.loads(gmod_settings_path(directory).read_text(encoding="utf-8")),
            )
            restored_manifest = manifest_path.read_text(encoding="utf-8")

        self.assertEqual(persisted["client_content_workshop_ids"], ["200"])
        self.assertEqual(restored_manifest, previous_manifest)

    def test_expired_gslt_exit_has_a_safe_actionable_diagnosis(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            app.file_stdout = directory / "stdout.log"
            app.file_stdout.write_text(
                "\x1b[38;2;255;90;90mCould not establish connection to Steam servers. (GSL token expired)\n",
                encoding="utf-8",
            )
            app._stdout_capture_started = True

            fault = app.diagnose_unexpected_stop()

        self.assertIsNotNone(fault)
        assert fault is not None
        self.assertIs(fault.kind, AppRuntimeFaultKind.CRASH)
        self.assertIs(fault.code, AppRuntimeFaultCode.GMOD_STEAM_GAME_SERVER_LOGIN_TOKEN_REJECTED)
        self.assertEqual(fault.summary, "Steam rejected the configured Game Server Login Token.")
        self.assertEqual(
            fault.remediation,
            "Create a fresh GSLT for Garry's Mod (App ID 4000), replace it in Properties, then restart.",
        )

    def test_unrelated_gmod_exit_does_not_claim_a_known_diagnosis(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            app.file_stdout = directory / "stdout.log"
            app.file_stdout.write_text("Server quit after receiving a shutdown command.\n", encoding="utf-8")
            app._stdout_capture_started = True

            fault = app.diagnose_unexpected_stop()

        self.assertIsNone(fault)

    def test_immediate_gslt_exit_is_written_before_the_startup_error(self) -> None:
        class _ExitedProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO("FATAL ERROR: GSL token expired\n")

            @staticmethod
            def poll() -> int:
                return 1

        async def _start() -> tuple[RuntimeError, AppRuntimeFault | None, bool]:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_x64_runtime(directory)
                app = self._app(directory)
                app.file_stdout = directory / "stdout.log"
                app.set_steam_game_server_login_token("A0B1C2D3E4F5G6H7I8J9K0L1M2")
                process = _ExitedProcess()

                async def _launch() -> None:
                    app.process = cast(subprocess.Popen[str], cast(object, process))

                with self.assertRaises(RuntimeError) as raised:
                    with patch.object(app, "_std_launch", new=_launch):
                        await app.start()
                return raised.exception, app.diagnose_unexpected_stop(), app.process is None

        error, fault, process_cleared = asyncio.run(_start())

        self.assertEqual(str(error), "Garry's Mod exited before startup completed.")
        self.assertTrue(process_cleared)
        self.assertIsNotNone(fault)
        assert fault is not None
        self.assertIs(fault.code, AppRuntimeFaultCode.GMOD_STEAM_GAME_SERVER_LOGIN_TOKEN_REJECTED)

    def _run_manager_failed_initial_startup(
        self,
        *,
        stdout: str,
        exit_code: int,
    ) -> tuple[AppRuntimeFault | None, tuple[str, ...], bool, bool, int]:
        class _ExitedProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO(stdout)

            def poll(self) -> int:
                return exit_code

        async def run() -> tuple[AppRuntimeFault | None, tuple[str, ...], bool, bool, int]:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_x64_runtime(directory)
                app = self._app(directory)
                app.file_stdout = directory / "stdout.log"
                app.chat_channel = hikari.Snowflake(123)
                app.set_steam_game_server_login_token("A0B1C2D3E4F5G6H7I8J9K0L1M2")
                process = _ExitedProcess()
                manager = object.__new__(App_Manager)
                manager.apps = {app.name: app}
                manager.current = None
                notices: list[str] = []

                async def launch() -> None:
                    app.process = cast(subprocess.Popen[str], cast(object, process))

                def capture_notice(bound: object) -> None:
                    notices.append(cast(Any, bound).content)

                with (
                    patch.object(app, "_std_launch", new=launch),
                    patch.object(app, "diagnose_unexpected_stop", wraps=app.diagnose_unexpected_stop) as diagnose_mock,
                    patch("_manager.DC_Relay.add", side_effect=capture_notice),
                ):
                    with self.assertRaisesRegex(RuntimeError, "exited before startup completed"):
                        await manager.launch(app)
                    should_monitor = App_Manager._should_monitor_app(app)
                    # A monitor/launch handoff can invoke inactive handling twice; it must
                    # not relay the same fault twice.
                    await manager._handle_inactive_app(app, start_attempted=True)
                return app.runtime_fault, tuple(notices), app.process is None, should_monitor, diagnose_mock.call_count

        return asyncio.run(run())

    def test_manager_diagnoses_gslt_from_a_failed_initial_startup_without_duplicate_notice(self) -> None:
        fault, notices, process_cleared, should_monitor, diagnosis_calls = self._run_manager_failed_initial_startup(
            stdout="FATAL ERROR: GSL token expired\n",
            exit_code=1,
        )

        self.assertEqual(notices, ("Crashed",))
        self.assertTrue(process_cleared)
        self.assertFalse(should_monitor)
        self.assertEqual(diagnosis_calls, 1)
        self.assertIsNotNone(fault)
        assert fault is not None
        self.assertIs(fault.kind, AppRuntimeFaultKind.CRASH)
        self.assertIs(fault.code, AppRuntimeFaultCode.GMOD_STEAM_GAME_SERVER_LOGIN_TOKEN_REJECTED)
        self.assertEqual(
            fault.remediation,
            "Create a fresh GSLT for Garry's Mod (App ID 4000), replace it in Properties, then restart.",
        )

    def test_manager_uses_generic_fault_for_an_unrecognised_failed_initial_exit(self) -> None:
        fault, notices, process_cleared, should_monitor, diagnosis_calls = self._run_manager_failed_initial_startup(
            stdout="FATAL ERROR: unexpected engine failure\n",
            exit_code=42,
        )

        self.assertEqual(notices, ("Stopped unexpectedly",))
        self.assertTrue(process_cleared)
        self.assertFalse(should_monitor)
        self.assertEqual(diagnosis_calls, 1)
        self.assertEqual(
            fault,
            AppRuntimeFault(
                kind=AppRuntimeFaultKind.UNEXPECTED_EXIT,
                summary="The server process stopped unexpectedly.",
                remediation="Review the console output, then try starting the server again.",
            ),
        )

    def test_manager_does_not_diagnose_stale_stdout_when_gmod_never_launches(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        async def run() -> tuple[AppRuntimeFault | None, tuple[str, ...]]:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_x64_runtime(directory)
                app = self._app(directory)
                app.file_stdout = directory / "stdout.log"
                app.file_stdout.write_text("FATAL ERROR: GSL token expired\n", encoding="utf-8")
                app.chat_channel = hikari.Snowflake(123)
                app.set_steam_game_server_login_token(token)
                manager = object.__new__(App_Manager)
                manager.apps = {app.name: app}
                manager.current = None
                notices: list[str] = []

                def capture_notice(bound: object) -> None:
                    notices.append(cast(Any, bound).content)

                with (
                    patch.object(app, "_std_launch", new=AsyncMock(side_effect=OSError("launch failed"))),
                    patch("_manager.DC_Relay.add", side_effect=capture_notice),
                ):
                    with self.assertRaisesRegex(RuntimeError, "could not be launched"):
                        await manager.launch(app)
                return app.runtime_fault, tuple(notices)

        fault, notices = asyncio.run(run())

        self.assertIsNone(fault)
        self.assertEqual(notices, ())

    def test_gslt_is_write_only_and_redacted_from_config_and_runtime_output(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            app.set_steam_game_server_login_token(f"  {token}  ")
            token_path = gmod_game_server_login_token_path(directory)

            status = app.steam_game_server_login_token_status
            self.assertTrue(status.configured)
            self.assertEqual(status.game_app_id, STEAM_GAME_APP_ID)
            self.assertEqual(_read_gmod_game_server_login_token(directory), token)
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(token, repr(status))
            self.assertNotIn(token, str(status.to_mapping()))

            server_config = gmod_server_config_path(directory)
            server_config.write_text(
                f'hostname "GMod Alpha"; sv_setsteamaccount {token}; sv_lan 0\n',
                encoding="utf-8",
            )
            displayed = app.read_config_file("server/server.cfg")
            displayed_config = displayed.content
            self.assertNotIn(token, displayed_config)
            self.assertEqual(
                displayed_config,
                'hostname "GMod Alpha"; sv_setsteamaccount [REDACTED]; sv_lan 0\n',
            )
            self.assertIsNotNone(displayed.warning)
            assert displayed.warning is not None
            self.assertIn("legacy/manual", displayed.warning)
            self.assertIn("Remove it", displayed.warning)
            self.assertIn("Yukibot", displayed.warning)
            with self.assertRaisesRegex(ValueError, "managed"):
                app.write_config_file("server/server.cfg", f"hostname GMod; sv_setsteamaccount {token}\n")

            app._launch_token = token
            app.cmd_start = app._launch_command(token)
            with patch("apps.gmod.log.info") as launch_log:
                app.log_launch_context()
            launch_text = str(launch_log.call_args)
            self.assertNotIn(token, launch_text)
            self.assertIn("[REDACTED]", launch_text)

            output_path = directory / "redacted-output.log"
            asyncio.run(app._tee(io.StringIO(f"token={token}\n"), output_path, "STDOUT"))
            output = output_path.read_text(encoding="utf-8")
            self.assertNotIn(token, output)
            self.assertIn("[REDACTED]", output)

            app._clear_launch_token()
            self.assertNotIn(token, app.cmd_start)
            self.assertIsNone(app._launch_token)

            app.set_steam_game_server_login_token(None)
            self.assertFalse(app.steam_game_server_login_token_status.configured)
            self.assertFalse(token_path.exists())

    def test_legacy_server_config_redacts_quoted_inline_token_without_hiding_commands(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            server_config = gmod_server_config_path(directory)
            server_config.write_text(
                f'hostname "Foo"; sv_setsteamaccount "{token}"; sv_lan 0\n',
                encoding="utf-8",
            )

            displayed = app.read_config_file("server/server.cfg")

        self.assertEqual(
            displayed.content,
            'hostname "Foo"; sv_setsteamaccount "[REDACTED]"; sv_lan 0\n',
        )
        self.assertNotIn(token, displayed.content)
        self.assertIsNotNone(displayed.warning)

    def test_commented_legacy_server_config_commands_are_ignored(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        content = f'// sv_setsteamaccount {token}\nhostname "Foo" // sv_setsteamaccount {token}\n'
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            server_config = gmod_server_config_path(directory)
            server_config.write_text(content, encoding="utf-8")

            displayed = app.read_config_file("server/server.cfg")
            written = app.write_config_file("server/server.cfg", content)

        self.assertEqual(displayed.content, content)
        self.assertIsNone(displayed.warning)
        self.assertEqual(written.content, content)

    def test_legacy_server_config_keeps_quoted_double_slashes_out_of_comments(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        content = f'hostname "Foo // Preview"; sv_setsteamaccount "{token}"; sv_lan 0\n'
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            server_config = gmod_server_config_path(directory)
            server_config.write_text(content, encoding="utf-8")

            displayed = app.read_config_file("server/server.cfg")
            with self.assertRaisesRegex(ValueError, "managed"):
                app.write_config_file("server/server.cfg", content)

        self.assertEqual(
            displayed.content,
            'hostname "Foo // Preview"; sv_setsteamaccount "[REDACTED]"; sv_lan 0\n',
        )
        self.assertNotIn(token, displayed.content)
        self.assertIsNotNone(displayed.warning)

    def test_legacy_server_config_only_redacts_active_commands_in_mixed_content(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        content = (
            f"// sv_setsteamaccount {token}\n"
            f'hostname "Foo" // sv_setsteamaccount {token}\n'
            f"sv_setsteamaccount {token}; sv_lan 0\n"
            f"sv_setsteamaccount {token}; sv_lan 0 // sv_setsteamaccount {token}\n"
        )
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            server_config = gmod_server_config_path(directory)
            server_config.write_text(content, encoding="utf-8")

            displayed = app.read_config_file("server/server.cfg")
            with self.assertRaisesRegex(ValueError, "managed"):
                app.write_config_file("server/server.cfg", content)

        self.assertEqual(
            displayed.content,
            (
                f"// sv_setsteamaccount {token}\n"
                f'hostname "Foo" // sv_setsteamaccount {token}\n'
                "sv_setsteamaccount [REDACTED]; sv_lan 0\n"
                f"sv_setsteamaccount [REDACTED]; sv_lan 0 // sv_setsteamaccount {token}\n"
            ),
        )
        self.assertIsNotNone(displayed.warning)

    def test_legacy_server_config_download_is_redacted(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            server_config = gmod_server_config_path(directory)
            server_config.write_text(
                f'hostname "Foo"; sv_setsteamaccount {token}; sv_lan 0\n',
                encoding="utf-8",
            )

            response = asyncio.run(
                NodeApiService().storage.build_config_root_download_response(app=app, root_id="server")
            )

        downloaded_content = bytes(response.body).decode("utf-8")
        self.assertEqual(
            downloaded_content,
            'hostname "Foo"; sv_setsteamaccount [REDACTED]; sv_lan 0\n',
        )
        self.assertNotIn(token, downloaded_content)
        self.assertEqual(
            response.headers["content-disposition"],
            'attachment; filename="server.cfg"',
        )

    def test_steam_updater_uses_gmod_dedicated_server_app_id_and_verify_flow(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            self.assertIsInstance(app.updater, SteamCmd_Update_Manager)
            assert isinstance(app.updater, SteamCmd_Update_Manager)
            self.assertTrue(app.updater.supports_verify)
            steam_update = app.cfg.steam_update
            assert steam_update is not None
            command = app.updater._steamcmd_command(
                branch=steam_update.selected_branch_config,
                validate=True,
            )

        self.assertEqual(STEAM_APP_ID, 4020)
        self.assertEqual(steam_update.login.username, "anonymous")
        self.assertEqual(steam_update.selected_branch, GMOD_X64_STEAM_BRANCH)
        self.assertEqual(
            command,
            [
                "steamcmd",
                "+force_install_dir",
                str(directory),
                "+login",
                "anonymous",
                "+app_update",
                "4020",
                "-beta",
                GMOD_X64_STEAM_BRANCH,
                "validate",
                "+quit",
            ],
        )

    def test_legacy_steam_branch_is_migrated_to_x64_and_cannot_be_selected(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            cfg = self._config(directory)
            cfg.steam_update = SteamUpdateConfig(
                app_id=STEAM_APP_ID,
                branches=(
                    SteamUpdateBranch(branch_id="public", label="Public"),
                    SteamUpdateBranch(branch_id="X86-64", label="64-bit binaries"),
                ),
                selected_branch="public",
            )
            with patch("apps._updater.resolve_steamcmd_command_prefix", return_value=("steamcmd",)):
                app = Gmod(Mock(), Mock(), cfg)

            steam_update = app.cfg.steam_update
            assert steam_update is not None
            self.assertEqual(steam_update.selected_branch, GMOD_X64_STEAM_BRANCH)
            self.assertEqual([branch.branch_id for branch in steam_update.branches], [GMOD_X64_STEAM_BRANCH])
            self.assertEqual(steam_update.selected_branch_config.display_label, "64-bit binaries")
            assert isinstance(app.updater, SteamCmd_Update_Manager)
            with self.assertRaisesRegex(ValueError, "requires branch"):
                app.updater.select_branch("public")

    def test_start_requires_the_x64_launcher(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            app = self._app(Path(temporary_directory))
            app.set_steam_game_server_login_token(token)

            with self.assertRaisesRegex(FileNotFoundError, "64-bit launcher"):
                asyncio.run(app.start())

    def test_start_requires_the_x64_engine_binary(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "srcds_run_x64").write_text("#!/bin/sh\n", encoding="utf-8")
            app = self._app(directory)
            app.set_steam_game_server_login_token(token)

            with self.assertRaisesRegex(FileNotFoundError, "engine binary"):
                asyncio.run(app.start())

    def test_launch_failure_does_not_surface_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_gmod_x64_runtime(directory)
            app = self._app(directory)
            app.set_steam_game_server_login_token(token)

            async def _start() -> RuntimeError:
                with (
                    patch.object(app, "_std_launch", new=AsyncMock(side_effect=RuntimeError(token))),
                    patch.object(app, "_drain_stderr_task", new=AsyncMock()),
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        await app.start()
                return raised.exception

            error = asyncio.run(_start())

        self.assertEqual(str(error), "Garry's Mod could not be launched.")
        self.assertNotIn(token, app.cmd_start)
        self.assertIsNone(app._launch_token)

    def test_stdout_capture_marks_a_complete_console_status_ready(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            app._reset_startup_status_probe()

            asyncio.run(
                app._tee(
                    io.StringIO(
                        "hostname: GMod Alpha\n"
                        "udp/ip  : 0.0.0.0:27015\n"
                        "map     : gm_construct\n"
                        "players : 0 humans, 0 bots (16 max)\n"
                    ),
                    directory / "stdout.log",
                    "STDOUT",
                )
            )

            ready_event = app._startup_status_ready_event

        self.assertIsNotNone(ready_event)
        assert ready_event is not None
        self.assertTrue(ready_event.is_set())

    def test_manifest_sync_failure_does_not_retain_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_gmod_x64_runtime(directory)
            app = self._app(directory)
            app.set_steam_game_server_login_token(token)

            with patch.object(app, "_sync_workshop_manifest", side_effect=OSError("read-only")):
                with self.assertRaisesRegex(OSError, "read-only"):
                    asyncio.run(app.start())

        self.assertNotIn(token, app.cmd_start)
        self.assertIsNone(app._launch_token)

    def test_start_waits_for_source_query_and_cleans_up_a_failed_readiness_check(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _RunningProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO()

            @staticmethod
            def poll() -> None:
                return None

        async def run() -> tuple[bool, AsyncMock]:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_x64_runtime(directory)
                app = self._app(directory)
                app.set_steam_game_server_login_token(token)
                process = _RunningProcess()

                async def launch() -> None:
                    app.process = cast(subprocess.Popen[str], cast(object, process))

                cleanup = AsyncMock(return_value=True)
                with (
                    patch.object(app, "_std_launch", new=launch),
                    patch.object(
                        app,
                        "_wait_for_startup_ready",
                        new=AsyncMock(side_effect=RuntimeError("query unavailable")),
                    ),
                    patch.object(app, "_terminate_runtime", new=cleanup),
                ):
                    with self.assertRaisesRegex(RuntimeError, "query unavailable"):
                        await app.start()
                return app.is_started, cleanup

        is_started, cleanup = asyncio.run(run())

        self.assertFalse(is_started)
        cleanup.assert_awaited_once()

    def test_start_marks_ready_after_a_source_info_response(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _RunningProcess:
            def __init__(self) -> None:
                self.exit_code: int | None = None
                self.stdout = io.StringIO()

            def poll(self) -> int | None:
                return self.exit_code

        def respond(_request: bytes, _request_count: int) -> bytes:
            return b"\xff\xff\xff\xffI\x11"

        async def run() -> tuple[bool, bool, bool, bool, str | None]:
            transport, protocol, port = await _create_source_query_responder(respond)
            try:
                with TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    _write_gmod_x64_runtime(directory)
                    app = self._app(directory, port=port)
                    app.set_steam_game_server_login_token(token)
                    process = _RunningProcess()

                    async def launch() -> None:
                        app.process = cast(subprocess.Popen[str], cast(object, process))

                    with patch.object(app, "_std_launch", new=launch):
                        started = await app.start()
                    is_started = app.is_started
                    manifest_exists = gmod_workshop_manifest_path(directory).is_file()
                    process.exit_code = 0
                    await app.handle_unexpected_stop()
                    saw_info_query = protocol.requests == [b"\xff\xff\xff\xffTSource Engine Query\x00"]
                    return started, is_started, saw_info_query, manifest_exists, app._launch_token
            finally:
                transport.close()

        started, is_started, saw_info_query, manifest_exists, launch_token = asyncio.run(run())

        self.assertTrue(started)
        self.assertTrue(is_started)
        self.assertTrue(saw_info_query)
        self.assertTrue(manifest_exists)
        self.assertIsNone(launch_token)

    def test_startup_status_command_marks_ready_when_source_query_is_unavailable(self) -> None:
        class _RunningProcess:
            def __init__(self) -> None:
                self.stdin = io.StringIO()

            @staticmethod
            def poll() -> None:
                return None

        async def run() -> tuple[str, AsyncMock]:
            with TemporaryDirectory() as temporary_directory:
                app = self._app(Path(temporary_directory))
                process = _RunningProcess()
                app.process = cast(subprocess.Popen[str], cast(object, process))
                app._reset_startup_status_probe()
                send_status = app._request_startup_console_status

                async def request_status() -> bool:
                    sent = await send_status()
                    for line in (
                        "hostname: GMod Alpha\n",
                        "udp/ip  : 0.0.0.0:27015\n",
                        "map     : gm_construct\n",
                        "players : 0 humans, 0 bots (16 max)\n",
                    ):
                        app._observe_startup_console_line(line)
                    return sent

                request = AsyncMock(side_effect=request_status)
                with (
                    patch("apps.gmod.gmod_server_info_responds", new=AsyncMock(return_value=False)),
                    patch.object(app, "_request_startup_console_status", new=request),
                    patch("apps.gmod._GMOD_STARTUP_STATUS_PROBE_INITIAL_DELAY_SECONDS", 0.0),
                ):
                    await app._wait_for_startup_ready()
                return process.stdin.getvalue(), request

        command, request = asyncio.run(run())

        self.assertEqual(command, "status\n")
        request.assert_awaited_once()

    def test_launch_cancellation_runs_startup_cleanup(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _RunningProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO()

            @staticmethod
            def poll() -> None:
                return None

        async def run() -> tuple[AsyncMock, bool]:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_x64_runtime(directory)
                app = self._app(directory)
                app.set_steam_game_server_login_token(token)
                process = _RunningProcess()
                launch_started = asyncio.Event()
                wait_forever = asyncio.Event()

                async def launch() -> None:
                    app.process = cast(subprocess.Popen[str], cast(object, process))
                    launch_started.set()
                    await wait_forever.wait()

                termination = AsyncMock(return_value=True)
                with (
                    patch.object(app, "_std_launch", new=launch),
                    patch.object(app, "_terminate_runtime", new=termination),
                ):
                    start_task = asyncio.create_task(app.start())
                    await launch_started.wait()
                    start_task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await start_task
                return termination, app.is_started

        termination, is_started = asyncio.run(run())

        self.assertFalse(is_started)
        termination.assert_awaited_once()

    def test_start_cleans_up_a_running_process_without_stdout(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _RunningProcess:
            stdout = None

            @staticmethod
            def poll() -> None:
                return None

        async def run() -> AsyncMock:
            with TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                _write_gmod_x64_runtime(directory)
                app = self._app(directory)
                app.set_steam_game_server_login_token(token)
                process = _RunningProcess()

                async def launch() -> None:
                    app.process = cast(subprocess.Popen[str], cast(object, process))

                cleanup = AsyncMock(return_value=True)
                with (
                    patch.object(app, "_std_launch", new=launch),
                    patch.object(app, "_terminate_runtime", new=cleanup),
                ):
                    with self.assertRaisesRegex(RuntimeError, "exited before startup completed"):
                        await app.start()
                return cleanup

        cleanup = asyncio.run(run())

        cleanup.assert_awaited_once()

    def test_process_detection_only_matches_this_instance_directory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = self._app(directory)
            expected = tuple(part.casefold() for part in app.proc_cmd)

            matches = app._matches_leftover_process(
                command_line=("srcds", "-game", "garrysmod"),
                expected_command_parts=expected,
                process_cwd=str(directory),
            )
            different_directory = app._matches_leftover_process(
                command_line=("srcds", "-game", "garrysmod"),
                expected_command_parts=expected,
                process_cwd=str(directory / "other"),
            )

        self.assertTrue(matches)
        self.assertFalse(different_directory)

    def test_leftover_process_cleanup_does_not_log_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _Process:
            def __init__(self, directory: Path) -> None:
                self.info = {
                    "name": "srcds",
                    "pid": 123,
                    "cmdline": ["srcds", "-game", "garrysmod", "+sv_setsteamaccount", token],
                    "cwd": str(directory),
                }
                self.terminate_calls = 0

            def terminate(self) -> None:
                self.terminate_calls += 1

            def wait(self, timeout: float) -> None:
                del timeout

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            app = object.__new__(Gmod)
            app.proc_name = "srcds"
            app.proc_cmd = ["srcds", "-game", "garrysmod"]
            app.directory = directory
            process = _Process(directory)

            with patch("apps._app.psutil.process_iter", return_value=(process,)), patch(
                "apps._app.log.info"
            ) as info_log:
                app._terminate_leftover_processes_sync()

        self.assertEqual(process.terminate_calls, 1)
        self.assertNotIn(token, str(info_log.call_args_list))

    def test_stored_process_cleanup_does_not_log_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"

        class _Process:
            pid = 123

            @staticmethod
            def wait(timeout: float) -> None:
                del timeout
                raise RuntimeError(f"GMod process failure: {token}")

            @staticmethod
            def poll() -> int:
                return 0

        app = object.__new__(Gmod)
        app.name = "gmod_alpha"
        app.process = cast(subprocess.Popen[str], cast(object, _Process()))
        app.proc_name = ""
        app._stderr_task = None

        with (
            patch("apps._app.os.killpg") as kill_process_group,
            patch("apps._app.log.warning") as warning_log,
            patch("apps._app.log.exception") as exception_log,
        ):
            asyncio.run(app._terminate())

        kill_process_group.assert_called_once()
        self.assertIsNone(app.process)
        self.assertNotIn(token, str(warning_log.call_args_list))
        self.assertNotIn(token, str(exception_log.call_args_list))

    def test_stdout_drain_cancels_a_stalled_task(self) -> None:
        async def _drain() -> bool:
            app = object.__new__(Gmod)
            app.name = "gmod_alpha"
            task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(3600))
            app._stdout_task = task
            await app._drain_stdout_task(timeout_seconds=0.05)
            return task.cancelled()

        self.assertTrue(asyncio.run(_drain()))


class GmodInstallerTests(unittest.TestCase):
    def test_steam_install_recipe_requires_and_redacts_the_gslt(self) -> None:
        token = "A0B1C2D3E4F5G6H7I8J9K0L1M2"
        manager = object.__new__(App_Manager)
        original_directory = Path.cwd()
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            scope_path = directory / "apps" / "gmod"
            scope_path.mkdir(parents=True)
            (scope_path / "__init__.py").write_text("", encoding="utf-8")
            os.chdir(directory)
            try:
                recipes = manager.list_steam_install_recipes()
                self.assertEqual(len(recipes), 1)
                recipe = recipes[0]
                self.assertEqual(recipe.scope, "gmod")
                self.assertEqual(recipe.label, "Garry's Mod")
                self.assertEqual(recipe.default_port, GMOD_DEFAULT_PORT)
                self.assertEqual(recipe.steam_update.app_id, STEAM_APP_ID)
                self.assertEqual(recipe.steam_update.selected_branch, GMOD_X64_STEAM_BRANCH)
                self.assertEqual(
                    [branch.branch_id for branch in recipe.steam_update.branches],
                    [GMOD_X64_STEAM_BRANCH],
                )
                self.assertEqual(recipe.inputs, (AppInstallInput.GAME_SERVER_LOGIN_TOKEN,))
                self.assertEqual(recipe.game_server_login_token_app_id, STEAM_GAME_APP_ID)
                self.assertNotEqual(recipe.game_server_login_token_app_id, recipe.steam_update.app_id)
                self.assertIsNotNone(recipe.post_steam_install)

                catalog = asyncio.run(
                    NodeAppInstallerService(
                        node_name=lambda: "node-a",
                        invalidate_state_caches=Mock(),
                    ).build_catalog(manager=manager)
                )
                catalog_recipe = catalog.recipes[0]
                self.assertEqual(catalog_recipe.default_instance_key, "alpha")
                self.assertEqual(catalog_recipe.default_subfolder, GMOD_DEFAULT_INSTALL_SUBFOLDER)
                install_field = catalog_recipe.fields[0]
                self.assertEqual(install_field.key, AppInstallInput.GAME_SERVER_LOGIN_TOKEN.value)
                self.assertEqual(install_field.kind, NodeAppInstallInputKind.PASSWORD)
                self.assertEqual(install_field.game_server_login_token_app_id, STEAM_GAME_APP_ID)
                self.assertEqual(install_field.action_label, "Manage tokens on Steam")
                self.assertEqual(install_field.action_url, STEAM_GAME_SERVER_LOGIN_TOKEN_MANAGEMENT_URL)
                self.assertNotIn(token, str(catalog_recipe.to_mapping()))

                request = NodeAppInstallRequest(
                    scope="gmod",
                    instance_key="alpha",
                    friendly_name="GMod Alpha",
                    subfolder="gmod-alpha",
                    port=27031,
                    steam_branch_id=GMOD_X64_STEAM_BRANCH,
                    inputs={AppInstallInput.GAME_SERVER_LOGIN_TOKEN: token},
                )
                self.assertNotIn(token, repr(request))
                create_request = NodeAppInstallerService._create_request(request)
                self.assertEqual(create_request.steam_game_server_login_token, token)
                self.assertNotIn(token, repr(create_request))
                with self.assertRaisesRegex(ValueError, "Login Token is required"):
                    manager.prepare_instance_creation(replace(create_request, steam_game_server_login_token=None))
                with self.assertRaisesRegex(ValueError, "requires branch"):
                    manager.prepare_instance_creation(replace(create_request, steam_branch="public"))
                redacted_detail = NodeAppInstallerService._redact_install_detail(
                    detail=f"SteamCMD printed {token}",
                    steam_update=recipe.steam_update,
                    secret_values=(token,),
                )
                self.assertNotIn(token, redacted_detail)

                staging_directory = directory / "staging"
                staging_directory.mkdir()
                _write_gmod_x64_runtime(staging_directory)
                post_processor = recipe.post_steam_install
                assert post_processor is not None
                asyncio.run(post_processor(staging_directory, create_request))
                self.assertEqual(_read_gmod_game_server_login_token(staging_directory), token)
                self.assertTrue(gmod_server_config_path(staging_directory).is_file())

                manager.create_instance(create_request)
                instance_payload = json.loads((scope_path / "instances.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(original_directory)

        self.assertEqual(instance_payload["alpha"]["join_port"], 27031)
        self.assertEqual(instance_payload["alpha"]["steam_update"]["app_id"], STEAM_APP_ID)
        self.assertEqual(instance_payload["alpha"]["steam_update"]["selected_branch"], GMOD_X64_STEAM_BRANCH)
        self.assertNotIn(token, json.dumps(instance_payload))
