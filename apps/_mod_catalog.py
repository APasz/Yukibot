"""Source-neutral mod inventory primitives.

The existing :mod:`apps._mod` implementation remains the local-filesystem
source.  This module deliberately keeps source identity and inventory data
separate from that implementation so a remote source does not need to imitate
filesystem paths or placement-marker filenames.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from apps._config import (
    ClientPackConfig,
    ClientPackPolicy,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageLink,
    ModPlacement,
    ModPlatformMetadata,
    ModType,
)
from apps._mod import Mod, Mod_Manager


class ModSourceKind(StrEnum):
    """A backend that supplies managed mods for an app."""

    LOCAL = "local"


class ModAction(StrEnum):
    """A mutation that a source can support for one inventory entry."""

    ENABLE = "enable"
    DISABLE = "disable"
    TOGGLE_COREMOD = "toggle_coremod"
    TOGGLE_DOWNLOAD_BLOCK = "toggle_download_block"
    UPDATE_PROPERTIES = "update_properties"
    UPDATE_NOTES = "update_notes"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class ModReference:
    """A stable, source-qualified identifier for one managed mod."""

    source: ModSourceKind
    source_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, ModSourceKind):
            raise TypeError("Mod reference source must be a ModSourceKind.")
        if not isinstance(self.source_key, str) or not self.source_key or "\x00" in self.source_key:
            raise ValueError("Mod reference source key must be non-empty text without NUL characters.")

    @classmethod
    def local(cls, name: str) -> ModReference:
        """Build the reference used by the legacy local mod manager."""

        return cls(source=ModSourceKind.LOCAL, source_key=name)

    @property
    def id(self) -> str:
        """Return a URL-safe opaque representation suitable for API clients."""

        encoded_key = base64.urlsafe_b64encode(self.source_key.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{self.source.value}:{encoded_key}"

    @classmethod
    def from_id(cls, raw: object) -> ModReference:
        """Parse an API identifier emitted by :attr:`id`."""

        if not isinstance(raw, str):
            raise TypeError("Mod reference ID must be text.")
        source_text, separator, encoded_key = raw.partition(":")
        if not separator or not source_text or not encoded_key:
            raise ValueError("Mod reference ID is invalid.")
        try:
            source = ModSourceKind(source_text)
        except ValueError as xcp:
            raise ValueError("Mod reference source is unsupported.") from xcp
        padding = "=" * (-len(encoded_key) % 4)
        try:
            decoded_key = base64.b64decode(
                encoded_key + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as xcp:
            raise ValueError("Mod reference ID is invalid.") from xcp
        return cls(source=source, source_key=decoded_key)


@dataclass(frozen=True, slots=True)
class ModArtifact:
    """A local artifact supplied by a source, if one is available."""

    path: Path
    archive_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("Mod artifact path must be a Path.")
        if not isinstance(self.archive_name, str) or not self.archive_name:
            raise ValueError("Mod artifact archive name must be non-empty text.")


@dataclass(frozen=True, slots=True)
class ModInventoryEntry:
    """A source-neutral snapshot rendered by mod-management clients.

    ``artifact`` is intentionally optional: remote sources can report a
    configured or mounted mod without exposing a local download path.
    """

    reference: ModReference
    available_actions: frozenset[ModAction]
    name: str
    friendly: str
    enabled: bool
    mod_type: ModType
    coremod: bool
    downloadable: bool
    download_block_reason: ModDownloadBlockReason | None
    download_block_label: str | None
    origin: str
    version: str | None
    added: datetime
    placement: ModPlacement
    server_loadable: bool
    client_pack_eligible: bool
    artifact: ModArtifact | None
    description: str | None = None
    notes: str | None = None
    client_path: Path | None = None
    mod_pages: tuple[ModPageLink, ...] = ()
    metadata_overrides: ModMetadataOverrides = field(default_factory=ModMetadataOverrides)
    client_pack: ClientPackConfig = field(default_factory=ClientPackConfig)
    platforms: ModPlatformMetadata = field(default_factory=ModPlatformMetadata)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Mod inventory entry name must be non-empty.")
        if not self.friendly:
            raise ValueError("Mod inventory entry friendly name must be non-empty.")
        if any(not isinstance(action, ModAction) for action in self.available_actions):
            raise TypeError("Mod inventory entry actions must be ModAction values.")
        if self.enabled is not self.placement.enabled:
            raise ValueError("Mod inventory entry enabled state conflicts with placement.")
        if self.server_loadable is not self.placement.server_loadable:
            raise ValueError("Mod inventory entry server-loadable state conflicts with placement.")

    @property
    def artifact_available(self) -> bool:
        return self.artifact is not None and self.artifact.path.exists()

    @classmethod
    def from_local_mod(cls, mod: Mod) -> ModInventoryEntry:
        """Build the source-neutral view of one legacy filesystem mod."""

        artifact_path = mod.storage_path
        artifact = ModArtifact(path=artifact_path, archive_name=mod.logical_archive_name)
        return cls(
            reference=ModReference.local(mod.name),
            available_actions=_local_available_actions(mod),
            name=mod.name,
            friendly=mod.friendly,
            enabled=mod.cfg.enabled,
            mod_type=mod.mod_type,
            coremod=mod.is_coremod_type,
            downloadable=mod.downloadable,
            download_block_reason=mod.download_block_reason,
            download_block_label=mod.download_block_label,
            origin=mod.origin,
            version=mod.version,
            added=mod.added,
            placement=mod.cfg.placement,
            server_loadable=mod.server_loadable,
            client_pack_eligible=mod.client_pack_eligible,
            artifact=artifact,
            description=mod.description,
            notes=mod.cfg.notes,
            client_path=mod.client_path,
            mod_pages=mod.cfg.mod_pages,
            metadata_overrides=mod.cfg.metadata_overrides,
            client_pack=mod.cfg.client_pack,
            platforms=mod.cfg.platforms,
        )


def _local_available_actions(mod: Mod) -> frozenset[ModAction]:
    actions = {
        ModAction.UPDATE_NOTES,
        ModAction.DELETE,
    }
    if not mod.is_builtin:
        actions.update(
            {
                ModAction.TOGGLE_COREMOD,
                ModAction.UPDATE_PROPERTIES,
            }
        )
        if not mod.downloadable or mod.cfg.client_pack.policy is ClientPackPolicy.REQUIRED:
            actions.add(ModAction.TOGGLE_DOWNLOAD_BLOCK)
    if mod.server_loadable:
        actions.add(ModAction.DISABLE if mod.cfg.enabled else ModAction.ENABLE)
    return frozenset(actions)


class ModInventorySource(Protocol):
    """A source that contributes source-neutral entries to an app catalog."""

    @property
    def kind(self) -> ModSourceKind: ...

    async def refresh(self) -> None: ...

    def list_entries(self) -> tuple[ModInventoryEntry, ...]: ...


class LocalModSource:
    """Adapter exposing ``Mod_Manager`` through the source-neutral catalog."""

    kind = ModSourceKind.LOCAL

    def __init__(self, manager: Mod_Manager) -> None:
        self._manager = manager

    async def refresh(self) -> None:
        await self._manager.reload_mods()

    def list_entries(self) -> tuple[ModInventoryEntry, ...]:
        return tuple(ModInventoryEntry.from_local_mod(mod) for mod in self._manager.list_mods())


class ModCatalog:
    """Aggregates inventory sources while preserving source-qualified identity."""

    def __init__(self, sources: Iterable[ModInventorySource]) -> None:
        source_values = tuple(sources)
        source_kinds = tuple(source.kind for source in source_values)
        if not source_values:
            raise ValueError("Mod catalog requires at least one source.")
        if any(not isinstance(kind, ModSourceKind) for kind in source_kinds):
            raise TypeError("Mod catalog source kinds must be ModSourceKind values.")
        if len(source_kinds) != len(set(source_kinds)):
            raise ValueError("Mod catalog source kinds must be unique.")
        self._sources = source_values

    @property
    def sources(self) -> tuple[ModInventorySource, ...]:
        return self._sources

    async def refresh(self) -> None:
        await asyncio.gather(*(source.refresh() for source in self._sources))

    def list_entries(self) -> tuple[ModInventoryEntry, ...]:
        """Return entries in configured-source order and source-owned entry order."""

        entries: list[ModInventoryEntry] = []
        references: set[ModReference] = set()
        for source in self._sources:
            for entry in source.list_entries():
                if not isinstance(entry, ModInventoryEntry):
                    raise TypeError("Mod catalog sources must return ModInventoryEntry values.")
                if entry.reference.source is not source.kind:
                    raise ValueError("Mod catalog source returned an entry for a different source kind.")
                if entry.reference in references:
                    raise ValueError("Mod catalog contains duplicate source references.")
                references.add(entry.reference)
                entries.append(entry)
        return tuple(entries)

    def get(self, reference: ModReference | str) -> ModInventoryEntry:
        if isinstance(reference, str):
            resolved_reference = ModReference.from_id(reference)
        elif isinstance(reference, ModReference):
            resolved_reference = reference
        else:
            raise TypeError("Mod catalog reference must be a ModReference or ID string.")
        for entry in self.list_entries():
            if entry.reference == resolved_reference:
                return entry
        raise LookupError(f"No such managed mod: {resolved_reference.id}")


__all__ = (
    "LocalModSource",
    "ModAction",
    "ModArtifact",
    "ModCatalog",
    "ModInventoryEntry",
    "ModInventorySource",
    "ModReference",
    "ModSourceKind",
)
