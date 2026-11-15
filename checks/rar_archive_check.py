from io import BytesIO
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from tempfile import SpooledTemporaryFile, TemporaryFile
from threading import Event, Timer
import time
from typing import BinaryIO, cast
from unittest.mock import patch

import pytest
import rarfile

from archive_safety import ZipArchiveLimits, _rar_decoder_stream, extract_rar_entries, open_rar_archive, validated_rar_entries
from checks.rar_helpers import RAR_FIXTURES, UNRAR_AVAILABLE, stored_rar


@pytest.mark.parametrize("name", ["../outside.txt", "C:\\outside.txt", "/outside.txt", "..\\outside.txt"])
def test_rar_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="member path is invalid"):
        with open_rar_archive(stored_rar(((name, b"data"),)), archive_label="Test", limits=ZipArchiveLimits(), temporary_directory=tmp_path) as archive:
            validated_rar_entries(archive, archive_label="Test", limits=ZipArchiveLimits())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600])
def test_rar4_rejects_nonregular_members(tmp_path: Path, mode: int) -> None:
    with pytest.raises(ValueError, match="only regular files"):
        with open_rar_archive(stored_rar((("link", b"outside"),), mode=mode), archive_label="Test", limits=ZipArchiveLimits(), temporary_directory=tmp_path) as archive:
            validated_rar_entries(archive, archive_label="Test", limits=ZipArchiveLimits())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("filename", ["symlink-rar5.rar", "hardlink-rar5.rar"])
def test_rar5_rejects_links(filename: str) -> None:
    with pytest.raises(ValueError, match="only regular files"):
        with open_rar_archive(RAR_FIXTURES / filename, archive_label="Test", limits=ZipArchiveLimits()) as archive:
            validated_rar_entries(archive, archive_label="Test", limits=ZipArchiveLimits())


@pytest.mark.parametrize("filename", ["encrypted-data.rar", "encrypted-headers.rar"])
def test_rar_rejects_encryption(filename: str) -> None:
    with pytest.raises(ValueError, match="encrypted"):
        with open_rar_archive(RAR_FIXTURES / filename, archive_label="Test", limits=ZipArchiveLimits()):
            pytest.fail("Encrypted archive was accepted")


def test_rar_rejects_split_volumes_without_opening_later_parts() -> None:
    with pytest.raises(ValueError, match="single-volume"):
        with open_rar_archive(RAR_FIXTURES / "split.part1.rar", archive_label="Test", limits=ZipArchiveLimits()):
            pytest.fail("Split archive was accepted")


def test_rar_rejects_members_without_declared_sizes() -> None:
    with pytest.raises(ValueError, match="must declare their extracted sizes"):
        with open_rar_archive(RAR_FIXTURES / "unknown-size.rar", archive_label="Test", limits=ZipArchiveLimits()):
            pytest.fail("Unknown-size archive was accepted")


