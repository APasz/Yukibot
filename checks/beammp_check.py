import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._config import Mod_Config
from apps.beammp import Mod_BeamMP


class BeamMpModMetadataTests(unittest.TestCase):
    def test_prefers_package_metadata_for_mod_details(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "opaque-download-name.zip"
            with zipfile.ZipFile(directory / filename, "w") as archive:
                archive.writestr(
                    "mod_info/ABC123/info.json",
                    json.dumps(
                        {
                            "title": "Repository Vehicle Pack",
                            "version_string": "2.4.1",
                            "resource_url": "https://www.beamng.com/resources/example.1234/",
                        }
                    ),
                )
                archive.writestr(
                    "levels/example/info.json",
                    json.dumps({"title": "Bundled Level", "version": "1.0"}),
                )
            mod = Mod_BeamMP(Mod_Config(name=filename, directory=directory))

            mod.sync_metadata()

        self.assertEqual(mod.friendly, "Repository Vehicle Pack")
        self.assertEqual(mod.version, "2.4.1")
        self.assertEqual(len(mod.cfg.mod_pages), 1)
        self.assertEqual(mod.cfg.mod_pages[0].url, "https://www.beamng.com/resources/example.1234/")

    def test_uses_single_level_metadata_when_package_metadata_is_absent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "download.zip"
            with zipfile.ZipFile(directory / filename, "w") as archive:
                archive.writestr(
                    "levels/coastal_run/info.json",
                    json.dumps({"title": "Coastal Run", "version": "1.7"}),
                )
            mod = Mod_BeamMP(Mod_Config(name=filename, directory=directory))

            mod.sync_metadata()

        self.assertEqual(mod.friendly, "Coastal Run")
        self.assertEqual(mod.version, "1.7")

    def test_multiple_content_entries_fall_back_to_filename(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "mixed-content_3.2.0.zip"
            with zipfile.ZipFile(directory / filename, "w") as archive:
                archive.writestr("levels/first/info.json", json.dumps({"title": "First"}))
                archive.writestr("vehicles/second/info.json", json.dumps({"Name": "Second"}))
            mod = Mod_BeamMP(Mod_Config(name=filename, directory=directory))

            mod.sync_metadata()

        self.assertEqual(mod.friendly, "Mixed Content 3.2.0")
        self.assertEqual(mod.version, "3.2.0")


if __name__ == "__main__":
    unittest.main()
