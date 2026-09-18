"""Contracts shared by the node API's mod, metadata, and client-pack domains.

The node API service and its HTTP routes both use these objects.  Keeping the
wire contracts here lets each layer depend on the mod domain without importing
the large node API composition module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import UploadFile
from modmux.models import Provider
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.config import ConfigDict

from _security import Power_Level
from apps._config import (
    CLIENT_PACK_CHANGELOG_MAX_LENGTH,
    BulkLauncherMetadataDiscovery,
    ClientPackConfig,
    ClientPackKubeJsScript,
    ClientPackMetadataConfig,
    LauncherProviderUrls,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageLink,
    ModPlacement,
    ModPlatformMetadata,
    ModType,
    is_client_pack_candidate,
    normalise_client_pack_changelog,
)
from apps._node_api import optional_string, required_bool, required_int, required_string, string_tuple
from apps._mod_catalog import (
    ModAction,
    ModReference,
    ModSourceKind,
    ModSourceStatus,
)
from apps.minecraft.pack_export import PackFormat, PackPurpose
from .app_state import NodeAppRuntimeSummary

if TYPE_CHECKING:
    from apps.gmod.workshop import GmodWorkshopSourceState


@dataclass(frozen=True, slots=True)
class NodeModSummary:
    total_count: int
    enabled_count: int
    disabled_count: int
    coremod_count: int
    downloadable_count: int
    non_downloadable_count: int
    client_only_count: int = 0
    client_pack_eligible_count: int = 0

    @property
    def server_enabled_count(self) -> int:
        return self.enabled_count

    @property
    def server_disabled_count(self) -> int:
        return self.disabled_count

    @property
    def server_loadable_count(self) -> int:
        return self.server_enabled_count + self.server_disabled_count

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModSummary:
        values: dict[str, int] = {}
        for key in (
            "total_count",
            "enabled_count",
            "disabled_count",
            "coremod_count",
            "downloadable_count",
            "non_downloadable_count",
        ):
            value: object | None = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Node mod summary {key} is invalid.")
            values[key] = value
        raw_client_only_count: object = payload.get("client_only_count", 0)
        if isinstance(raw_client_only_count, bool) or not isinstance(raw_client_only_count, int):
            raise ValueError("Node mod summary client_only_count is invalid.")
        raw_client_pack_eligible_count: object = payload.get(
            "client_pack_eligible_count",
            values["downloadable_count"],
        )
        if isinstance(raw_client_pack_eligible_count, bool) or not isinstance(raw_client_pack_eligible_count, int):
            raise ValueError("Node mod summary client_pack_eligible_count is invalid.")
        return cls(
            **values,
            client_only_count=raw_client_only_count,
            client_pack_eligible_count=raw_client_pack_eligible_count,
        )

    def to_mapping(self) -> dict[str, int]:
        return {
            "total_count": self.total_count,
            "enabled_count": self.enabled_count,
            "disabled_count": self.disabled_count,
            "coremod_count": self.coremod_count,
            "downloadable_count": self.downloadable_count,
            "non_downloadable_count": self.non_downloadable_count,
            "server_enabled_count": self.server_enabled_count,
            "server_disabled_count": self.server_disabled_count,
            "server_loadable_count": self.server_loadable_count,
            "client_only_count": self.client_only_count,
            "client_pack_eligible_count": self.client_pack_eligible_count,
        }


@dataclass(frozen=True, slots=True)
class NodeGmodWorkshopSourceState:
    """Typed GMod Workshop state rendered alongside generic source health."""

    collection_id: str | None
    collection_title: str | None
    auto_update: bool
    server_mounted_count: int
    client_content_ids: tuple[str, ...]
    client_required_count: int

    def __post_init__(self) -> None:
        if self.collection_id is not None and (not isinstance(self.collection_id, str) or not self.collection_id):
            raise ValueError("Node GMod Workshop collection ID must be non-empty text when set.")
        if self.collection_title is not None and (
            not isinstance(self.collection_title, str) or not self.collection_title
        ):
            raise ValueError("Node GMod Workshop collection title must be non-empty text when set.")
        if self.collection_id is None and self.collection_title is not None:
            raise ValueError("Node GMod Workshop collection title requires a collection ID.")
        if not isinstance(self.auto_update, bool):
            raise TypeError("Node GMod Workshop auto-update state must be a bool.")
        for value, label in (
            (self.server_mounted_count, "server-mounted count"),
            (self.client_required_count, "client-required count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Node GMod Workshop {label} must be an integer.")
            if value < 0:
                raise ValueError(f"Node GMod Workshop {label} cannot be negative.")
        if not isinstance(self.client_content_ids, tuple):
            raise TypeError("Node GMod Workshop client content IDs must be a tuple.")
        if any(not isinstance(item_id, str) or not item_id for item_id in self.client_content_ids):
            raise ValueError("Node GMod Workshop client content IDs must be non-empty text.")
        if len(set(self.client_content_ids)) != len(self.client_content_ids):
            raise ValueError("Node GMod Workshop client content IDs must be unique.")
        if self.collection_id is not None and self.collection_id in self.client_content_ids:
            raise ValueError("Node GMod Workshop client content cannot include its configured collection ID.")
        if self.client_required_count != len(self.client_content_ids):
            raise ValueError("Node GMod Workshop client-required count must match explicit client content IDs.")

    @classmethod
    def from_source_state(cls, state: GmodWorkshopSourceState) -> NodeGmodWorkshopSourceState:
        """Build the API contract from GMod's typed source state."""

        return cls(
            collection_id=state.collection_id,
            collection_title=state.collection_title,
            auto_update=state.auto_update,
            server_mounted_count=state.server_mounted_count,
            client_content_ids=state.client_content_ids,
            client_required_count=state.client_required_count,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeGmodWorkshopSourceState:
        return cls(
            collection_id=optional_string(payload, "collection_id"),
            collection_title=optional_string(payload, "collection_title"),
            auto_update=required_bool(payload, "auto_update"),
            server_mounted_count=required_int(payload, "server_mounted_count"),
            client_content_ids=string_tuple(payload, "client_content_ids"),
            client_required_count=required_int(payload, "client_required_count"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "collection_title": self.collection_title,
            "auto_update": self.auto_update,
            "server_mounted_count": self.server_mounted_count,
            "client_content_ids": list(self.client_content_ids),
            "client_required_count": self.client_required_count,
        }


@dataclass(frozen=True, slots=True)
class NodeModSourceStatus:
    """Generic source health plus an optional typed source-specific state."""

    source: ModSourceKind
    label: str
    healthy: bool = True
    warning: str | None = None
    using_cached_entries: bool = False
    gmod_workshop_state: NodeGmodWorkshopSourceState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, ModSourceKind):
            raise TypeError("Node mod source status source must be a ModSourceKind.")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Node mod source status label must be non-empty text.")
        if not isinstance(self.healthy, bool):
            raise TypeError("Node mod source status healthy must be a bool.")
        if self.warning is not None and (not isinstance(self.warning, str) or not self.warning.strip()):
            raise ValueError("Node mod source status warning must be non-empty text when set.")
        if not isinstance(self.using_cached_entries, bool):
            raise TypeError("Node mod source status cached-entry flag must be a bool.")
        if self.gmod_workshop_state is not None:
            if not isinstance(self.gmod_workshop_state, NodeGmodWorkshopSourceState):
                raise TypeError("Node GMod Workshop source state must be a NodeGmodWorkshopSourceState.")
            if self.source is not ModSourceKind.STEAM_WORKSHOP:
                raise ValueError("Node GMod Workshop source state belongs to a different source.")
        if self.healthy and self.warning is not None:
            raise ValueError("Healthy node mod sources cannot carry a warning.")
        if self.healthy and self.using_cached_entries:
            raise ValueError("Healthy node mod sources cannot report cached fallback entries.")
        if not self.healthy and self.warning is None:
            raise ValueError("Unhealthy node mod sources require a warning.")

    @classmethod
    def from_source_status(
        cls,
        status: ModSourceStatus,
        *,
        gmod_workshop_state: NodeGmodWorkshopSourceState | None = None,
    ) -> NodeModSourceStatus:
        return cls(
            source=status.source,
            label=status.label,
            healthy=status.healthy,
            warning=status.warning,
            using_cached_entries=status.using_cached_entries,
            gmod_workshop_state=gmod_workshop_state,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModSourceStatus:
        raw_source = required_string(payload, "source")
        try:
            source = ModSourceKind(raw_source)
        except ValueError as xcp:
            raise ValueError("Node mod source status source is invalid.") from xcp
        raw_gmod_workshop_state = payload.get("gmod_workshop_state")
        if raw_gmod_workshop_state is not None and not isinstance(raw_gmod_workshop_state, Mapping):
            raise ValueError("Node GMod Workshop source state is invalid.")
        return cls(
            source=source,
            label=required_string(payload, "label"),
            healthy=required_bool(payload, "healthy"),
            warning=optional_string(payload, "warning"),
            using_cached_entries=(
                required_bool(payload, "using_cached_entries")
                if "using_cached_entries" in payload
                else False
            ),
            gmod_workshop_state=(
                None
                if raw_gmod_workshop_state is None
                else NodeGmodWorkshopSourceState.from_mapping(raw_gmod_workshop_state)
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "label": self.label,
            "healthy": self.healthy,
            "warning": self.warning,
            "using_cached_entries": self.using_cached_entries,
            "gmod_workshop_state": (
                None if self.gmod_workshop_state is None else self.gmod_workshop_state.to_mapping()
            ),
        }


@dataclass(frozen=True, slots=True)
class NodeModEntry:
    name: str
    friendly: str
    enabled: bool
    mod_type: ModType
    coremod: bool
    downloadable: bool
    download_block_reason: str | None
    download_block_label: str | None
    origin: str
    version: str | None
    added: str
    size_bytes: int
    size_text: str
    placement: ModPlacement
    server_loadable: bool
    client_pack_eligible: bool
    archive_name: str
    source_path: str
    description: str | None = None
    notes: str | None = None
    client_path: str | None = None
    mod_pages: tuple[ModPageLink, ...] = ()
    metadata_overrides: ModMetadataOverrides = field(default_factory=ModMetadataOverrides)
    client_pack: ClientPackConfig = field(default_factory=ClientPackConfig)
    platforms: ModPlatformMetadata = field(default_factory=ModPlatformMetadata)
    source: ModSourceKind = ModSourceKind.LOCAL
    source_key: str | None = None
    available_actions: tuple[ModAction, ...] = ()
    artifact_available: bool = True
    client_required: bool = False

    def __post_init__(self) -> None:
        # Preserve the compact legacy representation for ordinary local mods.
        # A distinct local key remains available for a future local source that
        # needs one beyond its logical filename.
        if self.source is ModSourceKind.LOCAL and self.source_key == self.name:
            object.__setattr__(self, "source_key", None)
        if not isinstance(self.client_required, bool):
            raise TypeError("Node mod client-required state must be a bool.")

    @property
    def reference(self) -> ModReference:
        return ModReference(
            source=self.source,
            source_key=self.name if self.source_key is None else self.source_key,
        )

    @property
    def id(self) -> str:
        return self.reference.id

    def supports_action(self, action: ModAction) -> bool:
        """Return whether this source explicitly permits an entry action.

        Legacy local payloads predate ``available_actions``.  Their fallback
        preserves the existing local UI while non-local entries remain
        strictly capability-driven.
        """

        if action in self.available_actions:
            return True
        if self.available_actions or self.source is not ModSourceKind.LOCAL:
            return False
        if action is ModAction.DOWNLOAD:
            return self.downloadable
        if action is ModAction.DELETE:
            return True
        if action is ModAction.UPDATE_NOTES:
            return True
        if action is ModAction.UPDATE_PROPERTIES:
            return self.mod_type is not ModType.BUILTIN
        if action is ModAction.TOGGLE_COREMOD:
            return self.mod_type is not ModType.BUILTIN
        if action is ModAction.TOGGLE_DOWNLOAD_BLOCK:
            return self.mod_type is not ModType.BUILTIN
        if action is ModAction.ENABLE:
            return self.server_loadable and not self.enabled
        if action is ModAction.DISABLE:
            return self.server_loadable and self.enabled
        raise ValueError(f"Unsupported mod action: {action!r}")

    @property
    def download_is_policy_blocked(self) -> bool:
        """Return whether download is explicitly blocked rather than unavailable.

        Remote inventory sources can intentionally expose no local artifact.
        Legacy local entries retain their historical non-downloadable treatment
        even when old payloads omitted a detailed block reason.
        """

        return not self.downloadable and (
            self.source is ModSourceKind.LOCAL
            or self.download_block_reason is not None
            or self.download_block_label is not None
        )

    @property
    def added_at(self) -> datetime:
        try:
            return datetime.fromisoformat(self.added)
        except ValueError as xcp:
            raise ValueError(f"Node mod {self.name!r} has an invalid added timestamp: {self.added!r}") from xcp

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModEntry:
        name: str = required_string(payload, "name")
        friendly: str = required_string(payload, "friendly")
        raw_source: str | None = optional_string(payload, "source")
        raw_id: str | None = optional_string(payload, "id")
        try:
            reference = ModReference.local(name) if raw_id is None else ModReference.from_id(raw_id)
        except (TypeError, ValueError) as xcp:
            raise ValueError("Node mod ID is invalid.") from xcp
        try:
            source = reference.source if raw_source is None else ModSourceKind(raw_source)
        except ValueError as xcp:
            raise ValueError("Node mod source is invalid.") from xcp
        if source is not reference.source:
            raise ValueError("Node mod source conflicts with its ID.")
        raw_available_actions: object = payload.get("available_actions", ())
        if isinstance(raw_available_actions, (str, bytes)) or not isinstance(raw_available_actions, Sequence):
            raise ValueError("Node mod available actions are invalid.")
        try:
            available_actions = tuple(ModAction(action) for action in raw_available_actions)
        except (TypeError, ValueError) as xcp:
            raise ValueError("Node mod available actions are invalid.") from xcp
        if len(available_actions) != len(set(available_actions)):
            raise ValueError("Node mod available actions must be unique.")
        raw_artifact_available: object = payload.get("artifact_available", True)
        if not isinstance(raw_artifact_available, bool):
            raise ValueError("Node mod artifact availability is invalid.")
        raw_client_required: object = payload.get("client_required", False)
        if not isinstance(raw_client_required, bool):
            raise ValueError("Node mod client-required state is invalid.")
        client_path: str | None = optional_string(payload, "client_path")
        enabled: bool = required_bool(payload, "enabled")
        coremod: bool = required_bool(payload, "coremod")
        raw_mod_type: str | None = optional_string(payload, "mod_type")
        downloadable: bool = required_bool(payload, "downloadable")
        download_block_reason: str | None = optional_string(payload, "download_block_reason")
        origin: str = required_string(payload, "origin")
        added: str = required_string(payload, "added")
        size_bytes: int = required_int(payload, "size_bytes")
        size_text: str = required_string(payload, "size_text")
        raw_client_pack: object | None = payload.get("client_pack")
        if raw_client_pack is not None and not isinstance(raw_client_pack, Mapping):
            raise ValueError("Node mod client_pack is invalid.")
        raw_metadata_overrides: object | None = payload.get("metadata_overrides")
        if raw_metadata_overrides is not None and not isinstance(raw_metadata_overrides, Mapping):
            raise ValueError("Node mod metadata overrides are invalid.")
        raw_mod_pages: object = payload.get("mod_pages", ())
        if not isinstance(raw_mod_pages, (list, tuple)):
            raise ValueError("Node mod pages are invalid.")
        raw_platforms: object | None = payload.get("platforms")
        if raw_platforms is not None and not isinstance(raw_platforms, Mapping):
            raise ValueError("Node mod platform metadata is invalid.")
        if raw_mod_type is not None:
            mod_type: ModType = ModType(raw_mod_type)
        elif download_block_reason == ModDownloadBlockReason.BUILTIN.value:
            mod_type = ModType.BUILTIN
        elif coremod:
            mod_type = ModType.COREMOD
        else:
            mod_type = ModType.REGULAR
        client_pack_payload: dict[Any, object] = {} if raw_client_pack is None else dict(raw_client_pack)
        client_pack_payload.setdefault("included_in_client", mod_type.included_in_client_by_default)
        client_pack: ClientPackConfig = ClientPackConfig.model_validate(client_pack_payload)
        raw_placement: str | None = optional_string(payload, "placement")
        placement: ModPlacement = (
            (ModPlacement.SERVER_ENABLED if enabled else ModPlacement.SERVER_DISABLED)
            if raw_placement is None
            else ModPlacement(raw_placement)
        )
        if raw_placement is not None and enabled is not placement.enabled:
            raise ValueError("Node mod enabled state conflicts with placement.")
        raw_server_loadable: object | None = payload.get("server_loadable")
        server_loadable: bool = (
            placement.server_loadable if raw_server_loadable is None else required_bool(payload, "server_loadable")
        )
        if server_loadable is not placement.server_loadable:
            raise ValueError("Node mod server_loadable conflicts with placement.")
        raw_client_pack_eligible: object | None = payload.get("client_pack_eligible")
        expected_client_pack_eligible: bool = (
            is_client_pack_candidate(placement, mod_type.side) and client_pack.included_in_client and downloadable
        )
        client_pack_eligible: bool = (
            expected_client_pack_eligible
            if raw_client_pack_eligible is None
            else required_bool(payload, "client_pack_eligible")
        )
        if client_pack_eligible is not expected_client_pack_eligible:
            raise ValueError("Node mod client_pack_eligible conflicts with classification.")
        return cls(
            name=name,
            friendly=friendly,
            client_path=client_path,
            enabled=enabled,
            mod_type=mod_type,
            coremod=coremod,
            downloadable=downloadable,
            download_block_reason=download_block_reason,
            download_block_label=optional_string(payload, "download_block_label"),
            origin=origin,
            version=optional_string(payload, "version"),
            added=added,
            size_bytes=size_bytes,
            size_text=size_text,
            placement=placement,
            server_loadable=server_loadable,
            client_pack_eligible=client_pack_eligible,
            archive_name=optional_string(payload, "archive_name") or name,
            source_path=optional_string(payload, "source_path") or client_path or name,
            description=optional_string(payload, "description"),
            notes=optional_string(payload, "notes"),
            mod_pages=tuple(
                ModPageLink.model_validate(page) for page in cast(list[object] | tuple[object, ...], raw_mod_pages)
            ),
            metadata_overrides=(
                ModMetadataOverrides()
                if raw_metadata_overrides is None
                else ModMetadataOverrides.model_validate(dict(raw_metadata_overrides))
            ),
            client_pack=client_pack,
            platforms=(
                ModPlatformMetadata()
                if raw_platforms is None
                else ModPlatformMetadata.model_validate(dict(raw_platforms))
            ),
            source=source,
            source_key=reference.source_key,
            available_actions=available_actions,
            artifact_available=raw_artifact_available,
            client_required=raw_client_required,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "friendly": self.friendly,
            "client_path": self.client_path,
            "enabled": self.enabled,
            "mod_type": self.mod_type.value,
            "coremod": self.coremod,
            "downloadable": self.downloadable,
            "download_block_reason": self.download_block_reason,
            "download_block_label": self.download_block_label,
            "origin": self.origin,
            "version": self.version,
            "added": self.added,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "placement": self.placement.value,
            "server_loadable": self.server_loadable,
            "client_pack_eligible": self.client_pack_eligible,
            "archive_name": self.archive_name,
            "source_path": self.source_path,
            "id": self.id,
            "source": self.source.value,
            "available_actions": [action.value for action in self.available_actions],
            "artifact_available": self.artifact_available,
            "client_required": self.client_required,
            "description": self.description,
            "notes": self.notes,
            "mod_pages": [page.model_dump(mode="json") for page in self.mod_pages],
            "metadata_overrides": self.metadata_overrides.model_dump(mode="json"),
            "client_pack": self.client_pack.model_dump(mode="json"),
            "platforms": self.platforms.model_dump(mode="json"),
        }


class NodeModMutationAction(StrEnum):
    """Local-route mutations, kept distinct from source-specific capabilities."""

    ENABLE = ModAction.ENABLE.value
    DISABLE = ModAction.DISABLE.value
    TOGGLE_COREMOD = ModAction.TOGGLE_COREMOD.value
    TOGGLE_DOWNLOAD_BLOCK = ModAction.TOGGLE_DOWNLOAD_BLOCK.value
    UPDATE_PROPERTIES = ModAction.UPDATE_PROPERTIES.value
    UPDATE_NOTES = ModAction.UPDATE_NOTES.value
    DELETE = ModAction.DELETE.value


def required_mod_mutation_level(
    action: NodeModMutationAction,
    *,
    is_protected: bool = False,
) -> Power_Level:
    if action in {NodeModMutationAction.ENABLE, NodeModMutationAction.DISABLE}:
        return Power_Level.sudo if is_protected else Power_Level.admin
    if action in {
        NodeModMutationAction.TOGGLE_COREMOD,
        NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK,
        NodeModMutationAction.UPDATE_PROPERTIES,
        NodeModMutationAction.DELETE,
    }:
        return Power_Level.sudo
    if action is NodeModMutationAction.UPDATE_NOTES:
        return Power_Level.admin
    raise ValueError(f"Unsupported mod mutation action: {action}")


class NodeModMutationRequest(BaseModel):
    action: NodeModMutationAction


class NodeGmodWorkshopCollectionUpdateRequest(BaseModel):
    """A source-level GMod Workshop collection mutation, not a mod-row action."""

    collection_id: str

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class NodeGmodWorkshopAutoUpdateRequest(BaseModel):
    """A source-level next-start auto-update mutation."""

    enabled: bool

    model_config = ConfigDict(extra="forbid")


class NodeGmodWorkshopClientContentUpdateRequest(BaseModel):
    """The full explicit resource.AddWorkshop ID set after an edit."""

    item_ids: tuple[str, ...]

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class NodeModPropertiesUpdateRequest(BaseModel):
    mod_type: ModType
    download_block_reason: ModDownloadBlockReason | None
    metadata_overrides: ModMetadataOverrides
    mod_pages: tuple[ModPageLink, ...] | None = None
    client_pack: ClientPackConfig | None = None
    launcher_urls: LauncherProviderUrls = Field(default_factory=LauncherProviderUrls)


class NodeModNotesUpdateRequest(BaseModel):
    notes: str | None = None

    model_config = ConfigDict(str_strip_whitespace=True)


class NodeLauncherProviderSelectionRequest(BaseModel):
    providers: tuple[Provider, ...] | None = None

    model_config = ConfigDict(extra="forbid")


class NodeModMetadataFetchRequest(NodeLauncherProviderSelectionRequest):
    launcher_urls: LauncherProviderUrls


class NodeModMetadataResolveRequest(NodeLauncherProviderSelectionRequest):
    mod_pages: tuple[ModPageLink, ...]
    existing_launcher_urls: LauncherProviderUrls = Field(default_factory=LauncherProviderUrls)


class NodeModPageResolveRequest(NodeLauncherProviderSelectionRequest):
    mod_pages: tuple[ModPageLink, ...]


class NodeBulkLauncherMetadataRequest(BaseModel):
    mod_names: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("mod_names", mode="before")
    @classmethod
    def validate_mod_names(cls, raw: object) -> object:
        if not isinstance(raw, (list, tuple)):
            raise TypeError("bulk launcher metadata mod names must be a list")
        return raw

    @model_validator(mode="after")
    def validate_unique_mod_names(self) -> NodeBulkLauncherMetadataRequest:
        if any(not name for name in self.mod_names):
            raise ValueError("bulk launcher metadata mod names must not be blank")
        if len(self.mod_names) != len(set(self.mod_names)):
            raise ValueError("bulk launcher metadata mod names must be unique")
        return self


class NodeBulkLauncherMetadataApplyRequest(NodeBulkLauncherMetadataRequest):
    discovery_operation_id: str = Field(min_length=1)
    apply_suggested_type_mod_names: tuple[str, ...] = ()

    @field_validator("apply_suggested_type_mod_names", mode="before")
    @classmethod
    def validate_apply_suggested_type_mod_names(cls, raw: object) -> object:
        if not isinstance(raw, (list, tuple)):
            raise TypeError("bulk launcher metadata type selections must be a list")
        return raw

    @model_validator(mode="after")
    def validate_type_selections(self) -> NodeBulkLauncherMetadataApplyRequest:
        if not self.mod_names:
            raise ValueError("bulk metadata apply requires at least one selected mod")
        selected_names = self.apply_suggested_type_mod_names
        if any(not name for name in selected_names):
            raise ValueError("bulk launcher metadata type selection names must not be blank")
        if len(selected_names) != len(set(selected_names)):
            raise ValueError("bulk launcher metadata type selection names must be unique")
        if not set(selected_names).issubset(self.mod_names):
            raise ValueError("bulk launcher metadata type selections must be selected for apply")
        return self


class NodeBulkLauncherMetadataApplyResult(BaseModel):
    discovery: BulkLauncherMetadataDiscovery
    applied_mod_names: tuple[str, ...] = ()
    applied_type_mod_names: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeClientPackModConfigUpdate(BaseModel):
    mod_name: str = Field(min_length=1)
    client_pack: ClientPackConfig


class NodeClientPackConfigUpdateRequest(BaseModel):
    mods: tuple[NodeClientPackModConfigUpdate, ...]
    kubejs_scripts: tuple[ClientPackKubeJsScript, ...] | None = None
    metadata: ClientPackMetadataConfig | None = None

    @model_validator(mode="after")
    def validate_unique_mod_names(self) -> NodeClientPackConfigUpdateRequest:
        mod_names = tuple(update.mod_name for update in self.mods)
        if len(mod_names) != len(set(mod_names)):
            raise ValueError("client-pack configuration contains duplicate mod names")
        if self.kubejs_scripts is not None:
            script_paths = tuple(script.relative_path for script in self.kubejs_scripts)
            if len(script_paths) != len(set(script_paths)):
                raise ValueError("client-pack configuration contains duplicate KubeJS script paths")
        return self


class NodeClientPackPublishRequest(BaseModel):
    changelog: str = Field(min_length=1, max_length=CLIENT_PACK_CHANGELOG_MAX_LENGTH)

    @field_validator("changelog", mode="before")
    @classmethod
    def validate_changelog(cls, value: object) -> str:
        changelog = normalise_client_pack_changelog(value, required=True)
        assert changelog is not None
        return changelog


@dataclass(frozen=True, slots=True)
class NodeModMutationResult:
    app_name: str
    app_friendly: str
    node: str
    mod_name: str
    action: NodeModMutationAction
    message: str
    mod: NodeModEntry | None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModMutationResult:
        app_name = required_string(payload, "app_name")
        app_friendly = required_string(payload, "app_friendly")
        node = required_string(payload, "node")
        mod_name = required_string(payload, "mod_name")
        message = required_string(payload, "message")
        raw_action = required_string(payload, "action")
        try:
            action = NodeModMutationAction(raw_action)
        except ValueError as xcp:
            raise ValueError("Node mod mutation action is invalid.") from xcp
        raw_mod = payload.get("mod")
        if raw_mod is not None and not isinstance(raw_mod, Mapping):
            raise ValueError("Node mod mutation mod is invalid.")
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            mod_name=mod_name,
            action=action,
            message=message,
            mod=NodeModEntry.from_mapping(raw_mod) if raw_mod is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "mod_name": self.mod_name,
            "action": self.action.value,
            "message": self.message,
            "mod": self.mod.to_mapping() if self.mod is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeModUploadResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    mod: NodeModEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModUploadResult:
        raw_mod = payload.get("mod")
        if not isinstance(raw_mod, Mapping):
            raise ValueError("Node mod upload mod is invalid.")
        return cls(
            app_name=required_string(payload, "app_name"),
            app_friendly=required_string(payload, "app_friendly"),
            node=required_string(payload, "node"),
            message=required_string(payload, "message"),
            mod=NodeModEntry.from_mapping(raw_mod),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "mod": self.mod.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class NodeModUploadBatchResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    mods: tuple[NodeModEntry, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModUploadBatchResult:
        raw_mods = payload.get("mods")
        if isinstance(raw_mods, str) or not isinstance(raw_mods, Sequence):
            raise ValueError("Node mod upload mods are invalid.")
        mods: list[NodeModEntry] = []
        for raw_mod in raw_mods:
            if not isinstance(raw_mod, Mapping):
                raise ValueError("Node mod upload mods are invalid.")
            mods.append(NodeModEntry.from_mapping(raw_mod))
        if not mods:
            raise ValueError("Node mod upload mods are invalid.")
        return cls(
            app_name=required_string(payload, "app_name"),
            app_friendly=required_string(payload, "app_friendly"),
            node=required_string(payload, "node"),
            message=required_string(payload, "message"),
            mods=tuple(mods),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "mods": [mod.to_mapping() for mod in self.mods],
        }


@dataclass(frozen=True, slots=True)
class ResolvedModUploadFile:
    upload: UploadFile
    upload_name: str


@dataclass(frozen=True, slots=True)
class TimedModInventory:
    captured_at_seconds: float
    summary: NodeModSummary
    mods: tuple[NodeModEntry, ...]
    source_statuses: tuple[NodeModSourceStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeModList:
    app_name: str
    app_friendly: str
    node: str
    summary: NodeModSummary
    mods: tuple[NodeModEntry, ...]
    app_stats: NodeAppRuntimeSummary | None = None
    source_statuses: tuple[NodeModSourceStatus, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModList:
        app_name = required_string(payload, "app_name")
        app_friendly = required_string(payload, "app_friendly")
        node = required_string(payload, "node")
        raw_summary = payload.get("summary")
        raw_app_stats = payload.get("app_stats")
        raw_source_statuses = payload.get("source_statuses", ())
        if not isinstance(raw_summary, Mapping):
            raise ValueError("Node mod list summary is invalid.")
        if raw_app_stats is not None and not isinstance(raw_app_stats, Mapping):
            raise ValueError("Node mod list app_stats are invalid.")
        if not isinstance(raw_source_statuses, Sequence) or isinstance(raw_source_statuses, (str, bytes)):
            raise ValueError("Node mod list source statuses are invalid.")
        raw_mods = payload.get("mods")
        if not isinstance(raw_mods, Sequence) or isinstance(raw_mods, (str, bytes)):
            raise ValueError("Node mod list mods are invalid.")
        mods: list[NodeModEntry] = []
        for raw_mod in raw_mods:
            if not isinstance(raw_mod, Mapping):
                raise ValueError("Node mod list contains an invalid mod entry.")
            mods.append(NodeModEntry.from_mapping(raw_mod))
        if len({mod.id for mod in mods}) != len(mods):
            raise ValueError("Node mod list contains duplicate source-qualified mod IDs.")
        source_statuses: list[NodeModSourceStatus] = []
        for raw_status in raw_source_statuses:
            if not isinstance(raw_status, Mapping):
                raise ValueError("Node mod list contains an invalid source status.")
            source_statuses.append(NodeModSourceStatus.from_mapping(raw_status))
        if len({status.source for status in source_statuses}) != len(source_statuses):
            raise ValueError("Node mod list source statuses must be unique by source.")
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            summary=NodeModSummary.from_mapping(raw_summary),
            mods=tuple(mods),
            app_stats=NodeAppRuntimeSummary.from_mapping(raw_app_stats) if raw_app_stats is not None else None,
            source_statuses=tuple(source_statuses),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "summary": self.summary.to_mapping(),
            "mods": [mod.to_mapping() for mod in self.mods],
            "app_stats": self.app_stats.to_mapping() if self.app_stats is not None else None,
            "source_statuses": [status.to_mapping() for status in self.source_statuses],
        }


@dataclass(frozen=True, slots=True)
class NodeDownloadRequest:
    enabled_only: bool = False
    mod_name: str | None = None
    mod_names: tuple[str, ...] = ()
    selected_only: bool = False
    excluded_only: bool = False
    client_pack: bool = False
    pack_purpose: PackPurpose | None = None
    pack_format: PackFormat = PackFormat.GENERIC_ZIP
    publish_client_pack: bool = False
    publish_changelog: str | None = None
    include_kubejs_scripts: bool = True
    include_servers_dat: bool = True
    include_options_txt: bool = True

    @property
    def resolved_pack_purpose(self) -> PackPurpose | None:
        if self.pack_purpose is not None:
            return self.pack_purpose
        if self.client_pack or self.pack_format is not PackFormat.GENERIC_ZIP:
            return PackPurpose.CLIENT
        return None


@dataclass(frozen=True, slots=True)
class NodeDownloadFile:
    path: Path
    filename: str
    is_archive: bool


@dataclass(frozen=True, slots=True)
class NodeModDownloadForm:
    action_url: str
    access_token: str | None
