import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps._config import AppVersion
from apps.ets import detect_ets_version


class ETSVersionDetectionTests(unittest.TestCase):
    def test_detect_ets_version_prefers_game_version_line(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "home_data" / "Euro Truck Simulator 2"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "server.log.txt"
            log_path.write_text(
                "\n".join(
                    (
                        "00:00:00.002 : [ufs] Loaded pack set version 1.55.0.3 created at 1749652225",
                        "00:00:02.039 : [MP] Game version: 1.55s",
                    )
                ),
                encoding="utf-8",
            )

            version = detect_ets_version(directory=root, server_log=log_path)

        self.assertEqual(version, AppVersion(main="1.55s"))

    def test_detect_ets_version_falls_back_to_pack_set_version(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "home_data" / "Euro Truck Simulator 2"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "server.log.txt"
            log_path.write_text(
                "00:00:00.002 : [ufs] Loaded pack set version 1.55.0.3 created at 1749652225\n",
                encoding="utf-8",
            )

            version = detect_ets_version(directory=root, server_log=log_path)

        self.assertEqual(version, AppVersion(main="1.55.0.3"))


if __name__ == "__main__":
    unittest.main()
