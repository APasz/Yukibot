"""Source-neutral mod inventory primitives.

The existing :mod:`apps._mod` implementation remains the local-filesystem
source.  This module deliberately keeps source identity and inventory data
separate from that implementation so a remote source does not need to imitate
filesystem paths or placement-marker filenames.
"""

from __future__ import annotations

import asyncio
import base64
import logging
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

log = logging.getLogger(__name__)


class ModSourceKind(StrEnum):
    """A backend that supplies managed mods for an app."""

    LOCAL = "local"
    STEAM_WORKSHOP = "steam_workshop"

    @property
    def label(self) -> str:
        """Return the concise source name shown to operators."""

        match self:
            case ModSourceKind.LOCAL:
                return "Local"
            case ModSourceKind.STEAM_WORKSHOP:
                return "Steam Workshop"


class ModSourceRefreshPolicy(StrEnum):
    """How an inventory source handles a known external refresh failure."""

    FAIL_FAST = "fail_fast"
    RETAIN_LAST_GOOD = "retain_last_good"


class ModSourceRefreshError(RuntimeError):
    """An expected, user-safe failure while refreshing an external source.

    Sources only use this for anticipated external failures such as a timeout,
    unavailable endpoint, or malformed provider response.  Programming and
    local filesystem failures deliberately retain their original exception
    types and remain visible to callers.
    """


@dataclass(frozen=True, slots=True)
class ModSourceStatus:
    """Source-neutral health information for an aggregated mod inventory."""

    source: ModSourceKind
    label: str
    healthy: bool = True
    warning: str | None = None
    using_cached_entries: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, ModSourceKind):
            raise TypeError("Mod source status source must be a ModSourceKind.")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Mod source status label must be non-empty text.")
        if not isinstance(self.healthy, bool):
            raise TypeError("Mod source status healthy must be a bool.")
        if self.warning is not None and (not isinstance(self.warning, str) or not self.warning.strip()):
            raise ValueError("Mod source status warning must be non-empty text when set.")
        if not isinstance(self.using_cached_entries, bool):
            raise TypeError("Mod source status cached-entry flag must be a bool.")
        if self.healthy and self.warning is not None:
            raise ValueError("Healthy mod sources cannot carry a warning.")
        if self.healthy and self.using_cached_entries:
            raise ValueError("Healthy mod sources cannot report cached fallback entries.")
        if not self.healthy and self.warning is None:
            raise ValueError("Unhealthy mod sources require a warning.")

    @classmethod
    def ready(cls, source: ModSourceKind, *, label: str | None = None) -> ModSourceStatus:
        """Build a healthy status for one configured source."""

        return cls(source=source, label=source.label if label is None else label)

    @classmethod
    def unavailable(
        cls,
        source: ModSourceKind,
        *,
        warning: str,
        label: str | None = None,
        using_cached_entries: bool = False,
    ) -> ModSourceStatus:
        """Build a warning status without naming a provider-specific error type."""

        return cls(
            source=source,
            label=source.label if label is None else label,
            healthy=False,
            warning=warning,
            using_cached_entries=using_cached_entries,
        )


class ModAction(StrEnum):
    """An action that a source can support for one inventory entry."""

    DOWNLOAD = "download"
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
    client_required: bool = False
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
        if not isinstance(self.client_required, bool):
            raise TypeError("Mod inventory entry client-required state must be a bool.")
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
    if mod.downloadable:
        actions.add(ModAction.DOWNLOAD)
    return frozenset(actions)


class ModInventorySource(Protocol):
    """A source that contributes source-neutral entries to an app catalog."""

    @property
    def kind(self) -> ModSourceKind: ...

    @property
    def label(self) -> str: ...

    @property
    def refresh_policy(self) -> ModSourceRefreshPolicy: ...

    @property
    def status(self) -> ModSourceStatus: ...

    async def refresh(self) -> None: ...

    def list_entries(self) -> tuple[ModInventoryEntry, ...]: ...


