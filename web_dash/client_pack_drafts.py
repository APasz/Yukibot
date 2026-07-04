from __future__ import annotations

from pathlib import Path
from typing import cast

from diskcache import Cache

from apps._config import CLIENT_PACK_CHANGELOG_MAX_LENGTH

type ClientPackDraftKey = tuple[str, str, str]


class ClientPackDraftStore:
    """Persistent shared client-pack changelog drafts owned by the dashboard."""

    def __init__(self, directory: Path | None) -> None:
        self._cache = Cache(directory=None if directory is None else str(directory))

    @staticmethod
    def _key(node_name: str, app_name: str) -> ClientPackDraftKey:
        return (
            "client_pack_changelog",
            node_name.strip().casefold(),
            app_name.strip().casefold(),
        )

    def get(self, *, node_name: str, app_name: str) -> str | None:
        value = cast(object, self._cache.get(self._key(node_name, app_name)))
        if value is None:
            return None
        if not isinstance(value, str):
            raise RuntimeError("Stored client-pack changelog draft is invalid.")
        return value

    def set(self, *, node_name: str, app_name: str, changelog: str) -> None:
        normalised_changelog = changelog.strip()
        if len(normalised_changelog) > CLIENT_PACK_CHANGELOG_MAX_LENGTH:
            raise ValueError(
                f"Client-pack changelog must be at most {CLIENT_PACK_CHANGELOG_MAX_LENGTH} characters."
            )
        stored = self._cache.set(
            self._key(node_name, app_name),
            normalised_changelog,
            retry=True,
        )
        if not stored:
            raise RuntimeError("Failed to persist the client-pack changelog draft.")

    def clear(self, *, node_name: str, app_name: str) -> None:
        self._cache.delete(self._key(node_name, app_name), retry=True)

    def close(self) -> None:
        self._cache.close()


__all__ = ("ClientPackDraftStore",)
