"""Node-local, recipe-driven SteamCMD app provisioning."""

from __future__ import annotations

import asyncio
import enum
import logging
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

import config
from _manager import (
    AppInstallInput,
    AppInstanceCreateRequest,
    AppInstanceCreationPlan,
    AppSteamInstallPostProcessor,
    AppSteamInstallRecipe,
)
from _async_utils import run_blocking
from _security import Access_Control, Power_Level
from apps._config import AppVersion, SteamUpdateBranch, SteamUpdateConfig
from apps._steam import cached_steam_update_branches, load_steam_update_branches, merge_steam_update_branches, steam_update_preset_for_scope
from apps._updater import (
    build_steamcmd_command,
    redact_steamcmd_output,
    resolve_steamcmd_command_prefix,
    run_steamcmd_command,
    steamcmd_progress,
)
from .operations import (
    NodeOperationKind,
    NodeOperationProgress,
    NodeOperationRecord,
    NodeOperationResourceConflict,
    NodeOperationService,
    NodeOperationState,
)

log = logging.getLogger(__name__)

_INSTALL_LOG_LINE_MAX_LENGTH = 1_000


class _InstallCancellationRequested(Exception):
    """Raised when an install reaches a cooperative cancellation boundary."""


class NodeAppInstallState(enum.StrEnum):
    QUEUED = "queued"
    INSTALLING = "installing"
    REGISTERING = "registering"
    CANCEL_REQUESTED = "cancel_requested"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def running(self) -> bool:
        return self in {
            NodeAppInstallState.QUEUED,
            NodeAppInstallState.INSTALLING,
            NodeAppInstallState.REGISTERING,
            NodeAppInstallState.CANCEL_REQUESTED,
        }


class NodeAppInstallInputKind(enum.StrEnum):
    TEXT = "text"
    PASSWORD = "password"


@dataclass(frozen=True, slots=True)
class NodeAppInstallBranch:
    branch_id: str
    label: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallBranch":
        return cls(
            branch_id=_required_text(payload, "branch_id", label="install branch id"),
            label=_required_text(payload, "label", label="install branch label"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"branch_id": self.branch_id, "label": self.label}


@dataclass(frozen=True, slots=True)
class NodeAppInstallField:
    key: str
    label: str
    kind: NodeAppInstallInputKind
    required: bool
    help_text: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallField":
        raw_kind = _required_text(payload, "kind", label="install field kind")
        try:
            kind = NodeAppInstallInputKind(raw_kind)
        except ValueError as xcp:
            raise ValueError("Install field kind is invalid.") from xcp
        key = _required_text(payload, "key", label="install field key")
        try:
            AppInstallInput(key)
        except ValueError as xcp:
            raise ValueError("Install field is not supported.") from xcp
        raw_required = payload.get("required")
        if not isinstance(raw_required, bool):
            raise ValueError("Install field required flag is invalid.")
        return cls(
            key=key,
            label=_required_text(payload, "label", label="install field label"),
            help_text=_optional_text(payload.get("help_text"), label="install field help text"),
            kind=kind,
            required=raw_required,
        )

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key": self.key,
            "label": self.label,
            "kind": self.kind.value,
            "required": self.required,
        }
        if self.help_text is not None:
            payload["help_text"] = self.help_text
        return payload