class LocalModSource:
    """Adapter exposing ``Mod_Manager`` through the source-neutral catalog."""

    kind = ModSourceKind.LOCAL
    label = ModSourceKind.LOCAL.label
    refresh_policy = ModSourceRefreshPolicy.FAIL_FAST

    def __init__(self, manager: Mod_Manager) -> None:
        self._manager = manager

    async def refresh(self) -> None:
        await self._manager.reload_mods()

    @property
    def status(self) -> ModSourceStatus:
        return ModSourceStatus.ready(self.kind, label=self.label)

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
        self._source_snapshots: dict[ModSourceKind, tuple[ModInventoryEntry, ...]] = {}
        self._source_statuses: dict[ModSourceKind, ModSourceStatus] = {
            source.kind: self._status_for_source(source) for source in source_values
        }

    @property
    def sources(self) -> tuple[ModInventorySource, ...]:
        return self._sources

    @property
    def source_statuses(self) -> tuple[ModSourceStatus, ...]:
        """Return health information in configured-source order."""

        return tuple(self._source_statuses[source.kind] for source in self._sources)

    async def refresh(self) -> None:
        results = await asyncio.gather(*(source.refresh() for source in self._sources), return_exceptions=True)
        fatal_errors: list[BaseException] = []
        for source, result in zip(self._sources, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                if (
                    isinstance(result, ModSourceRefreshError)
                    and self._refresh_policy_for_source(source) is ModSourceRefreshPolicy.RETAIN_LAST_GOOD
                ):
                    self._record_expected_refresh_failure(source=source, error=result)
                    continue
                fatal_errors.append(result)
                continue
            entries = self._entries_for_source(source)
            self._source_snapshots[source.kind] = entries
            self._source_statuses[source.kind] = self._status_for_source(source)
        if fatal_errors:
            raise fatal_errors[0]

    def list_entries(self) -> tuple[ModInventoryEntry, ...]:
        """Return entries in configured-source order and source-owned entry order."""

        entries: list[ModInventoryEntry] = []
        references: set[ModReference] = set()
        for source in self._sources:
            source_entries = self._source_snapshots.get(source.kind)
            if source_entries is None:
                source_entries = self._entries_for_source(source)
            for entry in source_entries:
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

    @staticmethod
    def _label_for_source(source: ModInventorySource) -> str:
        raw_label = getattr(source, "label", source.kind.label)
        if not isinstance(raw_label, str) or not raw_label.strip():
            raise ValueError("Mod catalog source labels must be non-empty text.")
        return raw_label

    @staticmethod
    def _refresh_policy_for_source(source: ModInventorySource) -> ModSourceRefreshPolicy:
        raw_policy = getattr(source, "refresh_policy", ModSourceRefreshPolicy.FAIL_FAST)
        if not isinstance(raw_policy, ModSourceRefreshPolicy):
            raise TypeError("Mod catalog source refresh policies must be ModSourceRefreshPolicy values.")
        return raw_policy

    def _status_for_source(self, source: ModInventorySource) -> ModSourceStatus:
        raw_status = getattr(source, "status", None)
        if raw_status is None:
            return ModSourceStatus.ready(source.kind, label=self._label_for_source(source))
        if not isinstance(raw_status, ModSourceStatus):
            raise TypeError("Mod catalog source status must be a ModSourceStatus value.")
        if raw_status.source is not source.kind:
            raise ValueError("Mod catalog source status belongs to a different source kind.")
        return raw_status

    @staticmethod
    def _entries_for_source(source: ModInventorySource) -> tuple[ModInventoryEntry, ...]:
        source_entries = source.list_entries()
        if not isinstance(source_entries, tuple):
            raise TypeError("Mod catalog sources must return entries as a tuple.")
        for entry in source_entries:
            if not isinstance(entry, ModInventoryEntry):
                raise TypeError("Mod catalog sources must return ModInventoryEntry values.")
            if entry.reference.source is not source.kind:
                raise ValueError("Mod catalog source returned an entry for a different source kind.")
        return source_entries

    def _record_expected_refresh_failure(
        self,
        *,
        source: ModInventorySource,
        error: ModSourceRefreshError,
    ) -> None:
        has_snapshot = source.kind in self._source_snapshots
        warning = str(error).strip() or f"{self._label_for_source(source)} unavailable."
        if has_snapshot:
            warning = f"{warning.rstrip('.;')}; showing cached data."
        self._source_statuses[source.kind] = ModSourceStatus.unavailable(
            source.kind,
            label=self._label_for_source(source),
            warning=warning,
            using_cached_entries=has_snapshot,
        )
        log.warning("Mod inventory source refresh failed: source=%s warning=%s", source.kind.value, warning)


__all__ = (
    "LocalModSource",
    "ModAction",
    "ModArtifact",
    "ModCatalog",
    "ModInventoryEntry",
    "ModInventorySource",
    "ModReference",
    "ModSourceRefreshError",
    "ModSourceRefreshPolicy",
    "ModSourceKind",
    "ModSourceStatus",
)
