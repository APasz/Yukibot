from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

from _utils import Utilities
from apps._app import App
from apps._blueprint_files import (
    AppBlueprintEntry,
    AppBlueprintFileEntry,
    AppBlueprintFileType,
    blueprint_file_type_from_name,
    classify_blueprint_upload_filenames,
)
from apps._node_api import (
    optional_string as _optional_string,
    required_bool as _required_bool,
    required_int as _required_int,
    required_string as _required_string,
)
from apps.satisfactory import Satisfactory
from node_api_upload import persist_upload_to_temp, validated_upload_filename

@dataclass(frozen=True, slots=True)
class NodeBlueprintFileEntry:
    id: str
    label: str
    relative_path: str
    size_bytes: int
    size_text: str
    modified_at: str
    uploaded_by_display_name: str | None
    can_delete: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintFileEntry":
        uploaded_by_display_name = _optional_string(payload, "uploaded_by_display_name")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            relative_path=_required_string(payload, "relative_path"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            uploaded_by_display_name=uploaded_by_display_name,
            can_delete=_required_bool(payload, "can_delete"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "uploaded_by_display_name": self.uploaded_by_display_name,
            "can_delete": self.can_delete,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintEntry:
    id: str
    label: str
    session_name: str
    relative_path: str
    size_bytes: int
    size_text: str
    modified_at: str
    uploaded_by_display_name: str | None
    can_delete: bool
    config_file: NodeBlueprintFileEntry | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintEntry":
        uploaded_by_display_name = _optional_string(payload, "uploaded_by_display_name")
        raw_config_file = payload.get("config_file")
        if raw_config_file is not None and not isinstance(raw_config_file, Mapping):
            raise ValueError("Node blueprint entry config_file is invalid.")
        return cls(
            id=_required_string(payload, "id"),
            label=_required_string(payload, "label"),
            session_name=_required_string(payload, "session_name"),
            relative_path=_required_string(payload, "relative_path"),
            size_bytes=_required_int(payload, "size_bytes"),
            size_text=_required_string(payload, "size_text"),
            modified_at=_required_string(payload, "modified_at"),
            uploaded_by_display_name=uploaded_by_display_name,
            can_delete=_required_bool(payload, "can_delete"),
            config_file=NodeBlueprintFileEntry.from_mapping(raw_config_file) if raw_config_file is not None else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "session_name": self.session_name,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "size_text": self.size_text,
            "modified_at": self.modified_at,
            "uploaded_by_display_name": self.uploaded_by_display_name,
            "can_delete": self.can_delete,
            "config_file": self.config_file.to_mapping() if self.config_file is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintList:
    app_name: str
    app_friendly: str
    node: str
    blueprints: tuple[NodeBlueprintEntry, ...]
    default_session_name: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintList":
        app_name = _required_string(payload, "app_name")
        app_friendly = _required_string(payload, "app_friendly")
        node = _required_string(payload, "node")
        default_session_name = _optional_string(payload, "default_session_name")
        raw_blueprints = payload.get("blueprints")
        if not isinstance(raw_blueprints, Sequence) or isinstance(raw_blueprints, (str, bytes)):
            raise ValueError("Node blueprint list blueprints are invalid.")
        blueprints: list[NodeBlueprintEntry] = []
        for raw_blueprint in raw_blueprints:
            if not isinstance(raw_blueprint, Mapping):
                raise ValueError("Node blueprint list contains an invalid blueprint entry.")
            blueprints.append(NodeBlueprintEntry.from_mapping(raw_blueprint))
        return cls(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node,
            blueprints=tuple(blueprints),
            default_session_name=default_session_name,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "blueprints": [entry.to_mapping() for entry in self.blueprints],
            "default_session_name": self.default_session_name,
        }


@dataclass(frozen=True, slots=True)
class NodeBlueprintMutationResult:
    app_name: str
    app_friendly: str
    node: str
    message: str
    blueprint: NodeBlueprintEntry

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeBlueprintMutationResult":
        raw_blueprint = payload.get("blueprint")
        if not isinstance(raw_blueprint, Mapping):
            raise ValueError("Node blueprint mutation result blueprint is invalid.")
        return cls(
            app_name=_required_string(payload, "app_name"),
            app_friendly=_required_string(payload, "app_friendly"),
            node=_required_string(payload, "node"),
            message=_required_string(payload, "message"),
            blueprint=NodeBlueprintEntry.from_mapping(raw_blueprint),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "app_friendly": self.app_friendly,
            "node": self.node,
            "message": self.message,
            "blueprint": self.blueprint.to_mapping(),
        }


class SatisfactoryBlueprintService:
    """Owns Satisfactory blueprint listing and mutation for the node API."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        can_sudo: Callable[[int], bool],
        require_sudo: Callable[[int], bool],
        display_name_for_user: Callable[[int], str],
        http_exception: Callable[[int, str], Exception],
        traffic_log: logging.Logger,
    ) -> None:
        self._node_name = node_name
        self._can_sudo = can_sudo
        self._require_sudo = require_sudo
        self._display_name_for_user = display_name_for_user
        self._http_exception = http_exception
        self._traffic_log = traffic_log

    def build_list(self, *, app: App, actor_user_id: int) -> NodeBlueprintList:
        satisfactory = self._require_satisfactory(app, action="blueprint files")
        blueprints = satisfactory.list_blueprint_files()
        self._traffic_log.info(
            "Node API built Satisfactory blueprint list: node=%s app=%s blueprints=%s",
            self._node_name(),
            satisfactory.name,
            len(blueprints),
        )
        return replace(
            self.build_empty_list(app=satisfactory),
            blueprints=tuple(
                self._blueprint_entry(blueprint, actor_user_id=actor_user_id) for blueprint in blueprints
            ),
        )

    def build_empty_list(self, *, app: App) -> NodeBlueprintList:
        satisfactory = self._require_satisfactory(app, action="blueprint files")
        return NodeBlueprintList(
            app_name=satisfactory.name,
            app_friendly=satisfactory.friendly,
            node=self._node_name(),
            blueprints=(),
            default_session_name=satisfactory.default_blueprint_session_name,
        )

    async def upload_files(
        self,
        *,
        app: App,
        session_name: str,
        uploads: list[UploadFile],
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult:
        satisfactory = self._require_satisfactory(app, action="blueprint uploads")
        resolved_names = [self._resolve_upload_name(upload.filename or "") for upload in uploads]
        try:
            upload_pair = classify_blueprint_upload_filenames(resolved_names)
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp

        temp_paths: dict[str, Path] = {}
        try:
            for upload, resolved_name in zip(uploads, resolved_names, strict=True):
                temp_paths[resolved_name] = await persist_upload_to_temp(upload)
            config_source_path = (
                temp_paths[upload_pair.config_filename] if upload_pair.config_filename is not None else None
            )
            return self.upload_path(
                app=satisfactory,
                session_name=session_name,
                source_path=temp_paths[upload_pair.module_filename],
                upload_name=upload_pair.module_filename,
                actor_user_id=actor_user_id,
                config_source_path=config_source_path,
                config_upload_name=upload_pair.config_filename,
            )
        finally:
            for temp_path in temp_paths.values():
                temp_path.unlink(missing_ok=True)

    def upload_path(
        self,
        *,
        app: App,
        session_name: str,
        source_path: Path,
        upload_name: str,
        actor_user_id: int,
        config_source_path: Path | None = None,
        config_upload_name: str | None = None,
    ) -> NodeBlueprintMutationResult:
        satisfactory = self._require_satisfactory(app, action="blueprint uploads")
        resolved_upload_names = [self._resolve_upload_name(upload_name)]
        if config_upload_name is not None:
            resolved_upload_names.append(self._resolve_upload_name(config_upload_name))
        try:
            upload_pair = classify_blueprint_upload_filenames(resolved_upload_names)
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        try:
            uploaded = satisfactory.upload_blueprint_file(
                session_name=session_name,
                upload_name=upload_pair.module_filename,
                source_path=source_path,
                actor_user_id=actor_user_id,
                config_upload_name=upload_pair.config_filename,
                config_source_path=config_source_path,
            )
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except FileExistsError as xcp:
            raise self._http_exception(409, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Blueprint upload failed: {xcp}") from xcp

        self._traffic_log.info(
            "Node API uploaded Satisfactory blueprint: node=%s app=%s blueprint=%s actor=%s",
            self._node_name(),
            satisfactory.name,
            uploaded.id,
            actor_user_id,
        )
        message = f"Uploaded blueprint `{uploaded.label}` for {satisfactory.friendly}."
        if upload_pair.config_filename is not None:
            message = (
                f"Uploaded blueprint `{uploaded.label}` with config `{upload_pair.config_filename}` "
                f"for {satisfactory.friendly}."
            )
        return NodeBlueprintMutationResult(
            app_name=satisfactory.name,
            app_friendly=satisfactory.friendly,
            node=self._node_name(),
            message=message,
            blueprint=self._blueprint_entry(uploaded, actor_user_id=actor_user_id),
        )

    def delete_file(
        self,
        *,
        app: App,
        blueprint_id: str,
        actor_user_id: int,
    ) -> NodeBlueprintMutationResult:
        satisfactory = self._require_satisfactory(app, action="blueprint deletion")
        try:
            deleted = satisfactory.delete_blueprint_file(
                file_id=blueprint_id,
                actor_user_id=actor_user_id,
                actor_is_sudo=self._require_sudo(actor_user_id),
            )
        except FileNotFoundError as xcp:
            raise self._http_exception(404, str(xcp)) from xcp
        except PermissionError as xcp:
            raise self._http_exception(403, str(xcp)) from xcp
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp
        except Exception as xcp:
            raise self._http_exception(500, f"Blueprint delete failed: {xcp}") from xcp

        delete_message = f"Deleted blueprint `{deleted.label}` from {satisfactory.friendly}."
        try:
            deleted_file_type = blueprint_file_type_from_name(PurePosixPath(blueprint_id).name)
        except ValueError:
            deleted_file_type = AppBlueprintFileType.MODULE
        if deleted_file_type is AppBlueprintFileType.CONFIG:
            delete_message = f"Deleted blueprint config `{PurePosixPath(blueprint_id).name}` from {satisfactory.friendly}."
        elif deleted.config_file is not None:
            delete_message = f"Deleted blueprint `{deleted.label}` and its matching config from {satisfactory.friendly}."

        self._traffic_log.info(
            "Node API deleted Satisfactory blueprint: node=%s app=%s blueprint=%s actor=%s",
            self._node_name(),
            satisfactory.name,
            blueprint_id,
            actor_user_id,
        )
        return NodeBlueprintMutationResult(
            app_name=satisfactory.name,
            app_friendly=satisfactory.friendly,
            node=self._node_name(),
            message=delete_message,
            blueprint=self._blueprint_entry(deleted, actor_user_id=actor_user_id),
        )

    def _resolve_upload_name(self, upload_name: str) -> str:
        if upload_name != upload_name.strip():
            raise self._http_exception(400, "Blueprint filenames must not start or end with spaces.")
        try:
            return validated_upload_filename(upload_name, kind="Blueprint")
        except ValueError as xcp:
            raise self._http_exception(400, str(xcp)) from xcp

    def _blueprint_file_entry(
        self,
        blueprint_file: AppBlueprintFileEntry,
        *,
        actor_user_id: int,
    ) -> NodeBlueprintFileEntry:
        uploaded_by_user_id = blueprint_file.uploaded_by_user_id
        return NodeBlueprintFileEntry(
            id=blueprint_file.id,
            label=blueprint_file.label,
            relative_path=blueprint_file.relative_path,
            size_bytes=blueprint_file.size_bytes,
            size_text=Utilities.humanise_bytes(blueprint_file.size_bytes),
            modified_at=blueprint_file.modified_at.isoformat(sep=" ", timespec="seconds"),
            uploaded_by_display_name=(
                self._display_name_for_user(uploaded_by_user_id) if uploaded_by_user_id is not None else None
            ),
            can_delete=uploaded_by_user_id == actor_user_id or self._can_sudo(actor_user_id),
        )

    def _blueprint_entry(self, blueprint: AppBlueprintEntry, *, actor_user_id: int) -> NodeBlueprintEntry:
        main_file = self._blueprint_file_entry(
            AppBlueprintFileEntry(
                id=blueprint.id,
                label=blueprint.label,
                relative_path=blueprint.relative_path,
                size_bytes=blueprint.size_bytes,
                modified_at=blueprint.modified_at,
                uploaded_by_user_id=blueprint.uploaded_by_user_id,
            ),
            actor_user_id=actor_user_id,
        )
        config_file = (
            self._blueprint_file_entry(blueprint.config_file, actor_user_id=actor_user_id)
            if blueprint.config_file is not None
            else None
        )
        return NodeBlueprintEntry(
            id=main_file.id,
            label=main_file.label,
            session_name=blueprint.session_name,
            relative_path=main_file.relative_path,
            size_bytes=main_file.size_bytes,
            size_text=main_file.size_text,
            modified_at=main_file.modified_at,
            uploaded_by_display_name=main_file.uploaded_by_display_name,
            can_delete=main_file.can_delete and (config_file is None or config_file.can_delete),
            config_file=config_file,
        )

    def _require_satisfactory(self, app: App, *, action: str) -> Satisfactory:
        if not isinstance(app, Satisfactory):
            raise self._http_exception(409, f"{app.friendly} does not support Satisfactory {action}.")
        return app