@dataclass(frozen=True, slots=True)
class NodeAppInstallRecipe:
    scope: str
    label: str
    default_port: int | None
    default_branch_id: str
    branches: tuple[NodeAppInstallBranch, ...]
    fields: tuple[NodeAppInstallField, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallRecipe":
        default_port = _optional_port(payload.get("default_port"), label="install default port")
        raw_branches = _mapping_sequence(payload.get("branches"), label="install branches")
        raw_fields = _mapping_sequence(payload.get("fields", ()), label="install fields")
        branches = tuple(NodeAppInstallBranch.from_mapping(branch) for branch in raw_branches)
        if not branches:
            raise ValueError("Install recipe branches must not be empty.")
        default_branch_id = _required_text(payload, "default_branch_id", label="install default branch")
        if default_branch_id.casefold() not in {branch.branch_id.casefold() for branch in branches}:
            raise ValueError("Install recipe default branch is not available.")
        return cls(
            scope=_required_text(payload, "scope", label="install scope"),
            label=_required_text(payload, "label", label="install label"),
            default_port=default_port,
            default_branch_id=default_branch_id,
            branches=branches,
            fields=tuple(NodeAppInstallField.from_mapping(field) for field in raw_fields),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "label": self.label,
            "default_port": self.default_port,
            "default_branch_id": self.default_branch_id,
            "branches": [branch.to_mapping() for branch in self.branches],
            "fields": [field.to_mapping() for field in self.fields],
        }


@dataclass(frozen=True, slots=True)
class NodeAppInstallCatalog:
    node: str
    recipes: tuple[NodeAppInstallRecipe, ...]

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallCatalog":
        return cls(
            node=_required_text(payload, "node", label="install catalog node"),
            recipes=tuple(
                NodeAppInstallRecipe.from_mapping(recipe)
                for recipe in _mapping_sequence(payload.get("recipes"), label="install recipes")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"node": self.node, "recipes": [recipe.to_mapping() for recipe in self.recipes]}


@dataclass(frozen=True, slots=True)
class NodeAppInstallScopeOption:
    """One supported app that can be selected for node installation."""

    scope: str
    label: str

    def __post_init__(self) -> None:
        if not self.scope.strip() or not self.label.strip():
            raise ValueError("App install scope options require a scope and label.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallScopeOption":
        return cls(
            scope=_required_text(payload, "scope", label="install app scope"),
            label=_required_text(payload, "label", label="install app label"),
        )

    def to_mapping(self) -> dict[str, str]:
        return {"scope": self.scope, "label": self.label}


@dataclass(frozen=True, slots=True)
class NodeAppInstallerSettingsState:
    """The persisted node policy and its currently supported app choices."""

    node: str
    settings: config.AppInstallerSettings
    available_apps: tuple[NodeAppInstallScopeOption, ...]

    def __post_init__(self) -> None:
        if not self.node.strip():
            raise ValueError("App installer settings require a node.")
        scope_keys = tuple(option.scope.casefold() for option in self.available_apps)
        if len(set(scope_keys)) != len(scope_keys):
            raise ValueError("App installer settings must not contain duplicate app scopes.")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallerSettingsState":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("App installer settings are invalid.")
        return cls(
            node=_required_text(payload, "node", label="install settings node"),
            settings=config.AppInstallerSettings.model_validate(raw_settings),
            available_apps=tuple(
                NodeAppInstallScopeOption.from_mapping(app)
                for app in _mapping_sequence(payload.get("available_apps"), label="install app choices")
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "settings": self.settings.model_dump(mode="json"),
            "available_apps": [app.to_mapping() for app in self.available_apps],
        }


@dataclass(frozen=True, slots=True)
class NodeAppInstallerSettingsMutationResult:
    node: str
    message: str
    settings: config.AppInstallerSettings

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallerSettingsMutationResult":
        raw_settings = payload.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ValueError("App installer settings mutation settings are invalid.")
        return cls(
            node=_required_text(payload, "node", label="install settings node"),
            message=_required_text(payload, "message", label="install settings message"),
            settings=config.AppInstallerSettings.model_validate(raw_settings),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "node": self.node,
            "message": self.message,
            "settings": self.settings.model_dump(mode="json"),
        }


@dataclass(frozen=True, slots=True)
class NodeAppInstallStatus:
    job_id: str
    node: str
    scope: str
    state: NodeAppInstallState
    summary: str
    app_name: str | None = None
    detail: str | None = None
    progress_percent: float | None = None
    log_lines: tuple[str, ...] = ()
    started_at_unix_ms: int | None = None
    finished_at_unix_ms: int | None = None

    @property
    def running(self) -> bool:
        return self.state.running

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "NodeAppInstallStatus":
        raw_state = _required_text(payload, "state", label="install state")
        try:
            state = NodeAppInstallState(raw_state)
        except ValueError as xcp:
            raise ValueError("Install state is invalid.") from xcp
        progress_percent = _optional_progress(payload.get("progress_percent"))
        raw_log_lines = payload.get("log_lines", ())
        if not isinstance(raw_log_lines, Sequence) or isinstance(raw_log_lines, str | bytes):
            raise ValueError("Install log lines are invalid.")
        log_lines: list[str] = []
        for line in raw_log_lines:
            if not isinstance(line, str):
                raise ValueError("Install log lines are invalid.")
            log_lines.append(line)
        return cls(
            job_id=_required_text(payload, "job_id", label="install job id"),
            node=_required_text(payload, "node", label="install node"),
            scope=_required_text(payload, "scope", label="install scope"),
            state=state,
            summary=_required_text(payload, "summary", label="install summary"),
            app_name=_optional_text(payload.get("app_name"), label="install app name"),
            detail=_optional_text(payload.get("detail"), label="install detail"),
            progress_percent=progress_percent,
            log_lines=tuple(log_lines),
            started_at_unix_ms=_optional_timestamp(payload.get("started_at_unix_ms")),
            finished_at_unix_ms=_optional_timestamp(payload.get("finished_at_unix_ms")),
        )

    @classmethod
    def from_operation(cls, operation: NodeOperationRecord) -> "NodeAppInstallStatus":
        """Project a shared app-install operation into the legacy status view."""

        if operation.kind is not NodeOperationKind.APP_INSTALL:
            raise LookupError("Install job was not found.")
        if operation.state is NodeOperationState.QUEUED:
            state = NodeAppInstallState.QUEUED
        elif operation.state is NodeOperationState.RUNNING:
            state = (
                NodeAppInstallState.REGISTERING
                if operation.phase == NodeAppInstallState.REGISTERING.value
                else NodeAppInstallState.INSTALLING
            )
        elif operation.state is NodeOperationState.CANCEL_REQUESTED:
            state = NodeAppInstallState.CANCEL_REQUESTED
        elif operation.state is NodeOperationState.SUCCEEDED:
            state = NodeAppInstallState.READY
        elif operation.state is NodeOperationState.FAILED:
            state = NodeAppInstallState.FAILED
        elif operation.state is NodeOperationState.CANCELLED:
            state = NodeAppInstallState.CANCELLED
        elif operation.state is NodeOperationState.INTERRUPTED:
            state = NodeAppInstallState.INTERRUPTED
        else:
            raise ValueError(f"Unsupported operation state: {operation.state.value}")
        return cls(
            job_id=operation.operation_id,
            node=operation.node_name,
            scope=operation.subject,
            state=state,
            summary=operation.summary,
            app_name=operation.result_reference,
            detail=operation.detail,
            progress_percent=operation.progress_percent,
            log_lines=operation.log_lines,
            started_at_unix_ms=operation.started_at_unix_ms,
            finished_at_unix_ms=operation.finished_at_unix_ms,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "node": self.node,
            "scope": self.scope,
            "state": self.state.value,
            "summary": self.summary,
            "app_name": self.app_name,
            "detail": self.detail,
            "progress_percent": self.progress_percent,
            "log_lines": list(self.log_lines),
            "started_at_unix_ms": self.started_at_unix_ms,
            "finished_at_unix_ms": self.finished_at_unix_ms,
        }


class NodeAppInstallRequest(BaseModel):
    scope: str
    instance_key: str
    friendly_name: str
    subfolder: str
    port: int | None = None
    steam_branch_id: str
    inputs: dict[AppInstallInput, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("scope", "instance_key", "friendly_name", "subfolder", "steam_branch_id")
    @classmethod
    def _validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("port", mode="before")
    @classmethod
    def _validate_port(cls, raw: object) -> int | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise TypeError("port must be an integer")
        if not 1 <= raw <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return raw

    @field_validator("inputs", mode="before")
    @classmethod
    def _normalise_inputs(cls, raw: object) -> dict[AppInstallInput, str]:
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise TypeError("inputs must be an object")
        inputs: dict[AppInstallInput, str] = {}
        for raw_key, raw_value in cast(Mapping[object, object], raw).items():
            try:
                input_key = AppInstallInput(raw_key)
            except (TypeError, ValueError) as xcp:
                raise ValueError("Install input is not supported.") from xcp
            if not isinstance(raw_value, str):
                raise TypeError(f"{input_key.value} must be text")
            value = raw_value.strip()
            if value:
                inputs[input_key] = value
        return inputs


class NodeAppInstallerManager(Protocol):
    """The narrow manager interface needed by the installer service."""

    def list_steam_install_recipes(self) -> tuple[AppSteamInstallRecipe, ...]: ...

    def prepare_instance_creation(self, request: AppInstanceCreateRequest) -> AppInstanceCreationPlan: ...

    def create_instance(self, request: AppInstanceCreateRequest) -> str: ...

    async def load_instance(self, *, scope: str, instance_key: str) -> object: ...

    def discard_unloaded_instance(self, *, scope: str, instance_key: str) -> None: ...


class NodeAppInstallScopePolicy(Protocol):
    """The node-local policy that determines which recipes may be installed."""

    def allows(self, scope: str) -> bool: ...


class NodeAppInstallerService:
    """Executes app-install operations and safely registers their app instances."""

    def __init__(
        self,
        *,
        node_name: Callable[[], str],
        invalidate_state_caches: Callable[[], None],
        scope_policy: Callable[[], NodeAppInstallScopePolicy] | None = None,
        require_manager: Callable[[], NodeAppInstallerManager] | None = None,
        require_acl: Callable[[], Access_Control] | None = None,
        require_available: Callable[[], None] | None = None,
        operations: NodeOperationService | None = None,
    ) -> None:
        self._node_name = node_name
        self._invalidate_state_caches = invalidate_state_caches
        self._scope_policy = scope_policy or _allow_all_install_scope_policy
        self._require_manager = require_manager
        self._require_acl = require_acl
        self._require_available = require_available
        self._operations = operations or NodeOperationService()

    async def build_catalog(
        self,
        *,
        manager: NodeAppInstallerManager | None = None,
    ) -> NodeAppInstallCatalog:
        self._ensure_available()
        resolved_manager = self._resolve_manager(manager)
        return NodeAppInstallCatalog(
            node=self._node_name(),
            recipes=tuple(
                self._catalog_recipe(recipe)
                for recipe in await self._available_recipes(manager=resolved_manager)
            ),
        )

    async def start_install(
        self,
        *,
        actor_user_id: int,
        request: NodeAppInstallRequest,
        manager: NodeAppInstallerManager | None = None,
        acl: Access_Control | None = None,
    ) -> NodeAppInstallStatus:
        self._ensure_available()
        resolved_manager = self._resolve_manager(manager)
        resolved_acl = self._resolve_acl(acl)
        await resolved_acl.perm_check(actor_user_id, Power_Level.sudo)
        recipe = await self._recipe_for_scope(
            manager=resolved_manager, scope=request.scope
        )
        branch = self._recipe_branch(recipe=recipe, branch_id=request.steam_branch_id)
        normalised_request = request.model_copy(
            update={"scope": recipe.scope, "steam_branch_id": branch.branch_id}
        )
        self._validate_recipe_inputs(recipe=recipe, inputs=normalised_request.inputs)
        secret_values = tuple(
            value for input_key, value in normalised_request.inputs.items() if input_key.is_secret
        )
        create_request = self._create_request(normalised_request)
        plan = resolved_manager.prepare_instance_creation(create_request)
        if plan.directory.exists():
            raise ValueError("Install folder already exists.")

        directory_resource_key, instance_resource_key = self._install_resource_keys(plan)
        try:
            operation = self._operations.create(
                kind=NodeOperationKind.APP_INSTALL,
                node_name=self._node_name(),
                subject=plan.scope,
                requested_by_user_id=actor_user_id,
                progress=NodeOperationProgress(summary="Queued."),
                resource_keys=(directory_resource_key, instance_resource_key),
            )
        except NodeOperationResourceConflict as xcp:
            if xcp.resource_key == directory_resource_key:
                raise RuntimeError("An install is already using that folder.") from xcp
            if xcp.resource_key == instance_resource_key:
                raise RuntimeError("An install is already using that instance key.") from xcp
            raise RuntimeError("An install resource is already in use.") from xcp

        job_id = operation.operation_id
        staging_directory = plan.directory.with_name(f".{plan.directory.name}.install-{job_id}")
        task: asyncio.Task[None] | None = None
        try:
            task = asyncio.create_task(
                self._run_install(
                    operation_id=job_id,
                    staging_directory=staging_directory,
                    manager=resolved_manager,
                    plan=plan,
                    create_request=create_request,
                    steam_update=recipe.steam_update.with_selected_branch(branch.branch_id),
                    post_steam_install=recipe.post_steam_install,
                    secret_values=secret_values,
                ),
                name=f"app-install-{job_id}",
            )
            self._operations.track_task(
                operation_id=job_id,
                task=cast(asyncio.Task[object], task),
            )
        except Exception as xcp:
            if task is not None:
                task.cancel()
            self._operations.finish(
                operation_id=job_id,
                state=NodeOperationState.FAILED,
                summary="Install could not be started.",
                detail=type(xcp).__name__,
            )
            raise
        return self._status_from_operation(operation)

    def install_status(self, *, job_id: str) -> NodeAppInstallStatus:
        self._ensure_available()
        try:
            operation = self._operations.get(job_id)
        except LookupError as xcp:
            raise LookupError("Install job was not found.") from xcp
        return self._status_from_operation(operation)

    async def cancel_install(
        self,
        *,
        job_id: str,
        actor_user_id: int,
        acl: Access_Control | None = None,
    ) -> NodeAppInstallStatus:
        """Cancel an active install using its cancellation-safe executor path."""
        self._ensure_available()
        await self._resolve_acl(acl).perm_check(actor_user_id, Power_Level.sudo)
        try:
            operation = self._operations.get(job_id)
        except LookupError as xcp:
            raise LookupError("Install job was not found.") from xcp
        self._require_install_operation(operation)
        return self._status_from_operation(
            self._operations.hard_cancel(operation_id=operation.operation_id)
        )

    def _ensure_available(self) -> None:
        if self._require_available is not None:
            self._require_available()

    def _resolve_manager(
        self, manager: NodeAppInstallerManager | None
    ) -> NodeAppInstallerManager:
        if manager is not None:
            return manager
        if self._require_manager is None:
            raise RuntimeError("App installer manager is not configured.")
        return self._require_manager()

    def _resolve_acl(self, acl: Access_Control | None) -> Access_Control:
        if acl is not None:
            return acl
        if self._require_acl is None:
            raise RuntimeError("App installer access control is not configured.")
        return self._require_acl()

    def cancel_pending(self) -> None:
        self._operations.hard_cancel_pending(kind=NodeOperationKind.APP_INSTALL)

    async def _run_install(
        self,
        *,
        operation_id: str,
        staging_directory: Path,
        manager: NodeAppInstallerManager,
        plan: AppInstanceCreationPlan,
        create_request: AppInstanceCreateRequest,
        steam_update: SteamUpdateConfig,
        post_steam_install: AppSteamInstallPostProcessor | None,
        secret_values: tuple[str, ...],
    ) -> None:
        app_name: str | None = None
        instance_created = False
        promoted = False
        try:
            operation = self._operations.begin(
                operation_id=operation_id,
                progress=NodeOperationProgress(
                    summary="Installing.",
                    phase=NodeAppInstallState.INSTALLING.value,
                    progress_percent=0.0,
                ),
            )
            self._raise_if_install_cancellation_requested(operation_id=operation_id)
            if operation.state is not NodeOperationState.RUNNING:
                return
            staging_directory.parent.mkdir(parents=True, exist_ok=True)
            staging_directory.mkdir()
            branch = steam_update.selected_branch_config
            command = build_steamcmd_command(
                steam_update=steam_update,
                install_dir=staging_directory,
                branch=branch,
                validate=False,
            )
            log.info(
                "Starting app install: node=%s job=%s scope=%s branch=%s",
                self._node_name(),
                operation_id,
                plan.scope,
                branch.branch_id,
            )
            completed = await run_steamcmd_command(
                command=command,
                cwd=staging_directory,
                on_output=lambda source, line: self._record_output(
                    operation_id=operation_id,
                    source=source,
                    line=line,
                    steam_update=steam_update,
                    secret_values=secret_values,
                ),
            )
            self._raise_if_install_cancellation_requested(operation_id=operation_id)
            if not completed:
                raise RuntimeError("SteamCMD did not confirm the install.")
            if not any(staging_directory.iterdir()):
                raise RuntimeError("SteamCMD completed without installing files.")
            if post_steam_install is not None:
                self._operations.update_active(
                    operation_id=operation_id,
                    progress=NodeOperationProgress(
                        summary="Preparing app.",
                        phase=NodeAppInstallState.INSTALLING.value,
                        progress_percent=99.0,
                    ),
                )
                await post_steam_install(staging_directory, create_request)

            self._raise_if_install_cancellation_requested(operation_id=operation_id)
            self._operations.update_active(
                operation_id=operation_id,
                progress=NodeOperationProgress(
                    summary="Registering app.",
                    phase=NodeAppInstallState.REGISTERING.value,
                    progress_percent=100.0,
                ),
            )
            self._raise_if_install_cancellation_requested(operation_id=operation_id)
            # This registration-and-rename sequence has no await point, so a
            # hard cancellation can only be observed before or after promotion.
            if plan.directory.exists():
                raise RuntimeError("Install folder was created while the job was running.")
            app_name = manager.create_instance(create_request)
            instance_created = True
            staging_directory.replace(plan.directory)
            promoted = True
            if self._operations.cancellation_requested(operation_id=operation_id):
                self._finish_promoted_install(
                    operation_id=operation_id,
                    app_name=app_name,
                    detail="The installer stopped before the app could be loaded.",
                )
                return
            try:
                await manager.load_instance(scope=plan.scope, instance_key=plan.instance_key)
            except Exception as xcp:
                detail = self._redact_install_detail(
                    detail=str(xcp),
                    steam_update=steam_update,
                    secret_values=secret_values,
                )
                log.warning("Installed app could not be loaded immediately: app=%s error=%s", app_name, detail)
                self._finish_promoted_install(
                    operation_id=operation_id,
                    app_name=app_name,
                    detail=detail,
                )
            else:
                self._finish_promoted_install(
                    operation_id=operation_id,
                    app_name=app_name,
                    detail=None,
                )
        except _InstallCancellationRequested:
            self._finish_cancelled_install(
                operation_id=operation_id,
                app_name=app_name,
                instance_created=instance_created,
                manager=manager,
                plan=plan,
                promoted=promoted,
            )
        except asyncio.CancelledError:
            self._finish_cancelled_install(
                operation_id=operation_id,
                app_name=app_name,
                instance_created=instance_created,
                manager=manager,
                plan=plan,
                promoted=promoted,
            )
            raise
        except Exception as xcp:
            if instance_created and not promoted:
                self._discard_incomplete_instance(manager=manager, plan=plan)
            detail = self._redact_install_detail(
                detail=str(xcp),
                steam_update=steam_update,
                secret_values=secret_values,
            )
            log.warning("App install failed: node=%s job=%s error=%s", self._node_name(), operation_id, detail)
            self._operations.finish(
                operation_id=operation_id,
                state=NodeOperationState.FAILED,
                summary="Install failed.",
                detail=detail,
            )
        finally:
            if promoted:
                self._invalidate_install_state_caches()
            try:
                if staging_directory.exists():
                    await run_blocking(shutil.rmtree, staging_directory, ignore_errors=True)
            except Exception:
                log.exception(
                    "Failed to remove app install staging directory: job=%s path=%s",
                    operation_id,
                    staging_directory,
                )

    @staticmethod
    def _catalog_recipe(recipe: AppSteamInstallRecipe) -> NodeAppInstallRecipe:
        return NodeAppInstallRecipe(
            scope=recipe.scope,
            label=recipe.label,
            default_port=recipe.default_port,
            default_branch_id=recipe.steam_update.selected_branch,
            branches=tuple(
                NodeAppInstallBranch(branch_id=branch.branch_id, label=branch.display_label)
                for branch in recipe.steam_update.branches
            ),
            fields=tuple(
                NodeAppInstallField(
                    key=install_input.value,
                    label=install_input.label,
                    help_text=install_input.help_text,
                    kind=(
                        NodeAppInstallInputKind.PASSWORD
                        if install_input.is_secret
                        else NodeAppInstallInputKind.TEXT
                    ),
                    required=True,
                )
                for install_input in recipe.inputs
            ),
        )

    async def _available_recipes(self, *, manager: NodeAppInstallerManager) -> tuple[AppSteamInstallRecipe, ...]:
        recipes = manager.list_steam_install_recipes()
        policy = self._scope_policy()
        allowed_recipes = tuple(recipe for recipe in recipes if policy.allows(recipe.scope))
        return tuple(await asyncio.gather(*(self._with_discovered_branches(recipe) for recipe in allowed_recipes)))

    async def _recipe_for_scope(self, *, manager: NodeAppInstallerManager, scope: str) -> AppSteamInstallRecipe:
        scope_key = scope.strip().casefold()
        for recipe in await self._available_recipes(manager=manager):
            if recipe.scope.casefold() == scope_key:
                return recipe
        raise ValueError("That app is not available for installation.")

    @staticmethod
    async def _with_discovered_branches(recipe: AppSteamInstallRecipe) -> AppSteamInstallRecipe:
        if steam_update_preset_for_scope(recipe.scope) is None:
            return recipe
        working_directory = config.DIR_LOG / "steamcmd-app-info"
        try:
            discovered_branches = await load_steam_update_branches(
                recipe.steam_update,
                command_prefix=resolve_steamcmd_command_prefix(recipe.steam_update),
                working_directory=working_directory,
            )
        except Exception as xcp:
            discovered_branches = cached_steam_update_branches(recipe.steam_update.app_id, allow_stale=True) or ()
            log.warning(
                "Unable to discover Steam install branches: scope=%s app_id=%s error=%s",
                recipe.scope,
                recipe.steam_update.app_id,
                xcp,
            )
        branches = merge_steam_update_branches(discovered_branches, recipe.steam_update.branches)
        return replace(recipe, steam_update=recipe.steam_update.model_copy(update={"branches": branches}))

    @staticmethod
    def _recipe_branch(*, recipe: AppSteamInstallRecipe, branch_id: str) -> SteamUpdateBranch:
        try:
            return recipe.steam_update.branch(branch_id)
        except ValueError as xcp:
            raise ValueError("That release channel is not available.") from xcp

    @staticmethod
    def _create_request(request: NodeAppInstallRequest) -> AppInstanceCreateRequest:
        return AppInstanceCreateRequest(
            scope=request.scope,
            instance_key=request.instance_key,
            friendly_name=request.friendly_name,
            subfolder=request.subfolder,
            port=request.port,
            admin_password=request.inputs.get(AppInstallInput.ADMIN_PASSWORD),
            steam_branch=request.steam_branch_id,
            initial_version=AppVersion(main="0.0"),
        )

    @staticmethod
    def _validate_recipe_inputs(
        *,
        recipe: AppSteamInstallRecipe,
        inputs: Mapping[AppInstallInput, str],
    ) -> None:
        supported_inputs = frozenset(recipe.inputs)
        unsupported_inputs = tuple(input_key for input_key in inputs if input_key not in supported_inputs)
        if unsupported_inputs:
            raise ValueError("An install detail is not supported by that app.")
        for input_key in recipe.inputs:
            if input_key not in inputs:
                raise ValueError(f"{input_key.label} is required.")

    def _record_output(
        self,
        *,
        operation_id: str,
        source: str,
        line: str,
        steam_update: SteamUpdateConfig,
        secret_values: tuple[str, ...],
    ) -> None:
        redacted_line = redact_steamcmd_output(
            text=line,
            steam_update=steam_update,
            additional_secrets=secret_values,
        ).strip()
        if not redacted_line:
            return
        log_line = f"{source}: {redacted_line[:_INSTALL_LOG_LINE_MAX_LENGTH]}"
        progress = steamcmd_progress(redacted_line)
        try:
            operation = self._operations.get(operation_id)
        except LookupError:
            return
        if not operation.active:
            return
        if operation.state is NodeOperationState.CANCEL_REQUESTED:
            self._operations.append_log(operation_id=operation_id, log_line=log_line)
            return
        summary = operation.summary
        progress_percent = operation.progress_percent
        if progress is not None:
            phase, progress_percent = progress
            summary = phase[:1].upper() + phase[1:] if phase else redacted_line
        self._operations.update_active(
            operation_id=operation_id,
            progress=NodeOperationProgress(
                summary=summary,
                phase=operation.phase,
                detail=redacted_line,
                progress_percent=progress_percent,
            ),
            log_line=log_line,
        )

    @staticmethod
    def _redact_install_detail(
        *,
        detail: str,
        steam_update: SteamUpdateConfig,
        secret_values: tuple[str, ...],
    ) -> str:
        return redact_steamcmd_output(
            text=detail,
            steam_update=steam_update,
            additional_secrets=secret_values,
        )

    @staticmethod
    def _install_resource_keys(plan: AppInstanceCreationPlan) -> tuple[str, str]:
        directory = plan.directory.resolve(strict=False)
        directory_resource_key = f"app-install-directory:{directory}"
        instance_resource_key = (
            f"app-install-instance:{plan.scope.casefold()}:{plan.instance_key.casefold()}"
        )
        return directory_resource_key, instance_resource_key

    @staticmethod
    def _require_install_operation(operation: NodeOperationRecord) -> None:
        if operation.kind is not NodeOperationKind.APP_INSTALL:
            raise LookupError("Install job was not found.")

    @classmethod
    def _status_from_operation(cls, operation: NodeOperationRecord) -> NodeAppInstallStatus:
        return NodeAppInstallStatus.from_operation(operation)

    def _finish_promoted_install(
        self,
        *,
        operation_id: str,
        app_name: str | None,
        detail: str | None,
    ) -> None:
        self._operations.finish(
            operation_id=operation_id,
            state=NodeOperationState.SUCCEEDED,
            summary=(
                "Installed."
                if detail is None
                else "Installed. Restart to load it."
            ),
            detail=detail,
            result_reference=app_name,
            progress_percent=100.0,
        )

    def _finish_cancelled_install(
        self,
        *,
        operation_id: str,
        app_name: str | None,
        instance_created: bool,
        manager: NodeAppInstallerManager,
        plan: AppInstanceCreationPlan,
        promoted: bool,
    ) -> None:
        if promoted:
            self._finish_promoted_install(
                operation_id=operation_id,
                app_name=app_name,
                detail="The installer stopped before the app could be loaded.",
            )
            return
        if instance_created and not self._discard_incomplete_instance(
            manager=manager, plan=plan
        ):
            self._operations.finish(
                operation_id=operation_id,
                state=NodeOperationState.FAILED,
                summary="Install cleanup failed.",
                detail="The incomplete app instance could not be discarded.",
            )
            return
        self._operations.finish(
            operation_id=operation_id,
            state=NodeOperationState.CANCELLED,
            summary="Install stopped.",
            detail=None,
        )

    def _raise_if_install_cancellation_requested(self, *, operation_id: str) -> None:
        if self._operations.cancellation_requested(operation_id=operation_id):
            raise _InstallCancellationRequested

    @staticmethod
    def _discard_incomplete_instance(
        *,
        manager: NodeAppInstallerManager,
        plan: AppInstanceCreationPlan,
    ) -> bool:
        try:
            manager.discard_unloaded_instance(
                scope=plan.scope,
                instance_key=plan.instance_key,
            )
        except Exception:
            log.exception(
                "Failed to discard incomplete app instance: scope=%s key=%s",
                plan.scope,
                plan.instance_key,
            )
            return False
        return True

    def _invalidate_install_state_caches(self) -> None:
        try:
            self._invalidate_state_caches()
        except Exception:
            log.exception("Failed to invalidate state caches after app install: node=%s", self._node_name())


def _required_text(payload: Mapping[str, object], key: str, *, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label.title()} is invalid.")
    return value.strip()


def _optional_text(raw: object, *, label: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{label.title()} is invalid.")
    text = raw.strip()
    return text or None


def _optional_port(raw: object, *, label: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 65535:
        raise ValueError(f"{label.title()} is invalid.")
    return raw


def _optional_progress(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError("Install progress is invalid.")
    value = float(raw)
    if not 0.0 <= value <= 100.0:
        raise ValueError("Install progress is invalid.")
    return value


def _optional_timestamp(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("Install timestamp is invalid.")
    return raw


def _mapping_sequence(raw: object, *, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ValueError(f"{label.title()} are invalid.")
    items: list[Mapping[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label.title()} are invalid.")
        mapping = cast(Mapping[object, object], item)
        if any(not isinstance(key, str) for key in mapping):
            raise ValueError(f"{label.title()} are invalid.")
        items.append(cast(Mapping[str, object], mapping))
    return tuple(items)


class _AllowAllInstallScopePolicy:
    def allows(self, scope: str) -> bool:
        del scope
        return True


_ALLOW_ALL_INSTALL_SCOPE_POLICY = _AllowAllInstallScopePolicy()


def _allow_all_install_scope_policy() -> NodeAppInstallScopePolicy:
    return _ALLOW_ALL_INSTALL_SCOPE_POLICY


__all__: tuple[str, ...] = (
    "NodeAppInstallBranch",
    "NodeAppInstallCatalog",
    "NodeAppInstallField",
    "NodeAppInstallInputKind",
    "NodeAppInstallRecipe",
    "NodeAppInstallRequest",
    "NodeAppInstallScopeOption",
    "NodeAppInstallerService",
    "NodeAppInstallerSettingsMutationResult",
    "NodeAppInstallerSettingsState",
    "NodeAppInstallState",
    "NodeAppInstallStatus",
)
