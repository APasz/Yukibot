from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import config
from _relay_embeds import build_app_lifecycle_embed
from apps._config import Mod_Config, ModDownloadBlockReason, ModType
from apps.minecraft import Minecraft, Minecraft_Config, Mod_MC, _detect_minecraft_mod_version, _load_squaremap_web_address


class MinecraftModVersionDetectionTests(unittest.TestCase):
    def test_detects_versions_from_current_sample_file_names(self) -> None:
        cases = {
            "ChatImage-1.4.7+1.20.1+forge.jar": "1.4.7",
            "cloth-config-11.1.136-forge.jar": "11.1.136",
            "appleskin-forge-mc1.20.1-2.5.1.jar": "2.5.1",
            "chat_heads-0.13.18-forge-1.20.jar": "0.13.18",
            "Controlling-forge-1.20.1-12.0.2.jar": "12.0.2",
            "Jade-1.20.1-Forge-11.13.1.jar": "11.13.1",
            "modelfix-1.15.jar": "1.15",
            "modernfix-forge-5.23.1+mc1.20.1.jar": "5.23.1",
            "comforts-forge-6.4.0+1.20.1.jar": "6.4.0",
            "fallingleaves-1.20.1-2.1.2.jar": "2.1.2",
            "MouseTweaks-forge-mc1.20.1-2.25.1.jar": "2.25.1",
            "NoChatReports-FORGE-1.20.1-v2.2.2.jar": "2.2.2",
            "Searchables-forge-1.20.1-1.0.3.jar": "1.0.3",
        }

        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_detect_minecraft_mod_version(name), expected)

    def test_detects_human_friendly_names_from_current_sample_file_names(self) -> None:
        cases = {
            "ChatImage-1.4.7+1.20.1+forge.jar": "Chat Image",
            "cloth-config-11.1.136-forge.jar": "Cloth Config",
            "NoChatReports-FORGE-1.20.1-v2.2.2.jar": "No Chat Reports",
        }

        for name, expected in cases.items():
            with self.subTest(name=name):
                mod = Mod_MC(Mod_Config(name=name, directory=Path(".")))
                mod.sync_metadata()
                self.assertEqual(mod.friendly, expected)

    def test_squaremap_mod_is_server_only(self) -> None:
        mod = Mod_MC(Mod_Config(name="squaremap-forge-mc1.20.1-1.2.0.jar", directory=Path(".")))
        mod.sync_metadata()

        self.assertIs(mod.mod_type, ModType.SERVER_ONLY)
        self.assertIs(mod.cfg.download_block_reason, ModDownloadBlockReason.SERVER_ONLY)

    def test_started_embed_includes_squaremap_link(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.scope = "minecraft"
            app.directory = directory
            app.manage_embed_color = 0x22C55E
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                    Mod_MC(Mod_Config(name="squaremap-forge-mc1.20.1-1.2.0.jar", directory=directory / "mods"))
                    ]
                ),
            )
            app.cfg = Minecraft_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=directory,
                apps_dir=directory,
                scope="minecraft",
                join_host="play.example.com",
                join_port=25565,
            )

            with patch.object(config, "PUBLIC_BASE_URL", "https://maps.example.com"):
                embed = build_app_lifecycle_embed(app, started=True)

        self.assertEqual(
            embed.description,
            "Join: `play.example.com:25565`\n[Squaremap](https://maps.example.com/squaremap/?world=minecraft_overworld)",
        )

    def test_started_embed_omits_squaremap_link_when_mod_is_disabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.scope = "minecraft"
            app.directory = directory
            app.manage_embed_color = 0x22C55E
            disabled_squaremap_mod = Mod_MC(
                Mod_Config(
                    name="squaremap-forge-mc1.20.1-1.2.0.jar",
                    directory=directory / "mods",
                    enabled=False,
                )
            )
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [] if state is True else [disabled_squaremap_mod]
                ),
            )
            app.cfg = Minecraft_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=directory,
                apps_dir=directory,
                scope="minecraft",
                join_host="play.example.com",
                join_port=25565,
            )

            with patch.object(config, "PUBLIC_BASE_URL", "https://maps.example.com"):
                embed = build_app_lifecycle_embed(app, started=True)

        self.assertEqual(embed.description, "Join: `play.example.com:25565`")


class MinecraftBackgroundTaskCancellationTests(unittest.TestCase):
    def test_sync_kubejs_yuki_log_script_creates_bundled_script_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=state is not False,
                            )
                        )
                    ]
                ),
            )

            changed = app._sync_kubejs_yuki_log_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"

            self.assertTrue(changed)
            self.assertTrue(target_path.exists())
            self.assertIn("[YUKI_MC_EVENT]", target_path.read_text(encoding="utf-8"))

    def test_sync_kubejs_yuki_log_script_does_nothing_without_enabled_kubejs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: []
                    if state is True
                    else [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=False,
                            )
                        )
                    ]
                ),
            )

            changed = app._sync_kubejs_yuki_log_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"

            self.assertFalse(changed)
            self.assertFalse(target_path.exists())

    def test_load_squaremap_web_address_accepts_quoted_value_with_comment(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yml"
            config_path.write_text('web-address: "http://localhost:8080" # dev web root\n', encoding="utf-8")

            self.assertEqual(_load_squaremap_web_address(config_path), "http://localhost:8080")

    def test_cancel_background_task_handles_foreign_event_loop(self) -> None:
        app = object.__new__(Minecraft)
        app.name = "minecraft_alpha"

        task_ready = threading.Event()
        task_holder: dict[str, asyncio.Task[object]] = {}

        def run_task_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def wait_forever() -> None:
                current_task = asyncio.current_task()
                if current_task is None:
                    raise RuntimeError("Expected the background task to be available.")
                task_holder["task"] = current_task
                task_ready.set()
                await asyncio.Future()

            task = loop.create_task(wait_forever())
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()

        thread = threading.Thread(target=run_task_loop)
        thread.start()
        self.assertTrue(task_ready.wait(timeout=1.0))

        task = task_holder["task"]
        asyncio.run(app._cancel_background_task(task, label="test background task", timeout_seconds=1.0))

        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
