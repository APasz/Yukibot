from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import config
from _relay_embeds import build_app_lifecycle_embed
from apps._config import Mod_Config, ModDownloadBlockReason, ModType
from apps.minecraft import Minecraft, Minecraft_Config, Mod_MC, _detect_minecraft_mod_version


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
            config_dir = directory / "config" / "squaremap"
            config_dir.mkdir(parents=True)
            (config_dir / "config.yml").write_text(
                "settings:\n  internal-webserver:\n    enabled: true\n    port: 8123\n",
                encoding=config.STR_ENCODE,
            )

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
            "Join: `play.example.com:25565`\n[Squaremap](https://maps.example.com:8123/?world=minecraft_overworld)",
        )


if __name__ == "__main__":
    unittest.main()
