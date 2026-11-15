"""Small stored RAR4 fixtures that do not require an external decoder."""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
import shutil
import stat
import struct
import zlib

import rarfile

RAR_FIXTURES = Path(__file__).parent / "fixtures" / "rar"
UNRAR_AVAILABLE = shutil.which("unrar") is not None


def stored_rar(
    entries: Sequence[tuple[str, bytes]],
    *,
    main_flags: int = 0,
    member_flags: int = 0,
    mode: int = stat.S_IFREG | 0o644,
    declared_size: int | None = None,
) -> BytesIO:
    """Build real RAR headers, including deliberately invalid metadata for checks."""
    def header(kind: int, flags: int, payload: bytes = b"") -> bytes:
        data = struct.pack("<BHH", kind, flags, 7 + len(payload)) + payload
        return struct.pack("<H", zlib.crc32(data) & 0xFFFF) + data

    contents = bytearray(rarfile.RAR_ID)
    contents.extend(header(rarfile.RAR_BLOCK_MAIN, main_flags, b"\0" * 6))
    for name, data in entries:
        encoded_name = name.encode("utf-8")
        flags = rarfile.RAR_LONG_BLOCK | member_flags
        if name.endswith("/"):
            flags |= rarfile.RAR_FILE_DIRECTORY
        payload = struct.pack(
            "<IIBIIBBHI", len(data), len(data) if declared_size is None else declared_size,
            rarfile.RAR_OS_UNIX, zlib.crc32(data), 0, 20, rarfile.RAR_M0, len(encoded_name), mode,
        ) + encoded_name
        contents.extend(header(rarfile.RAR_BLOCK_FILE, flags, payload))
        contents.extend(data)
    contents.extend(header(rarfile.RAR_BLOCK_ENDARC, 0))
    return BytesIO(contents)