@pytest.mark.parametrize("limits,match", [
    (ZipArchiveLimits(member_count=1), "member count"),
    (ZipArchiveLimits(file_count=1), "file count"),
    (ZipArchiveLimits(file_bytes=1), "member is too large"),
    (ZipArchiveLimits(extracted_bytes=3), "extracted size"),
])
def test_rar_enforces_extraction_limits(tmp_path: Path, limits: ZipArchiveLimits, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        with open_rar_archive(stored_rar((("first", b"aa"), ("second", b"bb"))), archive_label="Test", limits=limits, temporary_directory=tmp_path) as archive:
            validated_rar_entries(archive, archive_label="Test", limits=limits)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("filename", ["bundle-rar4.rar", "bundle-solid-rar5.rar"])
def test_rar_enforces_compression_ratio_limits(filename: str) -> None:
    limits = ZipArchiveLimits(compression_ratio=1)
    with pytest.raises(ValueError, match="compression ratio"):
        with open_rar_archive(RAR_FIXTURES / filename, archive_label="Test", limits=limits) as archive:
            validated_rar_entries(archive, archive_label="Test", limits=limits)


def test_rar_reuses_a_linux_spool_without_copying(tmp_path: Path) -> None:
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("Linux descriptor paths are unavailable")
    limits = ZipArchiveLimits()
    with SpooledTemporaryFile(mode="w+b") as spool:
        spool.write(stored_rar((("inner/hello.txt", b"hello"),)).getvalue())
        spool.seek(0)
        with patch("archive_safety.tempfile.NamedTemporaryFile", side_effect=AssertionError("Copied the spool")):
            with open_rar_archive(cast(BinaryIO, cast(object, spool)), archive_label="Test", limits=limits) as archive:
                entries = validated_rar_entries(archive, archive_label="Test", limits=limits)
                extract_rar_entries(archive, entries, tmp_path)
    assert (tmp_path / "inner/hello.txt").read_bytes() == b"hello"


def test_rar_flushes_pending_file_writes(tmp_path: Path) -> None:
    limits = ZipArchiveLimits()
    with TemporaryFile(mode="w+b") as spool:
        spool.write(stored_rar((("hello.txt", b"hello"),)).getvalue())
        with open_rar_archive(spool, archive_label="Test", limits=limits) as archive:
            extract_rar_entries(archive, validated_rar_entries(archive, archive_label="Test", limits=limits), tmp_path)
    assert (tmp_path / "hello.txt").read_bytes() == b"hello"


@pytest.mark.parametrize("names", [
    ("same", "same"),
    ("Config/first", "config/second"),
    ("Config", "Config/second"),
    ("Config/first", "Config"),
])
def test_rar_rejects_conflicting_paths_before_extraction(tmp_path: Path, names: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="duplicate paths|conflicting paths"):
        with open_rar_archive(stored_rar(tuple((name, b"data") for name in names)), archive_label="Test", limits=ZipArchiveLimits(), temporary_directory=tmp_path) as archive:
            validated_rar_entries(archive, archive_label="Test", limits=ZipArchiveLimits())
    assert not list(tmp_path.iterdir())


def test_rar_rejects_truncated_member_data_before_extraction(tmp_path: Path) -> None:
    contents = stored_rar((("hello.txt", b"hello"),)).getvalue()[:-10]
    with pytest.raises(ValueError, match="damaged|truncated"):
        with open_rar_archive(BytesIO(contents), archive_label="Test", limits=ZipArchiveLimits(), temporary_directory=tmp_path):
            pytest.fail("Truncated member data was accepted")
    assert not list(tmp_path.iterdir())


def test_rar5_rejects_a_truncated_end_marker(tmp_path: Path) -> None:
    contents = (RAR_FIXTURES / "bundle-rar5.rar").read_bytes()[:-1]
    with pytest.raises(ValueError, match="damaged|truncated"):
        with open_rar_archive(BytesIO(contents), archive_label="Test", limits=ZipArchiveLimits(), temporary_directory=tmp_path):
            pytest.fail("Truncated RAR5 archive was accepted")
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not UNRAR_AVAILABLE, reason="Compressed RAR checks require unrar")
def test_solid_rar_is_decoded_without_reopening_each_member(tmp_path: Path) -> None:
    limits = ZipArchiveLimits()
    with (
        patch.object(rarfile.RarFile, "open", side_effect=AssertionError("Started a per-file decoder")),
        patch("archive_safety.subprocess.Popen", wraps=subprocess.Popen) as decoder,
    ):
        with open_rar_archive(RAR_FIXTURES / "bundle-solid-rar5.rar", archive_label="Test", limits=limits) as archive:
            extract_rar_entries(archive, validated_rar_entries(archive, archive_label="Test", limits=limits), tmp_path)
        decoder.assert_called_once()
    assert (tmp_path / "Wrapper/Mods/Second/Resources/asset.unity3d").read_bytes() == b"asset data" * 64


@pytest.mark.parametrize("output,exit_code,match", [
    (b"x", 2, "decoder exited 2"),
    (b"xy", 0, "beyond the declared"),
])
def test_rar_decoder_rejects_failed_or_excess_output(tmp_path: Path, output: bytes, exit_code: int, match: str) -> None:
    script = tmp_path / "decoder.py"
    script.write_text(f"import sys\nsys.stdout.buffer.write({output!r})\nsys.stdout.flush()\nsys.exit({exit_code})\n")
    with pytest.raises(ValueError, match=match):
        with _rar_decoder_stream([sys.executable, str(script)], cancelled=None) as stream:
            assert stream.read(1) == b"x"


