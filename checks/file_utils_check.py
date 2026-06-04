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
