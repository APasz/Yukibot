"""Filesystem safety checks for persistent caches that deserialize stored values."""

from __future__ import annotations

import stat
from pathlib import Path


def prepare_private_cache_directory(directory: Path | None) -> str | None:
    """Create and validate a cache directory that no other local user can write."""
    if directory is None:
        return None
    if directory.is_symlink():
        raise ValueError(f"Cache directory must not be a symlink: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = directory.stat().st_mode
    if not stat.S_ISDIR(mode):
        raise ValueError(f"Cache path is not a directory: {directory}")
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"Cache directory must not be group- or world-writable: {directory}")
    return str(directory)
