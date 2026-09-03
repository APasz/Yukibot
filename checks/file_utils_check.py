import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import hikari

import config

from _file import File_Utils


class FileUtilsTests(unittest.TestCase):
    def test_normalise_archive_member_path_rejects_windows_path_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "member path is invalid"):
            File_Utils._normalise_archive_member_path("..\\outside.txt")

    def test_normalise_archive_member_path_rejects_windows_drive_qualified_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "member path is invalid"):
            File_Utils._normalise_archive_member_path("C:/outside.txt")

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

    def test_extract_7z_uses_archive_stem_and_unwraps_single_root_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "No Blood Moon Limit.7z"
            archive_path.write_bytes(b"7z-data")
            mods_dir = root / "Mods"
            mods_dir.mkdir()

            def fake_extract(archive: Path, staging_dir: Path) -> None:
                self.assertEqual(archive, archive_path)
                mod_root = staging_dir / "Inner Mod Name"
                mod_root.mkdir()
                (mod_root / "ModInfo.xml").write_text("<mod />", encoding="utf-8")

            with patch.object(File_Utils, "_extract_7z_archive", side_effect=fake_extract):
                extracted_path = File_Utils.extract(archive_path, mods_dir, overwrite=True)

            self.assertEqual(extracted_path, mods_dir / "No Blood Moon Limit")
            self.assertTrue((extracted_path / "ModInfo.xml").exists())

    def test_extract_7z_without_backend_raises_clear_error(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "ExampleMod.7z"
            archive_path.write_bytes(b"7z-data")
            staging_dir = root / "staging"
            staging_dir.mkdir()

            with (
                patch("_file.importlib.util.find_spec", return_value=None),
                patch("_file.shutil.which", return_value=None),
            ):
                with self.assertRaises(ValueError) as raised:
                    File_Utils._extract_7z_archive(archive_path, staging_dir)

        self.assertIn("py7zr", str(raised.exception))


class FileUtilsAsyncTests(unittest.IsolatedAsyncioTestCase):
    class _AttachmentStream:
        def __init__(self, chunks: tuple[bytes, ...]) -> None:
            self._chunks = chunks
            self._index = 0

        async def __aenter__(self) -> "FileUtilsAsyncTests._AttachmentStream":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            del exc_type, exc, tb
            return False

        def __aiter__(self) -> "FileUtilsAsyncTests._AttachmentStream":
            return self

        async def __anext__(self) -> bytes:
            if self._index >= len(self._chunks):
                raise StopAsyncIteration
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk

    @staticmethod
    def _make_attachment(filename: str, chunks: tuple[bytes, ...]) -> hikari.Attachment:
        stream = FileUtilsAsyncTests._AttachmentStream(chunks)
        return cast(
            hikari.Attachment,
            cast(
                object,
                SimpleNamespace(
                    filename=filename,
                    stream=lambda: stream,
                ),
            ),
        )

    async def test_download_temp_uses_unique_paths_for_same_attachment_filename(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            first_attachment = self._make_attachment("cat.png", (b"first",))
            second_attachment = self._make_attachment("cat.png", (b"second",))

            with patch.object(config, "DIR_TMP", temp_root):
                first_path = await File_Utils.download_temp(first_attachment)
                second_path = await File_Utils.download_temp(second_attachment)

            try:
                self.assertNotEqual(first_path, second_path)
                self.assertEqual(first_path.read_bytes(), b"first")
                self.assertEqual(second_path.read_bytes(), b"second")
                self.assertEqual(first_path.suffix, ".png")
                self.assertEqual(second_path.suffix, ".png")
            finally:
                first_path.unlink(missing_ok=True)
                second_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
