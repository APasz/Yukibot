import os
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import hikari

import config

from _file import File_Utils


class FileUtilsTests(unittest.TestCase):
    def test_extract_treats_a_broken_symlink_as_an_existing_destination(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "Example.zip"
            destination = root / "Mods/Example"
            destination.parent.mkdir()
            destination.symlink_to(root / "missing")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ModInfo.xml", "<xml/>")
            with self.assertRaises(FileExistsError):
                File_Utils.extract(archive_path, destination.parent, overwrite=False)
            extracted = File_Utils.extract(archive_path, destination.parent, overwrite=None)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(extracted, destination.with_name("Example_1"))
            File_Utils.extract(archive_path, destination.parent)
            self.assertFalse(destination.is_symlink())
            self.assertTrue((destination / "ModInfo.xml").is_file())

    def test_extract_restores_previous_destination_if_publication_fails(self) -> None:
        original_rename = Path.rename

        def fail_publication(source: Path, target: Path) -> Path:
            if source.name == "contents":
                raise OSError("Publication failed")
            return original_rename(source, target)

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "Example.zip"
            destination = root / "Mods/Example"
            destination.mkdir(parents=True)
            (destination / "original.txt").write_text("keep")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ModInfo.xml", "<xml/>")
            with patch.object(Path, "rename", new=fail_publication):
                with self.assertRaisesRegex(OSError, "Publication failed"):
                    File_Utils.extract(archive_path, destination.parent)
            self.assertEqual((destination / "original.txt").read_text(), "keep")
            self.assertEqual(tuple(destination.parent.iterdir()), (destination,))

    def test_normalise_archive_member_path_accepts_windows_and_mixed_separators(self) -> None:
        for member_name in ("ExampleMod\\Config\\settings.xml", "ExampleMod\\Config/settings.xml"):
            with self.subTest(member_name=member_name):
                self.assertEqual(
                    File_Utils._normalise_archive_member_path(member_name),
                    Path("ExampleMod/Config/settings.xml"),
                )

    def test_normalise_archive_member_path_rejects_unsafe_paths(self) -> None:
        for member_name in (
            "../outside.txt",
            "..\\outside.txt",
            "ExampleMod\\../outside.txt",
            "ExampleMod/..\\outside.txt",
            "C:/outside.txt",
            "C:\\outside.txt",
            "C:outside.txt",
            "/outside.txt",
            "\\outside.txt",
            "\\\\server\\share\\outside.txt",
        ):
            with self.subTest(member_name=member_name), self.assertRaisesRegex(ValueError, "member path is invalid"):
                File_Utils._normalise_archive_member_path(member_name)

    def test_extract_zip_restores_windows_directories_and_mixed_separators(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "ExampleMod.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ExampleMod\\", "")
                archive.writestr("ExampleMod\\Config\\", "")
                archive.writestr("ExampleMod\\Empty\\", "")
                archive.writestr("ExampleMod\\ModInfo.xml", "<mod />")
                archive.writestr("ExampleMod\\Config/settings.xml", "<settings />")

            extracted_path = File_Utils.extract(archive_path, root / "Mods")

            self.assertEqual((extracted_path / "ModInfo.xml").read_text(), "<mod />")
            self.assertEqual((extracted_path / "Config/settings.xml").read_text(), "<settings />")
            self.assertTrue((extracted_path / "Empty").is_dir())
            self.assertFalse(any("\\" in path.name for path in extracted_path.rglob("*")))

    def test_extract_zip_rejects_conflicting_normalised_paths(self) -> None:
        for first, second in (
            ("ExampleMod/ModInfo.xml", "ExampleMod\\ModInfo.xml"),
            ("ExampleMod\\Config", "ExampleMod/Config/settings.xml"),
            ("ExampleMod/Config/settings.xml", "ExampleMod\\Config"),
            ("ExampleMod\\Config\\", "ExampleMod/Config"),
            ("ExampleMod\\Config", "ExampleMod/Config/"),
        ):
            with self.subTest(first=first, second=second), TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                archive_path = root / "ExampleMod.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(first, "first")
                    archive.writestr(second, "second")

                with self.assertRaisesRegex(ValueError, "conflicting paths"):
                    File_Utils.extract(archive_path, root / "Mods")
                self.assertEqual(tuple((root / "Mods").iterdir()), ())

    def test_failed_extraction_preserves_destination(self) -> None:
        for existing_destination in (False, True):
            with self.subTest(existing_destination=existing_destination), TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                archive_path = root / "ExampleMod.zip"
                destination = root / "Mods" / archive_path.stem
                if existing_destination:
                    destination.mkdir(parents=True)
                    (destination / "original.txt").write_text("original", encoding="utf-8")
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("ExampleMod/ModInfo.xml", "<mod />")
                    archive.writestr("..\\outside.txt", "unsafe")

                with self.assertRaisesRegex(ValueError, "member path is invalid"):
                    File_Utils.extract(archive_path, destination.parent)

                if existing_destination:
                    self.assertEqual((destination / "original.txt").read_text(), "original")
                    self.assertEqual(len(tuple(destination.iterdir())), 1)
                else:
                    self.assertFalse(destination.exists())
                self.assertFalse((root / "outside.txt").exists())

    def test_extract_replaces_destination_after_successful_staging(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "ExampleMod.zip"
            destination = root / "Mods" / archive_path.stem
            destination.mkdir(parents=True)
            (destination / "original.txt").write_text("original", encoding="utf-8")
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("ExampleMod/ModInfo.xml", "<mod />")

            extracted = File_Utils.extract(archive_path, destination.parent)

            self.assertEqual(extracted, destination)
            self.assertEqual((destination / "ModInfo.xml").read_text(), "<mod />")
            self.assertFalse((destination / "original.txt").exists())
            self.assertEqual(tuple(destination.parent.iterdir()), (destination,))

    def test_extract_preserves_existing_destination_without_overwrite(self) -> None:
        for overwrite in (False, None):
            with self.subTest(overwrite=overwrite), TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                archive_path = root / "ExampleMod.zip"
                destination = root / "Mods" / archive_path.stem
                destination.mkdir(parents=True)
                (destination / "original.txt").write_text("original", encoding="utf-8")
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("ModInfo.xml", "<mod />")

                if overwrite is False:
                    with self.assertRaises(FileExistsError):
                        File_Utils.extract(archive_path, destination.parent, overwrite)
                    self.assertEqual(tuple(destination.parent.iterdir()), (destination,))
                else:
                    extracted = File_Utils.extract(archive_path, destination.parent, overwrite)
                    self.assertEqual(extracted, destination.with_name("ExampleMod_1"))
                    self.assertEqual((extracted / "ModInfo.xml").read_text(), "<mod />")
                self.assertEqual((destination / "original.txt").read_text(), "original")

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
