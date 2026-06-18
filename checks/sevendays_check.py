import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Never, cast
from unittest.mock import AsyncMock, patch

import config
from _discord import Fileish, OutboundRelayFormatter, RelayMessageReferenceKind, RelayOutboundFormatOptions
from _security import Power_Level
from _utils import Utilities
from apps._config import App_Config, AppVersion, Mod_Config, ModDownloadBlockReason, ModType
from apps._mod import Mod_Manager
from apps.sevendays import (
    Activities as SevenDaysActivities,
)
from apps.sevendays import (
    Matchers,
    Mod_7D2D,
    Receiver,
    SevenDays,
    SevenDaysAdminAddRequest,
    _candidate_sevendays_logs,
    _preferred_sevendays_runtime_log,
    detect_sevendays_version,
    parse_admin_add_value,
    parse_gamestat_value,
)


class _RecordingActivityManager:
    def __init__(self) -> None:
        self.registered: list[object] = []
        self.deregistered: list[object] = []

    def register(self, provider: object) -> None:
        self.registered.append(provider)

    def deregister(self, provider: object) -> None:
        self.deregistered.append(provider)


class SevenDaysGameStatParsingTests(unittest.TestCase):
    def test_builtin_mods_are_not_downloadable(self) -> None:
        mod = Mod_7D2D(Mod_Config(name="0_TFP_Harmony", directory=Path(".")))

        self.assertFalse(mod.downloadable)
        self.assertEqual(mod.cfg.mod_type, ModType.BUILTIN)
        self.assertEqual(mod.cfg.download_block_reason, ModDownloadBlockReason.BUILTIN)

    def test_manager_detects_disabled_mod_by_modinfo_suffix(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            apps_dir = root / "apps"
            app_dir = apps_dir / "sevendays"
            mods_dir = app_dir / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml.disabled").write_text("<mod />", encoding="utf-8")
            app_cfg = App_Config(
                name="sevendays_alpha",
                instance_key="alpha",
                friendly_name="7D2D",
                directory=app_dir,
                apps_dir=apps_dir,
                mods_dir=mods_dir,
                join_host="127.0.0.1",
                scope="sevendays",
            )
            Mod_Manager._instances.clear()
            manager = Mod_Manager(app_cfg, mod_cls=Mod_7D2D, db_path=root / "mods.jsonl")

            try:
                asyncio.run(manager.reload_mods())
            finally:
                Mod_Manager._instances.clear()

            mod = manager.get("ExampleMod")

        self.assertFalse(mod.cfg.enabled)
        self.assertEqual(mod.path, mod_dir)

    def test_disable_renames_modinfo_xml_instead_of_the_mod_folder(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            mod_info = mod_dir / "ModInfo.xml"
            mod_info.write_text("<mod />", encoding="utf-8")
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            asyncio.run(mod.disable())

            self.assertTrue(mod_dir.exists())
            self.assertFalse(mod_info.exists())
            self.assertTrue((mod_dir / "ModInfo.xml.disabled").exists())
            self.assertFalse(mod.cfg.enabled)

    def test_detect_version_reads_modinfo_xml(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8" ?>
<xml>
    <Version value="1.2.3.4" />
</xml>""",
                encoding="utf-8",
            )
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            self.assertEqual(mod.detect_version(), "1.2.3.4")

    def test_detect_friendly_reads_display_name_from_modinfo_xml(self) -> None:
        with TemporaryDirectory() as tmp:
            mods_dir = Path(tmp) / "Mods"
            mod_dir = mods_dir / "ExampleMod"
            mod_dir.mkdir(parents=True)
            (mod_dir / "ModInfo.xml").write_text(
                """<?xml version="1.0" encoding="UTF-8" ?>
<xml>
    <DisplayName value="Better Loot" />
</xml>""",
                encoding="utf-8",
            )
            mod = Mod_7D2D(Mod_Config(name="ExampleMod", directory=mods_dir))

            mod.sync_metadata()

            self.assertEqual(mod.friendly, "Better Loot")

    def test_detect_sevendays_version_from_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "server_stdout.log"
            log_path.write_text(
                "2025-06-14T09:47:42 0.030 INF Version: V 1.4 (b8) Compatibility Version: V 1.4, Build: LinuxServer 64 Bit\n",
                encoding="utf-8",
            )

            version = detect_sevendays_version(directory=root, server_log=log_path)

        self.assertEqual(version, AppVersion(main="1.4", build=8))

    def test_candidate_sevendays_logs_include_timestamped_output_logs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir()
            older = log_dir / "output_log__2026-06-17__02-27-35.txt"
            newer = log_dir / "output_log__2026-06-17__03-27-35.txt"
            older.write_text("older", encoding="utf-8")
            newer.write_text("newer", encoding="utf-8")

            candidates = _candidate_sevendays_logs(directory=root, server_log=None)

        self.assertIn(newer, candidates)
        self.assertIn(older, candidates)
        self.assertLess(candidates.index(newer), candidates.index(older))

    def test_detect_sevendays_version_from_timestamped_output_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir()
            log_path = log_dir / "output_log__2026-06-17__02-27-35.txt"
            log_path.write_text(
                "2026-06-17T02:27:35 0.030 INF Version: V 2.0 (b1) Compatibility Version: V 2.0\n",
                encoding="utf-8",
            )

            version = detect_sevendays_version(directory=root, server_log=None)

        self.assertEqual(version, AppVersion(main="2.0", build=1))

    def test_preferred_sevendays_runtime_log_prefers_launch_created_timestamped_log(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir()
            older = log_dir / "output_log__2026-06-17__02-27-35.txt"
            newer = log_dir / "output_log__2026-06-17__03-27-35.txt"
            older.write_text("older", encoding="utf-8")
            newer.write_text("newer", encoding="utf-8")

            preferred = _preferred_sevendays_runtime_log(
                directory=root,
                server_log=None,
                previous_timestamped_logs={older},
            )

        self.assertEqual(preferred, newer)

    def test_app_config_parses_save_file_write_level_override(self) -> None:
        cfg = App_Config(
            name="sevendays_alpha",
            instance_key="alpha",
            directory=Path("/srv/7d2d"),
            apps_dir=Path("/srv/apps"),
            scope="sevendays",
            save_file_write_level_override=Power_Level.admin,
        )

        self.assertEqual(cfg.save_file_write_level_override, Power_Level.admin)

    def test_save_file_roots_require_userdata_folder_redirect(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "serverconfig.xml").write_text(
                """<?xml version="1.0"?>
<ServerSettings>
    <property name="GameWorld" value="Navezgane" />
    <property name="GameName" value="AlphaWorld" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir

            roots = app.save_file_roots

            self.assertEqual(roots, ())
            self.assertFalse(app.supports_save_uploads)

    def test_save_file_roots_use_redirected_userdata_folder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            app_dir.mkdir()
            userdata_dir.mkdir()
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
    <property name="GameWorld" value="Navezgane" />
    <property name="GameName" value="AlphaWorld" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir

            roots = app.save_file_roots

            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].path, userdata_dir / "Saves" / "Navezgane" / "AlphaWorld")
            self.assertTrue(app.supports_save_uploads)

    def test_list_save_files_discovers_redirected_saves_without_current_game_selection(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            alpha_save_dir = userdata_dir / "Saves" / "Navezgane" / "AlphaWorld"
            bravo_save_dir = userdata_dir / "Saves" / "Pregen10k" / "BravoWorld"
            app_dir.mkdir()
            alpha_save_dir.mkdir(parents=True)
            bravo_save_dir.mkdir(parents=True)
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir

            saves = app.list_save_files()

            self.assertEqual(
                tuple((save.root_label, save.label) for save in saves),
                (("Navezgane", "AlphaWorld"), ("Pregen10k", "BravoWorld")),
            )
            self.assertTrue(app.supports_save_uploads)

    def test_delete_save_file_removes_redirected_discovered_save_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_dir = root / "server"
            userdata_dir = root / "userdata"
            save_dir = userdata_dir / "Saves" / "Navezgane" / "AlphaWorld"
            app_dir.mkdir()
            save_dir.mkdir(parents=True)
            (save_dir / "region.dat").write_text("save-data", encoding="utf-8")
            (app_dir / "serverconfig.xml").write_text(
                f"""<?xml version="1.0"?>
<ServerSettings>
    <property name="UserDataFolder" value="{userdata_dir.as_posix()}" />
    <property name="GameWorld" value="Navezgane" />
    <property name="GameName" value="AlphaWorld" />
</ServerSettings>
""",
                encoding="utf-8",
            )
            app = cast(Any, object.__new__(SevenDays))
            app.directory = app_dir
            app.check_running = lambda: False

            save_id = next(save.id for save in app.list_save_files() if save.label == "AlphaWorld")
            deleted = app.delete_save_file(file_id=save_id)

            self.assertEqual(deleted.label, "AlphaWorld")
            self.assertFalse(save_dir.exists())

    def test_parse_gamestat_value_returns_none_for_empty_value(self) -> None:
        self.assertIsNone(parse_gamestat_value(""))

    def test_parse_gamestat_value_parses_python_literals(self) -> None:
        self.assertEqual(parse_gamestat_value("42"), 42)
        self.assertEqual(parse_gamestat_value("3.5"), 3.5)
        self.assertEqual(parse_gamestat_value("True"), True)
        self.assertEqual(parse_gamestat_value("'hello'"), "hello")

    def test_parse_gamestat_value_preserves_bare_identifier_as_string(self) -> None:
        self.assertEqual(parse_gamestat_value("XPOnly"), "XPOnly")

    def test_parse_admin_add_value_supports_pipe_separator(self) -> None:
        self.assertEqual(
            parse_admin_add_value("EOS_123456789 | 0"),
            SevenDaysAdminAddRequest(subject="EOS_123456789", permission_level=0),
        )

    def test_parse_admin_add_value_supports_trailing_level(self) -> None:
        self.assertEqual(
            parse_admin_add_value("Alice 100"),
            SevenDaysAdminAddRequest(subject="Alice", permission_level=100),
        )

    def test_parse_admin_add_value_rejects_missing_level(self) -> None:
        with self.assertRaises(ValueError):
            parse_admin_add_value("Alice")


class SevenDaysRelayFormattingTests(unittest.IsolatedAsyncioTestCase):
    def test_outbound_formatter_appends_public_urls_for_attachments(self) -> None:
        payload = SimpleNamespace(
            urls=set(),
            files={Fileish("/tmp/cat.png", "cat.png")},
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        with patch(
            "_discord.Utilities.linkify", return_value=("https://public.example/uploads/cat.png", Path("/tmp/cat.png"))
        ):
            formatted = OutboundRelayFormatter.format_payload(
                cast(Any, payload),
                RelayOutboundFormatOptions(base_content="look"),
            )

        self.assertEqual(formatted, "look https://public.example/uploads/cat.png")


class SevenDaysActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sevendays_activities_track_started_tasks_and_deregister_provider(self) -> None:
        activity_manager = _RecordingActivityManager()

        async def background_worker() -> Never:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        app = SimpleNamespace(activity_manager=activity_manager, _tail_matchers=set())
        activities = SevenDaysActivities(cast(Any, app))
        provider = activities.providers[0]
        provider.task_funcs = [background_worker]

        await activities.start()

        self.assertEqual(activity_manager.registered, [provider])
        self.assertEqual(len(activities.tasks), 1)

        await activities.stop()

        self.assertEqual(activity_manager.deregistered, [provider])
        self.assertEqual(activities.tasks, set())

    def test_outbound_formatter_uses_percent_encoded_public_urls_for_attachments(self) -> None:
        payload = SimpleNamespace(
            urls=set(),
            files={Fileish("/tmp/cat pic.png", "cat pic.png")},
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        with patch(
            "_discord.Utilities.linkify",
            return_value=("https://public.example/uploads/cat%20pic.png", Path("/tmp/cat pic.png")),
        ):
            formatted = OutboundRelayFormatter.format_payload(
                cast(Any, payload),
                RelayOutboundFormatOptions(base_content="look"),
            )

        self.assertEqual(formatted, "look https://public.example/uploads/cat%20pic.png")

    def test_linkify_percent_encodes_attachment_file_names(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "cat pic.png"
            source.write_text("meow", encoding="utf-8")
            upload_dir = root / "uploads"
            upload_dir.mkdir()

            with (
                patch.object(config, "DIR_UPLOAD", upload_dir),
                patch.object(config, "PUBLIC_UPLOADS_BASE_URL", "https://public.example/uploads/"),
            ):
                link, uploaded = Utilities.linkify(source)

        self.assertEqual(link, "https://public.example/uploads/cat%20pic.png")
        self.assertEqual(uploaded, source)

    def test_outbound_formatter_prefixes_reply_indicator(self) -> None:
        payload = SimpleNamespace(
            urls=set(),
            files=set(),
            reference_kind=RelayMessageReferenceKind.REPLY,
        )

        formatted = OutboundRelayFormatter.format_payload(
            cast(Any, payload),
            RelayOutboundFormatOptions(
                base_content="look",
                reference_renderer=lambda kind: "reply;" if kind is RelayMessageReferenceKind.REPLY else None,
            ),
        )

        self.assertEqual(formatted, "reply; look")

    async def test_receiver_appends_public_urls_for_attachments(self) -> None:
        app = SimpleNamespace(_relay=SimpleNamespace(send=AsyncMock()))
        receiver = Receiver(cast(Any, app))
        payload = SimpleNamespace(
            alias="Erin",
            content_demojised="look",
            urls=set(),
            files={Fileish("/tmp/cat.png", "cat.png")},
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        with patch(
            "_discord.Utilities.linkify", return_value=("https://public.example/uploads/cat.png", Path("/tmp/cat.png"))
        ):
            await receiver.send(cast(Any, payload))

        sent_command = app._relay.send.await_args.args[0]
        self.assertEqual(sent_command, 'say "Erin: look https://public.example/uploads/cat.png"')

    async def test_receiver_prefixes_forward_indicator(self) -> None:
        app = SimpleNamespace(_relay=SimpleNamespace(send=AsyncMock()))
        receiver = Receiver(cast(Any, app))
        payload = SimpleNamespace(
            alias="Erin",
            content_demojised="look",
            urls=set(),
            files=set(),
            reference_kind=RelayMessageReferenceKind.FORWARD,
        )

        await receiver.send(cast(Any, payload))

        sent_command = app._relay.send.await_args.args[0]
        self.assertEqual(sent_command, 'say "Erin: forwarded; look"')

    async def test_receiver_quotes_embedded_double_quotes(self) -> None:
        app = SimpleNamespace(_relay=SimpleNamespace(send=AsyncMock()))
        receiver = Receiver(cast(Any, app))
        payload = SimpleNamespace(
            alias='Erin "Admin"',
            content_demojised='say "hi"',
            urls=set(),
            files=set(),
            reference_kind=RelayMessageReferenceKind.NONE,
        )

        await receiver.send(cast(Any, payload))

        sent_command = app._relay.send.await_args.args[0]
        self.assertEqual(sent_command, 'say "Erin \'Admin\': say \'hi\'"')


class SevenDaysRelayMatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_death_relays_player_death(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app.scope = "sevendays"
        app._tail_matchers = set()
        app._server_ready = asyncio.Event()
        matcher = Matchers(app)

        with patch("apps.sevendays.DC_Relay.add") as add_mock:
            await matcher.match_death("2026-05-27T22:07:40 8405.351 INF GMSG: Player 'asdblackmea' died")

        add_mock.assert_called_once()
        relayed_message = add_mock.call_args.args[0]
        self.assertEqual(relayed_message.player, "asdblackmea")
        self.assertEqual(relayed_message.content, "asdblackmea died")

    async def test_match_ready_sets_server_ready_event(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app.scope = "sevendays"
        app._tail_matchers = set()
        app._server_ready = asyncio.Event()
        matcher = Matchers(app)

        await matcher.match_ready("2026-05-23T11:19:12 8.590 INF StartAsServer")

        self.assertTrue(app._server_ready.is_set())

    async def test_start_waits_for_ready_signal_before_marking_app_running(self) -> None:
        app = cast(Any, object.__new__(SevenDays))
        app.name = "sevendays_demo"
        app.directory = Path("/tmp/sevendays_demo")
        app.server_log = None
        app.file_stdout = Path("/tmp/sevendays_stdout.log")
        app.process = SimpleNamespace(stdout=object())
        app._server_ready = asyncio.Event()
        app._tail_matchers = set()
        app._std_launch = AsyncMock()
        app.check_running = lambda: True
        relay_reader = object()
        app._relay = SimpleNamespace(
            setup=AsyncMock(return_value=relay_reader),
            connected_event=asyncio.Event(),
        )
        call_order: list[str] = []

        async def wait_for_ready_event(*args: object, **kwargs: object) -> None:
            self.assertFalse(app._running)
            self.assertEqual(args, (app._server_ready,))
            self.assertEqual(kwargs, {"timeout_seconds": 900.0, "ready_label": "server readiness"})
            call_order.append("ready")

        async def start_players() -> None:
            call_order.append("players")

        async def start_activities() -> None:
            call_order.append("activities")

        app.wait_for_ready_event = wait_for_ready_event
        app._players = SimpleNamespace(start=AsyncMock(side_effect=start_players))
        app._activities = SimpleNamespace(start=AsyncMock(side_effect=start_activities))
        app._running = False

        tailer = SimpleNamespace(start=AsyncMock())
        with patch("apps.sevendays.Tailer", return_value=tailer) as tailer_cls:
            result = await SevenDays.start(app)

        self.assertTrue(result)
        self.assertTrue(app._running)
        self.assertEqual(call_order, ["ready", "players", "activities"])
        app._std_launch.assert_awaited_once()
        app._relay.setup.assert_awaited_once()
        tailer.start.assert_awaited_once_with(app._tail_matchers)
        tailer_cls.assert_called_once()
        tailer_args = tailer_cls.call_args.args
        self.assertEqual(tailer_args[1:], (relay_reader, app.file_stdout))

    async def test_start_prefers_launch_created_runtime_log_file_over_existing_timestamped_logs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "7DaysToDieServer_Data"
            log_dir.mkdir(parents=True)
            older_runtime_log = log_dir / "output_log__2026-06-17__02-27-35.txt"
            older_runtime_log.write_text("old\n", encoding="utf-8")
            runtime_log = log_dir / "output_log__2026-06-17__03-27-35.txt"
            app = cast(Any, object.__new__(SevenDays))
            app.name = "sevendays_demo"
            app.directory = root
            app.server_log = None
            app.file_stdout = root / "stdout.log"
            app.process = SimpleNamespace(stdout=object())
            app._server_ready = asyncio.Event()
            app._tail_matchers = set()
            app._std_launch = AsyncMock(
                side_effect=lambda: runtime_log.write_text("INF Version: V 2.0 (b1)\n", encoding="utf-8")
            )
            app.check_running = lambda: True
            app._relay = SimpleNamespace(
                setup=AsyncMock(return_value=object()),
                connected_event=asyncio.Event(),
            )
            app.wait_for_ready_event = AsyncMock()
            app._players = SimpleNamespace(start=AsyncMock())
            app._activities = SimpleNamespace(start=AsyncMock())
            app._running = False

            tailer = SimpleNamespace(start=AsyncMock())
            with (
                patch("apps.sevendays.Tailer", return_value=tailer) as tailer_cls,
                patch("apps.sevendays.File_Utils.link") as link_mock,
            ):
                result = await SevenDays.start(app)

        self.assertTrue(result)
        tailer_cls.assert_called_once()
        self.assertEqual(tailer_cls.call_args.args[1:], (runtime_log, app.file_stdout))
        link_mock.assert_called_once_with(runtime_log, app.file_stdout.with_name(runtime_log.name))

    async def test_start_falls_back_to_existing_runtime_log_when_no_new_log_is_discovered(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_log = root / "7DaysToDieServer_Data" / "output_log__2026-06-17__02-27-35.txt"
            runtime_log.parent.mkdir(parents=True)
            runtime_log.write_text("INF Version: V 2.0 (b1)\n", encoding="utf-8")
            app = cast(Any, object.__new__(SevenDays))
            app.name = "sevendays_demo"
            app.directory = root
            app.server_log = None
            app.file_stdout = root / "stdout.log"
            app.process = SimpleNamespace(stdout=object())
            app._server_ready = asyncio.Event()
            app._tail_matchers = set()
            app._std_launch = AsyncMock()
            app.check_running = lambda: True
            app._relay = SimpleNamespace(
                setup=AsyncMock(return_value=object()),
                connected_event=asyncio.Event(),
            )
            app.wait_for_ready_event = AsyncMock()
            app._players = SimpleNamespace(start=AsyncMock())
            app._activities = SimpleNamespace(start=AsyncMock())
            app._running = False

            tailer = SimpleNamespace(start=AsyncMock())
            with (
                patch("apps.sevendays.Tailer", return_value=tailer) as tailer_cls,
                patch("apps.sevendays.File_Utils.link") as link_mock,
                patch("apps.sevendays._SEVENDAYS_RUNTIME_LOG_DISCOVERY_TIMEOUT_SECONDS", 0.0),
            ):
                result = await SevenDays.start(app)

        self.assertTrue(result)
        tailer_cls.assert_called_once()
        self.assertEqual(tailer_cls.call_args.args[1:], (runtime_log, app.file_stdout))
        link_mock.assert_called_once_with(runtime_log, app.file_stdout.with_name(runtime_log.name))


if __name__ == "__main__":
    unittest.main()
