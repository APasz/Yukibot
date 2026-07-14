from __future__ import annotations

import asyncio
import enum
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import _errors
import config
from _async_utils import run_blocking
from apps._config import App_Config, AppVersion, SteamUpdateBranch, SteamUpdateConfig

log = logging.getLogger(__name__)


class AppUpdateProviderKind(enum.StrEnum):
    STEAMCMD = "steamcmd"
    FACTORIO = "factorio"


class AppUpdateOperationKind(enum.StrEnum):
    UPDATE = "update"
    VERIFY = "verify"


class AppUpdateState(enum.StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AppUpdateBranchState:
    branch_id: str
    label: str
    selected: bool

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "AppUpdateBranchState":
        branch_id = payload.get("branch_id")
        label = payload.get("label")
        selected = payload.get("selected")
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ValueError("App update branch id is invalid.")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("App update branch label is invalid.")
        if not isinstance(selected, bool):
            raise ValueError("App update branch selected state is invalid.")
        return cls(branch_id=branch_id.strip(), label=label.strip(), selected=selected)

    def to_mapping(self) -> dict[str, object]:
        return {
            "branch_id": self.branch_id,
            "label": self.label,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class AppUpdateStatus:
    state: AppUpdateState
    summary: str
    operation_kind: AppUpdateOperationKind | None = None
    progress_percent: float | None = None
    detail: str | None = None
    log_lines: tuple[str, ...] = ()
    started_at_unix_ms: int | None = None
    finished_at_unix_ms: int | None = None

    @property
    def running(self) -> bool:
        return self.state is AppUpdateState.RUNNING

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "AppUpdateStatus":
        raw_state = payload.get("state")
        summary = payload.get("summary")
        raw_operation_kind = payload.get("operation_kind")
        progress_percent = payload.get("progress_percent")
        detail = payload.get("detail")
        raw_log_lines = payload.get("log_lines", [])
        started_at_unix_ms = payload.get("started_at_unix_ms")
        finished_at_unix_ms = payload.get("finished_at_unix_ms")
        if not isinstance(raw_state, str):
            raise ValueError("App update status state is invalid.")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("App update status summary is invalid.")
        if raw_operation_kind is not None and not isinstance(raw_operation_kind, str):
            raise ValueError("App update status operation kind is invalid.")
        if progress_percent is not None and not isinstance(progress_percent, (int, float)):
            raise ValueError("App update status progress percent is invalid.")
        if detail is not None and not isinstance(detail, str):
            raise ValueError("App update status detail is invalid.")
        if not isinstance(raw_log_lines, list):
            raise ValueError("App update status log lines are invalid.")
        if started_at_unix_ms is not None and (
            isinstance(started_at_unix_ms, bool) or not isinstance(started_at_unix_ms, int)
        ):
            raise ValueError("App update status started timestamp is invalid.")
        if finished_at_unix_ms is not None and (
            isinstance(finished_at_unix_ms, bool) or not isinstance(finished_at_unix_ms, int)
        ):
            raise ValueError("App update status finished timestamp is invalid.")
        try:
            state = AppUpdateState(raw_state)
        except ValueError as xcp:
            raise ValueError("App update status state is invalid.") from xcp
        operation_kind: AppUpdateOperationKind | None = None
        if raw_operation_kind is not None:
            try:
                operation_kind = AppUpdateOperationKind(raw_operation_kind)
            except ValueError as xcp:
                raise ValueError("App update status operation kind is invalid.") from xcp
        log_lines: list[str] = []
        for raw_line in raw_log_lines:
            if not isinstance(raw_line, str):
                raise ValueError("App update status log lines are invalid.")
            log_lines.append(raw_line)
        progress_value = float(progress_percent) if progress_percent is not None else None
        if progress_value is not None and not 0.0 <= progress_value <= 100.0:
            raise ValueError("App update status progress percent is invalid.")
        return cls(
            state=state,
            summary=summary.strip(),
            operation_kind=operation_kind,
            progress_percent=progress_value,
            detail=detail.strip() if isinstance(detail, str) and detail.strip() else None,
            log_lines=tuple(log_lines),
            started_at_unix_ms=started_at_unix_ms,
            finished_at_unix_ms=finished_at_unix_ms,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "summary": self.summary,
            "operation_kind": self.operation_kind.value if self.operation_kind is not None else None,
            "progress_percent": self.progress_percent,
            "detail": self.detail,
            "log_lines": list(self.log_lines),
            "started_at_unix_ms": self.started_at_unix_ms,
            "finished_at_unix_ms": self.finished_at_unix_ms,
        }


@dataclass(frozen=True, slots=True)
class SteamAppManifestState:
    app_id: int
    build_id: int | None = None
    branch_id: str | None = None

    @property
    def build_label(self) -> str | None:
        if self.build_id is None:
            return None
        return str(self.build_id)


@dataclass(frozen=True, slots=True)
class AppUpdateInfo:
    provider_kind: AppUpdateProviderKind
    provider_label: str
    selected_branch_id: str
    selected_branch_label: str
    branches: tuple[AppUpdateBranchState, ...]
    supports_verify: bool = False
    app_id: int | None = None
    installed_build_id: int | None = None
    installed_branch_id: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "AppUpdateInfo":
        raw_provider_kind = payload.get("provider_kind")
        provider_label = payload.get("provider_label")
        selected_branch_id = payload.get("selected_branch_id")
        selected_branch_label = payload.get("selected_branch_label")
        supports_verify = payload.get("supports_verify", False)
        app_id = payload.get("app_id")
        installed_build_id = payload.get("installed_build_id")
        installed_branch_id = payload.get("installed_branch_id")
        raw_branches = payload.get("branches")
        if not isinstance(raw_provider_kind, str):
            raise ValueError("App update provider kind is invalid.")
        if not isinstance(provider_label, str) or not provider_label.strip():
            raise ValueError("App update provider label is invalid.")
        if not isinstance(selected_branch_id, str) or not selected_branch_id.strip():
            raise ValueError("App update selected branch id is invalid.")
        if not isinstance(selected_branch_label, str) or not selected_branch_label.strip():
            raise ValueError("App update selected branch label is invalid.")
        if not isinstance(supports_verify, bool):
            raise ValueError("App update supports_verify is invalid.")
        if app_id is not None and not isinstance(app_id, int):
            raise ValueError("App update app id is invalid.")
        if installed_build_id is not None and (
            isinstance(installed_build_id, bool) or not isinstance(installed_build_id, int)
        ):
            raise ValueError("App update installed build id is invalid.")
        if installed_branch_id is not None and (
            not isinstance(installed_branch_id, str) or not installed_branch_id.strip()
        ):
            raise ValueError("App update installed branch id is invalid.")
        if not isinstance(raw_branches, list):
            raise ValueError("App update branches are invalid.")
        try:
            provider_kind = AppUpdateProviderKind(raw_provider_kind)
        except ValueError as xcp:
            raise ValueError("App update provider kind is invalid.") from xcp
        branches = tuple(
            AppUpdateBranchState.from_mapping(_mapping(item, label="app update branch")) for item in raw_branches
        )
        return cls(
            provider_kind=provider_kind,
            provider_label=provider_label.strip(),
            selected_branch_id=selected_branch_id.strip(),
            selected_branch_label=selected_branch_label.strip(),
            branches=branches,
            supports_verify=supports_verify,
            app_id=app_id,
            installed_build_id=installed_build_id,
            installed_branch_id=installed_branch_id.strip() if isinstance(installed_branch_id, str) else None,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider_kind": self.provider_kind.value,
            "provider_label": self.provider_label,
            "selected_branch_id": self.selected_branch_id,
            "selected_branch_label": self.selected_branch_label,
            "branches": [branch.to_mapping() for branch in self.branches],
            "supports_verify": self.supports_verify,
            "app_id": self.app_id,
            "installed_build_id": self.installed_build_id,
            "installed_branch_id": self.installed_branch_id,
        }


@dataclass(frozen=True, slots=True)
class AppUpdateOperationResult:
    kind: AppUpdateOperationKind
    message: str
    version_text: str | None = None
    selected_branch_id: str | None = None
    selected_branch_label: str | None = None


class UpdateManagerApp(Protocol):
    friendly: str
    scope: str
    directory: Path
    dir_log: Path

    @property
    def cfg(self) -> App_Config: ...

    @property
    def mods(self) -> object | None: ...

    def apply_version(self, version: AppVersion | str | None, *, persist: bool) -> bool: ...

    def check_running(self) -> bool: ...

    def detect_installed_version(self) -> AppVersion | None: ...

    def persist_instance_config_overrides(self) -> None: ...


class Update_Manager:
    version: tuple[int, ...] | None = None

    def __init__(self, app: UpdateManagerApp, *, base: bool = False, mods: bool = False) -> None:
        self.app = app
        self.can_base = base
        self.can_mods = mods if app.mods else False

    @staticmethod
    def stringise(version: tuple[int, ...]) -> str:
        return ".".join(map(str, version))

    @staticmethod
    def extract_version(line: str, regex: re.Pattern[str]) -> tuple[int, ...] | None:
        match = regex.search(line)
        ver = match.group(1) if match else None
        return tuple(map(int, ver.split("."))) if ver else None

    def info(self) -> AppUpdateInfo | None:
        return None

    def select_branch(self, branch_id: str) -> AppUpdateInfo:
        del branch_id
        raise _errors.UnsupportedUpdate("Branch selection is not supported")

    def status(self) -> AppUpdateStatus | None:
        return None

    async def start_selected_update(self) -> AppUpdateOperationResult:
        return await self.update_selected()

    async def start_selected_verify(self) -> AppUpdateOperationResult:
        return await self.verify_selected()

    async def update_selected(self) -> AppUpdateOperationResult:
        previous_version = self.stringise(self.version) if self.version is not None else None
        version_text = await self.base()
        if version_text is None:
            return AppUpdateOperationResult(
                kind=AppUpdateOperationKind.UPDATE,
                message=f"No new update found for {self.app.friendly}.",
            )
        if previous_version is None:
            message = f"Updated {self.app.friendly} to {version_text}."
        else:
            message = f"Updated {self.app.friendly} from {previous_version} to {version_text}."
        return AppUpdateOperationResult(
            kind=AppUpdateOperationKind.UPDATE,
            message=message,
            version_text=version_text,
        )

    async def verify_selected(self) -> AppUpdateOperationResult:
        raise _errors.UnsupportedUpdate("Verification is not supported")

    async def base(self) -> str | None:
        if not self.can_base:
            raise _errors.UnsupportedUpdate("Base updating not supported")

    async def mods(self) -> list[str] | None:
        if not self.can_mods:
            raise _errors.UnsupportedUpdate("Mod updating not supported")


class SteamCmd_Update_Manager(Update_Manager):
    def __init__(self, app: UpdateManagerApp) -> None:
        super().__init__(app, base=True, mods=False)
        steam_update = self._steam_update_config()
        self._steamcmd_command_prefix: tuple[str, ...] = self._resolve_steamcmd_command_prefix(steam_update)
        self._app_id: int = steam_update.app_id
        self._state_lock = threading.Lock()
        self._status: AppUpdateStatus = AppUpdateStatus(
            state=AppUpdateState.IDLE,
            summary="Ready",
        )
        self._log_tail: deque[str] = deque(maxlen=80)
        self._last_logged_manifest_signature: tuple[int, str | None, int | None] | None = None
        self._operation_running: bool = False
        self._active_task: asyncio.Task[AppUpdateOperationResult] | None = None

    def info(self) -> AppUpdateInfo:
        steam_update = self._steam_update_config()
        selected_branch = steam_update.selected_branch_config
        installed_manifest = self._safe_read_installed_manifest()
        return AppUpdateInfo(
            provider_kind=AppUpdateProviderKind.STEAMCMD,
            provider_label="SteamCMD",
            app_id=steam_update.app_id,
            selected_branch_id=selected_branch.branch_id,
            selected_branch_label=selected_branch.display_label,
            branches=tuple(
                AppUpdateBranchState(
                    branch_id=branch.branch_id,
                    label=branch.display_label,
                    selected=branch.branch_id.casefold() == steam_update.selected_branch.casefold(),
                )
                for branch in steam_update.branches
            ),
            supports_verify=True,
            installed_build_id=installed_manifest.build_id if installed_manifest is not None else None,
            installed_branch_id=installed_manifest.branch_id if installed_manifest is not None else None,
        )

    def select_branch(self, branch_id: str) -> AppUpdateInfo:
        if self.status().running:
            raise RuntimeError(f"Cannot change the Steam branch while {self.app.friendly} is updating.")
        steam_update = self._steam_update_config()
        branch = steam_update.branch(branch_id)
        if branch.branch_id.casefold() == steam_update.selected_branch.casefold():
            return self.info()
        next_update = steam_update.model_copy(update={"selected_branch": branch.branch_id})
        cfg = self._app_config()
        cfg.steam_update = next_update
        if hasattr(self.app, "persist_instance_config_overrides"):
            self.app.persist_instance_config_overrides()
        log.info("Selected Steam update branch for %s: %s", self.app.friendly, branch.branch_id)
        return self.info()

    def status(self) -> AppUpdateStatus:
        with self._state_lock:
            return self._status

    async def start_selected_update(self) -> AppUpdateOperationResult:
        return self._start_selected_operation(AppUpdateOperationKind.UPDATE)

    async def start_selected_verify(self) -> AppUpdateOperationResult:
        return self._start_selected_operation(AppUpdateOperationKind.VERIFY)

    async def update_selected(self) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(AppUpdateOperationKind.UPDATE)
        return await self._run_started_operation(kind=AppUpdateOperationKind.UPDATE, branch=branch)

    async def verify_selected(self) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(AppUpdateOperationKind.VERIFY)
        return await self._run_started_operation(kind=AppUpdateOperationKind.VERIFY, branch=branch)

    async def base(self) -> str | None:
        result = await self.update_selected()
        return result.version_text

    def _start_selected_operation(self, kind: AppUpdateOperationKind) -> AppUpdateOperationResult:
        branch = self._begin_selected_operation(kind)
        log.info(
            "Starting Steam update task: app=%s kind=%s branch=%s",
            self.app.friendly,
            kind.value,
            branch.branch_id,
        )
        task: asyncio.Task[AppUpdateOperationResult] = asyncio.create_task(
            self._run_started_operation(kind=kind, branch=branch)
        )
        with self._state_lock:
            self._active_task = task
        task.add_done_callback(self._log_background_task_outcome)
        return AppUpdateOperationResult(
            kind=kind,
            message=f"Started {kind.value} for {self.app.friendly} on Steam branch {branch.display_label}.",
            selected_branch_id=branch.branch_id,
            selected_branch_label=branch.display_label,
        )

    def _begin_selected_operation(self, kind: AppUpdateOperationKind) -> SteamUpdateBranch:
        if self.app.check_running():
            raise RuntimeError(f"{self.app.friendly} must be stopped before {kind.value}.")
        steam_update = self._steam_update_config()
        branch = steam_update.selected_branch_config
        started_at_unix_ms = _unix_ms_now()
        with self._state_lock:
            if self._operation_running:
                raise RuntimeError(f"{self.app.friendly} already has a Steam update in progress.")
            self._operation_running = True
            self._active_task = None
            self._log_tail.clear()
            self._status = AppUpdateStatus(
                state=AppUpdateState.RUNNING,
                summary=f"Starting {kind.value}...",
                operation_kind=kind,
                progress_percent=0.0,
                started_at_unix_ms=started_at_unix_ms,
            )
        self._reset_command_log(kind=kind, branch=branch)
        return branch

    async def _run_started_operation(
        self,
        *,
        kind: AppUpdateOperationKind,
        branch: SteamUpdateBranch,
    ) -> AppUpdateOperationResult:
        validate = kind is AppUpdateOperationKind.VERIFY
        log.info(
            "Steam update task running: app=%s kind=%s branch=%s validate=%s",
            self.app.friendly,
            kind.value,
            branch.branch_id,
            validate,
        )
        try:
            completed = await self._run_steamcmd(branch=branch, validate=validate)
            if not completed:
                raise RuntimeError(f"SteamCMD did not complete {kind.value} for {self.app.friendly}.")
            manifest_state = await run_blocking(self._safe_read_installed_manifest)
            detected_version = await run_blocking(self._detect_installed_version)
            resolved_version = self._version_with_manifest_data(detected_version, manifest_state)
            version_text: str | None = None
            if resolved_version is not None:
                self.app.apply_version(resolved_version, persist=True)
                version_text = resolved_version.display_value
            steam_build_text = manifest_state.build_label if manifest_state is not None else None
            if kind is AppUpdateOperationKind.VERIFY:
                message = f"Verified {self.app.friendly} on Steam branch {branch.display_label}."
                if steam_build_text is not None:
                    message = f"{message} Steam build {steam_build_text}."
            elif version_text is not None:
                message = f"Updated {self.app.friendly} on Steam branch {branch.display_label} to {version_text}."
            elif steam_build_text is not None:
                message = f"Updated {self.app.friendly} on Steam branch {branch.display_label} to Steam build {steam_build_text}."
            else:
                message = f"Updated {self.app.friendly} on Steam branch {branch.display_label}."
            self._finish_operation(
                state=AppUpdateState.SUCCEEDED,
                summary=message,
                detail=None,
                progress_percent=100.0,
            )
            log.info(
                "Steam update task completed: app=%s kind=%s branch=%s version=%s steam_build=%s",
                self.app.friendly,
                kind.value,
                branch.branch_id,
                version_text,
                steam_build_text,
            )
            return AppUpdateOperationResult(
                kind=kind,
                message=message,
                version_text=version_text,
                selected_branch_id=branch.branch_id,
                selected_branch_label=branch.display_label,
            )
        except Exception as xcp:
            self._finish_operation(
                state=AppUpdateState.FAILED,
                summary=f"{kind.value.title()} failed for {self.app.friendly}.",
                detail=str(xcp),
            )
            log.warning(
                "Steam update task failed: app=%s kind=%s branch=%s error=%s",
                self.app.friendly,
                kind.value,
                branch.branch_id,
                xcp,
            )
            raise
        finally:
            with self._state_lock:
                self._operation_running = False
                self._active_task = None

    async def _run_steamcmd(self, *, branch: SteamUpdateBranch, validate: bool) -> bool:
        install_dir = str(self.app.directory)
        command = self._steamcmd_command(branch=branch, validate=validate)
        log.info(
            "Running SteamCMD: app=%s cwd=%s command=%s",
            self.app.friendly,
            install_dir,
            _display_command(command),
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=install_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        await asyncio.gather(
            self._consume_process_stream(process.stdout, source="stdout", sink=stdout_lines),
            self._consume_process_stream(process.stderr, source="stderr", sink=stderr_lines),
        )
        return_code = await process.wait()
        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines)
        if return_code != 0:
            raise RuntimeError(_command_error_text(command=command, stdout_text=stdout_text, stderr_text=stderr_text))
        success_markers = (
            "Success! App",
            "fully installed",
            "fully installed.",
            "Success! Verified",
        )
        completed = any(marker in stdout_text for marker in success_markers)
        log.info(
            "SteamCMD finished: app=%s returncode=%s success=%s stdout_lines=%s stderr_lines=%s",
            self.app.friendly,
            return_code,
            completed,
            len(stdout_lines),
            len(stderr_lines),
        )
        return completed

    def _steamcmd_command(self, *, branch: SteamUpdateBranch, validate: bool) -> list[str]:
        steam_update = self._steam_update_config()
        login = steam_update.login
        command: list[str] = [
            *self._steamcmd_command_prefix,
            "+force_install_dir",
            str(self.app.directory),
            "+login",
            login.username,
        ]
        if login.username.casefold() != "anonymous":
            if login.password is None:
                raise ValueError("Steam login password is required for non-anonymous logins.")
            command.append(login.password)
        command.extend(["+app_update", str(self._app_id), "-beta", branch.branch_id])
        if branch.beta_password is not None:
            command.extend(["-betapassword", branch.beta_password])
        if validate:
            command.append("validate")
        command.append("+quit")
        return command

    async def _consume_process_stream(
        self,
        stream: asyncio.StreamReader | None,
        *,
        source: str,
        sink: list[str],
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.readline()
            if not chunk:
                return
            line = chunk.decode(config.STR_ENCODE, errors="replace").rstrip("\r\n")
            sink.append(line)
            self._record_output_line(source=source, line=line)

    def _detect_installed_version(self) -> AppVersion | None:
        return self.app.detect_installed_version()

    def _version_with_manifest_data(
        self,
        version: AppVersion | None,
        manifest_state: SteamAppManifestState | None,
    ) -> AppVersion | None:
        if manifest_state is None:
            return version
        base_version = version if version is not None else self._app_config().version
        if base_version is None:
            return None
        return base_version.model_copy(
            update={
                "steam_build": manifest_state.build_id,
                "steam_branch": manifest_state.branch_id,
            }
        )

    def _read_installed_manifest(self) -> SteamAppManifestState | None:
        manifest_path = self._installed_manifest_path()
        if manifest_path is None:
            return None
        raw_root = _parse_acf_mapping(manifest_path.read_text(encoding=config.STR_ENCODE))
        raw_state = raw_root.get("AppState")
        if not isinstance(raw_state, Mapping):
            raise ValueError(f"Steam app manifest is invalid: {manifest_path}")
        build_id = _optional_positive_int(raw_state.get("buildid"), field_name="Steam build id")
        raw_user_config = raw_state.get("UserConfig")
        branch_id: str | None = None
        if isinstance(raw_user_config, Mapping):
            branch_id = _optional_text(raw_user_config.get("betakey"))
        if branch_id is None:
            branch_id = "public"
        return SteamAppManifestState(
            app_id=self._app_id,
            build_id=build_id,
            branch_id=branch_id,
        )

    def _safe_read_installed_manifest(self) -> SteamAppManifestState | None:
        try:
            manifest_state = self._read_installed_manifest()
        except Exception as xcp:
            log.warning("Failed to parse Steam app manifest for %s: %s", self.app.friendly, xcp)
            return None
        if manifest_state is not None:
            manifest_signature = (
                manifest_state.app_id,
                manifest_state.branch_id,
                manifest_state.build_id,
            )
            if manifest_signature != self._last_logged_manifest_signature:
                log.info(
                    "Steam app manifest loaded: app=%s app_id=%s branch=%s build=%s",
                    self.app.friendly,
                    manifest_state.app_id,
                    manifest_state.branch_id,
                    manifest_state.build_id,
                )
                self._last_logged_manifest_signature = manifest_signature
        return manifest_state

    def _installed_manifest_path(self) -> Path | None:
        manifest_name = f"appmanifest_{self._app_id}.acf"
        for candidate in _steam_manifest_candidates(self.app.directory, manifest_name=manifest_name):
            if candidate.is_file():
                return candidate
        return None

    def _steam_update_config(self) -> SteamUpdateConfig:
        cfg = self._app_config()
        steam_update = cfg.steam_update
        if steam_update is None:
            raise _errors.UnsupportedUpdate(f"{self.app.friendly} does not have a Steam update configuration.")
        return steam_update

    def _app_config(self) -> App_Config:
        return self.app.cfg

    def _record_output_line(self, *, source: str, line: str) -> None:
        clean_line = line.strip()
        self._append_command_log(source=source, line=line)
        if not clean_line:
            return
        progress_match = _STEAMCMD_PROGRESS_RE.search(clean_line)
        if progress_match is not None:
            log.info(
                "SteamCMD progress: app=%s source=%s phase=%s progress=%s",
                self.app.friendly,
                source,
                progress_match.group("phase").strip(),
                progress_match.group("progress"),
            )
        elif source == "stderr":
            log.warning("SteamCMD stderr: app=%s line=%s", self.app.friendly, clean_line)
        elif clean_line.startswith("Success!"):
            log.info("SteamCMD success marker: app=%s line=%s", self.app.friendly, clean_line)
        with self._state_lock:
            self._log_tail.append(f"{source}: {clean_line}")
            if self._status.state is not AppUpdateState.RUNNING:
                return
            next_status = replace(
                self._status,
                summary=clean_line,
                detail=clean_line,
                log_lines=tuple(self._log_tail),
            )
            if progress_match is not None:
                phase_text = progress_match.group("phase").strip()
                progress_percent = float(progress_match.group("progress"))
                next_status = replace(
                    next_status,
                    summary=phase_text[:1].upper() + phase_text[1:] if phase_text else clean_line,
                    progress_percent=progress_percent,
                )
            self._status = next_status

    def _finish_operation(
        self,
        *,
        state: AppUpdateState,
        summary: str,
        detail: str | None,
        progress_percent: float | None = None,
    ) -> None:
        finished_at_unix_ms = _unix_ms_now()
        with self._state_lock:
            self._status = replace(
                self._status,
                state=state,
                summary=summary,
                detail=detail,
                progress_percent=progress_percent,
                log_lines=tuple(self._log_tail),
                finished_at_unix_ms=finished_at_unix_ms,
            )

    def _reset_command_log(self, *, kind: AppUpdateOperationKind, branch: SteamUpdateBranch) -> None:
        log_dir = self.app.dir_log
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "steamcmd-update.log"
        log_path.write_text(
            "\n".join(
                (
                    f"# SteamCMD {kind.value}",
                    f"# Branch: {branch.branch_id}",
                    f"# Started: {int(time.time())}",
                    "",
                )
            ),
            config.STR_ENCODE,
        )

    def _append_command_log(self, *, source: str, line: str) -> None:
        log_path = self.app.dir_log / "steamcmd-update.log"
        with log_path.open("a", encoding=config.STR_ENCODE) as file_handle:
            file_handle.write(f"[{source}] {line}\n")

    def _log_background_task_outcome(self, task: asyncio.Task[AppUpdateOperationResult]) -> None:
        if task.cancelled():
            log.warning("SteamCMD update task cancelled: app=%s", self.app.friendly)
            return
        error = task.exception()
        if error is not None:
            log.warning("SteamCMD update task failed: app=%s error=%s", self.app.friendly, error)

    @staticmethod
    def _resolve_steamcmd_command_prefix(steam_update: SteamUpdateConfig) -> tuple[str, ...]:
        bot_config = config.load_bot_configuration(Path("configuration.json"))
        configured_command = bot_config.steamcmd_path
        if steam_update.steamcmd_executable != "steamcmd":
            configured_command = steam_update.steamcmd_executable
        return config.steamcmd_command_prefix(configured_command)


def _mapping(raw: object, *, label: str) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} is invalid.")
    payload: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} key is invalid.")
        payload[key] = value
    return payload


def _command_error_text(*, command: list[str], stdout_text: str, stderr_text: str) -> str:
    output_lines = [line.strip() for line in (stdout_text + "\n" + stderr_text).splitlines() if line.strip()]
    tail_lines = output_lines[-4:]
    tail_text = " | ".join(tail_lines)
    if tail_text:
        return f"Command failed: {' '.join(command)} [{tail_text}]"
    return f"Command failed: {' '.join(command)}"


def _display_command(command: list[str]) -> str:
    parts = list(command)
    for index, part in enumerate(parts):
        if part == "+login" and index + 2 < len(parts):
            if parts[index + 1].casefold() != "anonymous":
                parts[index + 2] = "******"
    return " ".join(parts)


def _optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("Steam manifest text value is invalid.")
    text = raw.strip()
    return text or None


def _optional_positive_int(raw: object, *, field_name: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} is invalid.")
    text = raw.strip()
    if not text:
        return None
    if not text.isdecimal():
        raise ValueError(f"{field_name} is invalid.")
    return int(text)


def _steam_manifest_candidates(directory: Path, *, manifest_name: str) -> tuple[Path, ...]:
    candidates: list[Path] = [directory / "steamapps" / manifest_name]
    seen: set[Path] = set(candidates)
    for parent in directory.parents:
        if parent.name.casefold() == "steamapps":
            candidate = parent / manifest_name
            if candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
        candidate = parent / "steamapps" / manifest_name
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    return tuple(candidates)


def _parse_acf_mapping(text: str) -> dict[str, object]:
    tokens = _acf_tokens(text)
    payload, index = _parse_acf_block(tokens=tokens, index=0, stop_on_brace=False)
    if index != len(tokens):
        raise ValueError("Steam app manifest has trailing tokens.")
    return payload


def _acf_tokens(text: str) -> tuple[str, ...]:
    tokens: list[str] = []
    position = 0
    length = len(text)
    while position < length:
        char = text[position]
        if char.isspace():
            position += 1
            continue
        if char in "{}":
            tokens.append(char)
            position += 1
            continue
        if char != '"':
            raise ValueError(f"Steam app manifest contains unexpected token near position {position}.")
        position += 1
        value_chars: list[str] = []
        while position < length:
            next_char = text[position]
            if next_char == "\\":
                if position + 1 >= length:
                    raise ValueError("Steam app manifest contains an incomplete escape sequence.")
                value_chars.append(text[position + 1])
                position += 2
                continue
            if next_char == '"':
                position += 1
                break
            value_chars.append(next_char)
            position += 1
        else:
            raise ValueError("Steam app manifest contains an unterminated string.")
        tokens.append("".join(value_chars))
    return tuple(tokens)


def _parse_acf_block(
    *,
    tokens: tuple[str, ...],
    index: int,
    stop_on_brace: bool,
) -> tuple[dict[str, object], int]:
    payload: dict[str, object] = {}
    while index < len(tokens):
        token = tokens[index]
        if token == "}":
            if not stop_on_brace:
                raise ValueError("Steam app manifest contains an unexpected closing brace.")
            return payload, index + 1
        if token == "{":
            raise ValueError("Steam app manifest contains an unexpected opening brace.")
        key = token
        index += 1
        if index >= len(tokens):
            raise ValueError(f"Steam app manifest is missing a value for {key!r}.")
        next_token = tokens[index]
        if next_token == "{":
            value, index = _parse_acf_block(tokens=tokens, index=index + 1, stop_on_brace=True)
            payload[key] = value
            continue
        if next_token == "}":
            raise ValueError(f"Steam app manifest is missing a value for {key!r}.")
        payload[key] = next_token
        index += 1
    if stop_on_brace:
        raise ValueError("Steam app manifest is missing a closing brace.")
    return payload, index


_STEAMCMD_PROGRESS_RE = re.compile(
    r"Update state \([^)]*\)\s*(?P<phase>[^,]+),\s*progress:\s*(?P<progress>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _unix_ms_now() -> int:
    return int(time.time() * 1000)
