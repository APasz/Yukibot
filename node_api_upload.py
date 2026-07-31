"""Shared temporary-file handling for streamed node API uploads."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import UploadFile


async def persist_upload_to_temp(upload: UploadFile) -> Path:
    """Write an upload to a temporary file and close its request stream."""
    suffix = Path(upload.filename or "upload").suffix
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="yukibot-upload-", suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
            while chunk := await upload.read(1024 * 1024):
                handle.write(chunk)
            return temp_path
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
