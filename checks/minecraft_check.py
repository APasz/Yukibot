from __future__ import annotations

import unittest
from pathlib import Path

from apps._config import Mod_Config
from apps.minecraft import Mod_MC, _detect_minecraft_mod_version


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


if __name__ == "__main__":
    unittest.main()