@pytest.mark.skipif(os.name != "posix", reason="Checks POSIX child process reaping")
def test_rar_decoder_cancellation_interrupts_blocked_reads_and_reaps_the_child(tmp_path: Path) -> None:
    script = tmp_path / "decoder.py"
    script.write_text("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(5)\n")
    cancelled = Event()
    timer = Timer(0.05, cancelled.set)
    started_at = time.monotonic()
    process_id: int | None = None
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            with _rar_decoder_stream([sys.executable, str(script)], cancelled=cancelled) as stream:
                process_id = int(stream.readline())
                timer.start()
    finally:
        timer.cancel()
        if timer.ident is not None:
            timer.join()
    assert time.monotonic() - started_at < 2
    assert process_id is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(process_id, os.WNOHANG)


@pytest.mark.skipif(shutil.which("bsdtar") is None, reason="Checks the alternate bsdtar backend")
def test_compressed_rar_keeps_alternate_backend_support(tmp_path: Path) -> None:
    limits = ZipArchiveLimits()
    backend = rarfile.ToolSetup(rarfile.BSDTAR_CONFIG)
    with patch("rarfile.tool_setup", return_value=backend):
        with open_rar_archive(RAR_FIXTURES / "single-rar4.rar", archive_label="Test", limits=limits) as archive:
            extract_rar_entries(archive, validated_rar_entries(archive, archive_label="Test", limits=limits), tmp_path)
    assert (tmp_path / "First/Resources/asset.unity3d").read_bytes() == b"asset data" * 64


def test_rar_temporary_copy_is_removed_after_success_and_failure(tmp_path: Path) -> None:
    limits = ZipArchiveLimits()
    for contents in (stored_rar((("hello.txt", b"hello"),)).getvalue(), b"damaged"):
        try:
            with open_rar_archive(BytesIO(contents), archive_label="Test", limits=limits, temporary_directory=tmp_path) as archive:
                assert len(validated_rar_entries(archive, archive_label="Test", limits=limits)) == 1
                assert len(list(tmp_path.glob("yukibot-rar-*"))) == 1
        except ValueError:
            assert contents == b"damaged"
        assert not list(tmp_path.iterdir())


def test_rar_missing_backend_has_a_readable_error(tmp_path: Path) -> None:
    limits = ZipArchiveLimits()
    with patch("rarfile.tool_setup", side_effect=rarfile.RarCannotExec("Missing decoder")):
        with pytest.raises(ValueError, match="requires.*unrar"):
            with open_rar_archive(RAR_FIXTURES / "bundle-rar5.rar", archive_label="Test", limits=limits) as archive:
                entries = validated_rar_entries(archive, archive_label="Test", limits=limits)
                extract_rar_entries(archive, entries, tmp_path)
    assert not list(tmp_path.iterdir())


def test_rar_cancellation_cleans_up_the_temporary_archive(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        with open_rar_archive(stored_rar((("hello.txt", b"hello"),)), archive_label="Test", limits=ZipArchiveLimits(), temporary_directory=tmp_path, cancelled=cancelled):
            pytest.fail("Cancelled archive was accepted")
    assert not list(tmp_path.iterdir())


def test_rar_data_corruption_fails_during_extraction(tmp_path: Path) -> None:
    contents = bytearray(stored_rar((("hello.txt", b"hello"),)).getvalue())
    contents[contents.index(b"hello", contents.index(b"hello.txt") + len(b"hello.txt"))] ^= 1
    limits = ZipArchiveLimits()
    with pytest.raises(ValueError, match="damaged"):
        with open_rar_archive(BytesIO(contents), archive_label="Test", limits=limits, temporary_directory=tmp_path) as archive:
            extract_rar_entries(archive, validated_rar_entries(archive, archive_label="Test", limits=limits), tmp_path)
    assert not list(tmp_path.glob("yukibot-rar-*"))
