"""Contracts shared by the node API's mod, metadata, and client-pack domains.

The node API service and its HTTP routes both use these objects.  Keeping the
wire contracts here lets each layer depend on the mod domain without importing
the large node API composition module.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

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
from apps._node_api import optional_string, required_bool, required_int, required_string
from apps.minecraft.pack_export import PackFormat, PackPurpose
from node_api_app_state import NodeAppRuntimeSummary


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
            "description": self.description,
            "notes": self.notes,
            "mod_pages": [page.model_dump(mode="json") for page in self.mod_pages],
            "metadata_overrides": self.metadata_overrides.model_dump(mode="json"),
            "client_pack": self.client_pack.model_dump(mode="json"),
            "platforms": self.platforms.model_dump(mode="json"),
        }


class NodeModMutationAction(StrEnum):
    ENABLE = "enable"
    DISABLE = "disable"
    TOGGLE_COREMOD = "toggle_coremod"
    TOGGLE_DOWNLOAD_BLOCK = "toggle_download_block"
    UPDATE_PROPERTIES = "update_properties"
    UPDATE_NOTES = "update_notes"
    DELETE = "delete"


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
    operation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
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
    discovery_operation_id: uuid.UUID
    apply_suggested_type_mod_names: tuple[str, ...] = ()

    @field_validator("apply_suggested_type_mod_names", mode="before")
    @classmethod
    def validate_apply_suggested_type_mod_names(cls, raw: object) -> object:
        if not isinstance(raw, (list, tuple)):
            raise TypeError("bulk launcher metadata type selections must be a list")
        return raw

    @model_validator(mode="after")
    def validate_type_selections(self) -> NodeBulkLauncherMetadataApplyRequest:
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


@dataclass(frozen=True, slots=True)
class CachedBulkMetadataDiscovery:
    captured_at_seconds: float
    discovery: BulkLauncherMetadataDiscovery


@dataclass(frozen=True, slots=True)
class NodeModList:
    app_name: str
    app_friendly: str
    node: str
    summary: NodeModSummary
    mods: tuple[NodeModEntry, ...]
    app_stats: NodeAppRuntimeSummary | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> NodeModList:
        app_name = required_string(payload, "app_name")
        app_friendly = required_string(payload, "app_friendly")
        node = required_string(payload, "node")
        raw_summary = payload.get("summary")
        raw_app_stats = payload.get("app_stats")
        if not isinstance(raw_summary, Mapping):
            raise ValueError("Node mod list summary is invalid.")
        if raw_app_stats is not None and not isinstance(raw_app_stats, Mapping):
            raise ValueError("Node mod list app_stats are invalid.")
        raw_mods = payload.get("mods")
        if not isinstance(raw_mods, Sequence) or isinstance(raw_mods, (str, bytes)):
            raise ValueError("Node mod list mods are invalid.")
        mods: list[NodeModEntry] = []
        for raw_mod in raw_mods:
            if not isinstance(raw_mod, Mapping):
                raise ValueError("Node mod list contains an invalid mod entry.")
            mods.append(NodeModEntry.from_mapping(raw_mod))
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            summary=NodeModSummary.from_mapping(raw_summary),
            mods=tuple(mods),
            app_stats=NodeAppRuntimeSummary.from_mapping(raw_app_stats) if raw_app_stats is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "summary": self.summary.to_mapping(),
            "mods": [mod.to_mapping() for mod in self.mods],
            "app_stats": self.app_stats.to_mapping() if self.app_stats is not None else None,
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
