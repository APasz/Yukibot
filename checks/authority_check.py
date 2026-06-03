from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _authority import read_json_object, write_json_object


class AuthorityJsonWriteTests(unittest.TestCase):
    def test_write_json_object_persists_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "authority" / "bot_registry.json"

            write_json_object(path, {"beta": 2, "alpha": 1})

            self.assertEqual(read_json_object(path), {"alpha": 1, "beta": 2})

    def test_write_json_object_preserves_existing_file_when_replace_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot_registry.json"
            path.write_text(json.dumps({"existing": True}), encoding="utf-8")
            path_type = type(path)

            with patch.object(path_type, "replace", autospec=True, side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_json_object(path, {"updated": True})

            self.assertEqual(read_json_object(path), {"existing": True})
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
