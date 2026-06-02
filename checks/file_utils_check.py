import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _file import File_Utils


class FileUtilsTests(unittest.TestCase):
    def test_link_creates_relative_symlink_for_absolute_source(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            src = root / "source" / "data.txt"
            src.parent.mkdir()
            src.write_text("hello", encoding="utf-8")
            dst = root / "links" / "data.txt"
            dst.parent.mkdir()

            linked = File_Utils.link(src, dst)

            self.assertEqual(linked, src)
            self.assertTrue(dst.is_symlink())
            self.assertEqual(Path(os.readlink(dst)), Path("..") / "source" / "data.txt")
            self.assertEqual(dst.resolve(strict=True), src.resolve(strict=True))

    def test_pointer_size_returns_zero_for_broken_symlink(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "missing.txt"
            pointer = root / "missing-link.txt"
            pointer.symlink_to(target)

            self.assertEqual(File_Utils.pointer_size(pointer), 0)


if __name__ == "__main__":
    unittest.main()
