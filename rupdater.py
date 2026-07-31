from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from _io import BytesIO
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from subprocess import CompletedProcess
from typing import Final

from deployment_metadata import DEPLOYMENT_METADATA_RELATIVE_PATH, DeploymentMetadata
from restart_state import PENDING_PROCESS_RESTART_KIND_PATH, RestartKind, is_process_restart_kind

REPO_ROOT: Path = Path(__file__).resolve().parent
DEFAULT_TARGETS_FILE: Final[Path] = REPO_ROOT / "rupdater.targets.json"
SSH_CONNECTION_TIMEOUT_SECONDS = 5
SSH_CONTROL_PERSIST_SECONDS = 30
RESTART_INTERVAL_SECONDS: Final[int] = 5
REMOTE_MKDIR_PATH = "/bin/mkdir"
REMOTE_CAT_PATH = "/bin/cat"
REMOTE_RM_PATH = "/bin/rm"
REMOTE_SH_PATH = "/bin/sh"
REMOTE_TAR_PATH = "/usr/bin/tar"
REMOTE_USER_PATH_PREFIXES: tuple[str, ...] = (
    "$HOME/.local/bin",
    "$HOME/.cargo/bin",
)
REMOTE_SYSTEM_PATH_PREFIXES: tuple[str, ...] = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)
ALWAYS_SYNCED_FILES: tuple[Path, ...] = (
    Path("pyproject.toml"),
    Path("uv.lock"),
)
REMOTE_UV_SYNC_COMMAND = "uv sync"
NO_CHANGED_PYTHON_FILES_ERROR = "git status returned no changed Python files"


class TargetName(StrEnum):
    WAKUSEI = "wakusei"
    KOUSEI = "kousei"
    PORTAL = "portal"


@dataclass(frozen=True, slots=True)
class RemoteTarget:
    name: TargetName
    host: str
    user: str
    password: str | None
    remote_root: PurePosixPath
    restart_command: str | None = None

    @property
    def ssh_destination(self) -> str:
        return f"{self.user}@{self.host}"

    def validate(self) -> None:
        if not self.host.strip():
            raise ValueError(f"{self.name.value} host must not be blank")
        if not self.user.strip():
            raise ValueError(f"{self.name.value} user must not be blank")
        if not self.remote_root.is_absolute() or self.remote_root == PurePosixPath("/"):
            raise ValueError(f"{self.name.value} remote_root must be a non-root absolute path")
        if self.restart_command is not None and not self.restart_command.strip():
            raise ValueError(f"{self.name.value} restart_command must not be blank when configured")


@dataclass(frozen=True, slots=True)
class RemoteSession:
    target: RemoteTarget
    control_path: Path


@dataclass(frozen=True, slots=True)
class SyncPlan:
    write_files: tuple[Path, ...]
    delete_files: tuple[Path, ...]


def parse_args() -> argparse.Namespace:
    parser: ArgumentParser = argparse.ArgumentParser(
        description="Synchronise Yukibot source files to one or more remote hosts.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        choices=[target.value for target in TargetName],
        default=[target.value for target in TargetName],
        help="Targets to update. Defaults to all configured targets.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Show what would be synced without copying files.",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Run each target's restart_command after syncing.",
    )
    parser.add_argument(
        "--dry",
        action="store_true",
        help="Write the planned file list to a local .txt report and skip remote actions.",
    )
    parser.add_argument(
        "--all-tracked",
        action="store_true",
        help="Sync all tracked Python files instead of only changed local Python files.",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Commit local changes, deploy that revision, record its metadata, and restart each selected target.",
    )
    parser.add_argument(
        "--targets-file",
        type=Path,
        default=DEFAULT_TARGETS_FILE,
        help="Ignored JSON file containing remote target settings (default: rupdater.targets.json).",
    )
    return parser.parse_args()


def _target_file_path(path: Path) -> Path:
    return path if path.is_absolute() else REPO_ROOT / path


def _json_object(*, value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object.")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _required_target_text(*, settings: dict[str, object], target_name: TargetName, field: str) -> str:
    value: object = settings.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Target {target_name.value!r} requires a non-blank {field!r} string.")
    return value.strip()


def _optional_target_text(*, settings: dict[str, object], target_name: TargetName, field: str) -> str | None:
    value: object = settings.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Target {target_name.value!r} {field!r} must be a string or null.")
    stripped_value: str = value.strip()
    return stripped_value or None


def remote_targets_from_json(*, raw: object) -> dict[TargetName, RemoteTarget]:
    payload: dict[str, object] = _json_object(value=raw, label="Target configuration")
    raw_targets: object = payload.get("targets")
    target_settings_by_name: dict[str, object] = _json_object(value=raw_targets, label="Target configuration targets")
    expected_target_names: set[str] = {target_name.value for target_name in TargetName}
    configured_target_names: set[str] = set(target_settings_by_name)
    unknown_target_names: set[str] = configured_target_names - expected_target_names
    missing_target_names: set[str] = expected_target_names - configured_target_names
    if unknown_target_names or missing_target_names:
        details: list[str] = []
        if missing_target_names:
            details.append(f"missing: {', '.join(sorted(missing_target_names))}")
        if unknown_target_names:
            details.append(f"unknown: {', '.join(sorted(unknown_target_names))}")
        raise ValueError(f"Target configuration must define exactly the supported targets ({'; '.join(details)}).")

    targets: dict[TargetName, RemoteTarget] = {}
    for target_name in TargetName:
        settings: dict[str, object] = _json_object(
            value=target_settings_by_name[target_name.value],
            label=f"Target {target_name.value!r}",
        )
        target = RemoteTarget(
            name=target_name,
            host=_required_target_text(settings=settings, target_name=target_name, field="host"),
            user=_required_target_text(settings=settings, target_name=target_name, field="user"),
            password=_optional_target_text(settings=settings, target_name=target_name, field="password"),
            remote_root=PurePosixPath(
                _required_target_text(settings=settings, target_name=target_name, field="remote_root")
            ),
            restart_command=_required_target_text(settings=settings, target_name=target_name, field="restart_command"),
        )
        target.validate()
        targets[target_name] = target
    return targets


def load_remote_targets(*, target_file: Path) -> dict[TargetName, RemoteTarget]:
    resolved_path: Path = _target_file_path(target_file)
    if not resolved_path.exists():
        raise FileNotFoundError(f"Updater target configuration does not exist: {resolved_path}")
    if os.name != "nt" and resolved_path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"Updater target configuration must be owner-only (chmod 600): {resolved_path}")
    try:
        raw: object = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as xcp:
        raise ValueError(f"Updater target configuration must contain valid JSON: {resolved_path}") from xcp
    return remote_targets_from_json(raw=raw)


def require_program(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Required program not found on PATH: {name}")


def parse_tracked_python_files(stdout: str) -> list[Path]:
    files: list[Path] = [Path(line.strip()) for line in stdout.splitlines() if line.strip()]
    if not files:
        raise RuntimeError("git ls-files returned no tracked Python files")
    return files


def tracked_python_files() -> list[Path]:
    result: CompletedProcess[str] = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_tracked_python_files(result.stdout)


def parse_tracked_project_files(stdout: str) -> list[Path]:
    files: list[Path] = [Path(line) for line in stdout.splitlines() if line]
    if not files:
        raise RuntimeError("git ls-files returned no tracked project files")
    return files


def tracked_project_files() -> list[Path]:
    result: CompletedProcess[str] = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_tracked_project_files(result.stdout)


def planned_sync_files(source_files: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for path in [*source_files, *ALWAYS_SYNCED_FILES]:
        if path in seen:
            continue
        local_path: Path = REPO_ROOT / path
        if not local_path.is_file():
            raise RuntimeError(f"Required sync file does not exist: {path.as_posix()}")
        seen.add(path)
        files.append(path)
    return files


def _append_unique_path(files: list[Path], seen: set[Path], path: Path) -> None:
    if path not in seen:
        seen.add(path)
        files.append(path)


def parse_changed_python_plan(stdout: str) -> SyncPlan:
    write_files: list[Path] = []
    delete_files: list[Path] = []
    seen_write: set[Path] = set[Path]()
    seen_delete: set[Path] = set[Path]()
    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        status: str = raw_line[:2]
        path_text: str = raw_line[3:]
        old_path: Path | None = None
        is_rename: bool = "R" in status
        if is_rename and " -> " in path_text:
            old_text, path_text = path_text.rsplit(" -> ", 1)
            old_path = Path(old_text)
        path: Path = Path(path_text)
        if is_rename and old_path is not None and old_path != path:
            if old_path.suffix == ".py":
                _append_unique_path(delete_files, seen_delete, old_path)
        if "D" in status:
            if path.suffix == ".py":
                _append_unique_path(delete_files, seen_delete, path)
            continue
        if path.suffix != ".py":
            continue
        if not (REPO_ROOT / path).is_file():
            continue
        _append_unique_path(write_files, seen_write, path)
    if not write_files and not delete_files:
        raise RuntimeError(NO_CHANGED_PYTHON_FILES_ERROR)
    return SyncPlan(write_files=tuple[Path, ...](write_files), delete_files=tuple[Path, ...](delete_files))


def parse_changed_python_files(stdout: str) -> list[Path]:
    files: list[Path] = list[Path](parse_changed_python_plan(stdout).write_files)
    if not files:
        raise RuntimeError(NO_CHANGED_PYTHON_FILES_ERROR)
    return files


def changed_python_plan() -> SyncPlan:
    result: CompletedProcess[str] = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", "*.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_changed_python_plan(result.stdout)


def tracked_python_sync_plan() -> SyncPlan:
    return SyncPlan(write_files=tuple[Path, ...](tracked_python_files()), delete_files=())


def tracked_project_sync_plan() -> SyncPlan:
    return SyncPlan(write_files=tuple[Path, ...](tracked_project_files()), delete_files=())


def select_sync_plan(*, sync_all_tracked: bool) -> SyncPlan:
    if sync_all_tracked:
        return tracked_python_sync_plan()
    try:
        return changed_python_plan()
    except RuntimeError as error:
        if str(error) != NO_CHANGED_PYTHON_FILES_ERROR:
            raise
        print("No changed Python files detected; falling back to syncing all tracked Python files.")
        return tracked_python_sync_plan()


def changed_python_files() -> list[Path]:
    return list[Path](changed_python_plan().write_files)


def dry_run_report_path() -> Path:
    return REPO_ROOT / "update_remotes_dry.txt"


def write_dry_run_report(targets: list[RemoteTarget], plan: SyncPlan, files: list[Path]) -> Path:
    report_path: Path = dry_run_report_path()
    report_lines: list[str] = [
        "Planned Yukibot sync",
        f"Targets: {', '.join(target.name.value for target in targets)}",
        f"Write count: {len(files)}",
        f"Delete count: {len(plan.delete_files)}",
        "",
    ]
    report_lines.append("Write files:")
    report_lines.extend(path.as_posix() for path in files)
    if plan.delete_files:
        report_lines.extend(["", "Delete files:"])
        report_lines.extend(path.as_posix() for path in plan.delete_files)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return report_path


def run_checked(
    command: list[str],
    *,
    password: str | None,
    stdin_text: str | None = None,
    stdin_bytes: bytes | None = None,
) -> None:
    if stdin_text is not None and stdin_bytes is not None:
        raise ValueError("stdin_text and stdin_bytes are mutually exclusive")
    env: dict[str, str] = os.environ.copy()
    if password is not None:
        env["SSHPASS"] = password
    print(shlex.join(command))
    subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        text=stdin_bytes is None,
        input=stdin_text if stdin_bytes is None else stdin_bytes,
        env=env,
    )


def run_captured(command: list[str], *, password: str | None) -> str:
    env: dict[str, str] = os.environ.copy()
    if password is not None:
        env["SSHPASS"] = password
    print(shlex.join(command))
    result: CompletedProcess[str] = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def working_tree_status() -> str:
    result: CompletedProcess[str] = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def prompt_release_message() -> str:
    try:
        message: str = input("Release commit message: ").strip()
    except EOFError as xcp:
        raise RuntimeError("A release commit message is required, but standard input is unavailable.") from xcp
    if not message:
        raise ValueError("A release commit message is required.")
    return message


def commit_release_changes(*, message: str) -> bool:
    """Stage and commit every non-ignored local change for a release deployment."""
    if not message.strip():
        raise ValueError("Release commit messages must not be blank.")
    if not working_tree_status():
        return False
    run_checked(["git", "add", "--all"], password=None)
    run_checked(["git", "commit", "-m", message], password=None)
    if working_tree_status():
        raise RuntimeError("Release commit completed but the working tree is still not clean.")
    return True


def release_revision() -> str:
    result: CompletedProcess[str] = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def release_version() -> str | None:
    result: CompletedProcess[str] = subprocess.run(
        ["git", "tag", "--points-at", "HEAD", "--sort=-version:refname"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    versions: list[str] = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return versions[0] if versions else None


def build_deployment_metadata(
    *,
    target_name: TargetName,
    source_files: tuple[Path, ...],
    now: datetime | None = None,
) -> DeploymentMetadata:
    return DeploymentMetadata(
        revision=release_revision(),
        deployed_at=now or datetime.now(timezone.utc),
        target_name=target_name.value,
        version=release_version(),
        source_paths=tuple(PurePosixPath(path.as_posix()) for path in source_files),
    )


def remote_file_path(target: RemoteTarget, relative_path: Path) -> PurePosixPath:
    return target.remote_root / PurePosixPath(relative_path.as_posix())


def ssh_control_path(target: RemoteTarget, run_token: str) -> Path:
    return Path(tempfile.gettempdir()) / f"yukibot-{target.name.value}-{run_token}.ssh"


def ssh_connection_options(*, control_path: Path, use_password: bool) -> list[str]:
    options: list[str] = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={SSH_CONNECTION_TIMEOUT_SECONDS}",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPersist={SSH_CONTROL_PERSIST_SECONDS}s",
        "-o",
        f"ControlPath={control_path.as_posix()}",
    ]
    if use_password:
        options[:0] = ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no"]
    return options


def open_ssh_master(target: RemoteTarget, control_path: Path) -> None:
    command: list[str] = [
        "ssh",
        "-M",
        "-N",
        "-f",
        *ssh_connection_options(control_path=control_path, use_password=target.password is not None),
        target.ssh_destination,
    ]
    if target.password is not None:
        command[:0] = ["sshpass", "-e"]
    run_checked(
        command,
        password=target.password,
    )


def close_ssh_master(target: RemoteTarget, control_path: Path) -> None:
    if not control_path.exists():
        return

    try:
        run_checked(
            [
                "ssh",
                "-O",
                "exit",
                "-o",
                f"ControlPath={control_path.as_posix()}",
                target.ssh_destination,
            ],
            password=None,
        )
    finally:
        control_path.unlink(missing_ok=True)


def open_remote_session(target: RemoteTarget, run_token: str) -> RemoteSession:
    control_path: Path = ssh_control_path(target, run_token)
    open_ssh_master(target, control_path)
    return RemoteSession(target=target, control_path=control_path)


def close_remote_session(session: RemoteSession) -> None:
    close_ssh_master(session.target, session.control_path)


def remote_parent_directories(target: RemoteTarget, files: list[Path]) -> list[PurePosixPath]:
    directories: set[PurePosixPath] = {remote_file_path(target, path).parent for path in files}
    return sorted(directories, key=lambda path: path.as_posix())


def heredoc_delimiter(path: Path, content: str) -> str:
    base: str = f"__YUKIBOT_SYNC_{path.as_posix().replace('/', '_').replace('.', '_').upper()}__"
    delimiter: str = base
    suffix = 0
    while delimiter in content:
        suffix += 1
        delimiter = f"{base}_{suffix}"
    return delimiter


def build_remote_sync_script(target: RemoteTarget, files: list[Path]) -> str:
    directories: list[PurePosixPath] = remote_parent_directories(target, files)
    if not directories:
        raise RuntimeError("No remote directories were derived from the selected files")

    lines: list[str] = ["set -eu"]
    quoted_directories: str = " ".join(shlex.quote(directory.as_posix()) for directory in directories)
    lines.append(f"{REMOTE_MKDIR_PATH} -p -- {quoted_directories}")

    for path in files:
        remote_path: str = remote_file_path(target, path).as_posix()
        content: str = (REPO_ROOT / path).read_text(encoding="utf-8")
        delimiter: str = heredoc_delimiter(path, content)
        lines.append(f"{REMOTE_CAT_PATH} > {shlex.quote(remote_path)} <<'{delimiter}'")
        lines.append(content)
        if not content.endswith("\n"):
            lines.append("")
        lines.append(delimiter)

    return "\n".join(lines) + "\n"


def build_sync_archive(files: list[Path]) -> bytes:
    archive_buffer: BytesIO = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        for path in files:
            archive.add(REPO_ROOT / path, arcname=path.as_posix(), recursive=False)
    return archive_buffer.getvalue()


def build_remote_extract_command(target: RemoteTarget) -> str:
    remote_root: str = shlex.quote(target.remote_root.as_posix())
    return f"{REMOTE_MKDIR_PATH} -p -- {remote_root} && {REMOTE_TAR_PATH} -xf - -C {remote_root}"


def sync_files(session: RemoteSession, files: list[Path]) -> None:
    archive_bytes: bytes = build_sync_archive(files)
    run_checked(
        [
            "ssh",
            *ssh_connection_options(control_path=session.control_path, use_password=False),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_extract_command(session.target)),
        ],
        password=None,
        stdin_bytes=archive_bytes,
    )


def delete_remote_files(session: RemoteSession, files: list[Path]) -> None:
    if not files:
        return
    quoted_paths: str = " ".join(shlex.quote(remote_file_path(session.target, path).as_posix()) for path in files)
    run_checked(
        [
            "ssh",
            *ssh_connection_options(control_path=session.control_path, use_password=False),
            session.target.ssh_destination,
            build_remote_shell_command(f"{REMOTE_RM_PATH} -f -- {quoted_paths}"),
        ],
        password=None,
    )


def build_remote_project_command(target: RemoteTarget, command: str) -> str:
    return f"{build_remote_path_setup_command()}cd {shlex.quote(target.remote_root.as_posix())} && {command}"


def build_pending_restart_kind_write_command(kind: RestartKind) -> str:
    if not is_process_restart_kind(kind):
        raise ValueError(f"{kind.value!r} is not a process restart kind")
    payload: str = json.dumps({"kind": kind.value}, sort_keys=True)
    sentinel_path: str = shlex.quote(PENDING_PROCESS_RESTART_KIND_PATH.as_posix())
    return f"printf '%s\\n' {shlex.quote(payload)} > {sentinel_path}"


def build_remote_restart_command(target: RemoteTarget, kind: RestartKind = RestartKind.UPDATE_BOT) -> str:
    if target.restart_command is None:
        raise ValueError(f"{target.name.value} restart_command is not configured")
    return f"{build_pending_restart_kind_write_command(kind)} && {target.restart_command}"


def build_remote_program_check_command(program_name: str) -> str:
    quoted_program_name: str = shlex.quote(program_name)
    return (
        f"{build_remote_path_setup_command()}"
        f"command -v {quoted_program_name} >/dev/null 2>&1 || "
        f"{{ echo 'Required program not found on remote PATH: {quoted_program_name}' >&2; exit 1; }}"
    )


def build_remote_command_path_check_command(command_path: str) -> str:
    quoted_command_path: str = shlex.quote(command_path)
    return (
        f"[ -x {quoted_command_path} ] || "
        f"{{ echo 'Required remote command is missing or not executable: {quoted_command_path}' >&2; exit 1; }}"
    )


def build_remote_path_setup_command() -> str:
    prefix: str = ":".join((*REMOTE_USER_PATH_PREFIXES, *REMOTE_SYSTEM_PATH_PREFIXES))
    return f'export PATH="{prefix}${{PATH:+:$PATH}}"; '


def build_remote_shell_command(command: str) -> str:
    return f"{REMOTE_SH_PATH} -c {shlex.quote(command)}"


def run_remote_project_command(session: RemoteSession, command: str) -> None:
    run_checked(
        [
            "ssh",
            *ssh_connection_options(control_path=session.control_path, use_password=False),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_project_command(session.target, command)),
        ],
        password=None,
    )


def run_remote_project_command_captured(session: RemoteSession, command: str) -> str:
    return run_captured(
        [
            "ssh",
            *ssh_connection_options(control_path=session.control_path, use_password=False),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_project_command(session.target, command)),
        ],
        password=None,
    )


def require_remote_program(session: RemoteSession, program_name: str) -> None:
    run_checked(
        [
            "ssh",
            *ssh_connection_options(control_path=session.control_path, use_password=False),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_program_check_command(program_name)),
        ],
        password=None,
    )


def require_remote_command_path(session: RemoteSession, command_path: str) -> None:
    run_checked(
        [
            "ssh",
            *ssh_connection_options(control_path=session.control_path, use_password=False),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_command_path_check_command(command_path)),
        ],
        password=None,
    )


def sync_remote_dependencies(session: RemoteSession) -> None:
    run_remote_project_command(session, REMOTE_UV_SYNC_COMMAND)


def build_deployment_metadata_write_command(metadata: DeploymentMetadata) -> str:
    metadata_path: str = shlex.quote(DEPLOYMENT_METADATA_RELATIVE_PATH.as_posix())
    metadata_directory: str = shlex.quote(DEPLOYMENT_METADATA_RELATIVE_PATH.parent.as_posix())
    return (
        f"{REMOTE_MKDIR_PATH} -p -- {metadata_directory} && "
        f"printf '%s\\n' {shlex.quote(metadata.to_json())} > {metadata_path}"
    )


def write_deployment_metadata(session: RemoteSession, metadata: DeploymentMetadata) -> None:
    run_remote_project_command(session, build_deployment_metadata_write_command(metadata))


def build_deployment_metadata_read_command() -> str:
    metadata_path: str = shlex.quote(DEPLOYMENT_METADATA_RELATIVE_PATH.as_posix())
    return f"if [ -f {metadata_path} ]; then {REMOTE_CAT_PATH} -- {metadata_path}; fi"


def read_deployment_metadata(session: RemoteSession) -> DeploymentMetadata | None:
    raw_metadata: str = run_remote_project_command_captured(session, build_deployment_metadata_read_command())
    if not raw_metadata.strip():
        return None
    metadata = DeploymentMetadata.from_json(raw_metadata)
    if metadata.target_name != session.target.name.value:
        raise ValueError(
            f"Remote deployment metadata target {metadata.target_name!r} does not match {session.target.name.value!r}."
        )
    return metadata


def stale_deployment_files(*, previous: DeploymentMetadata, current: DeploymentMetadata) -> list[Path]:
    current_paths: set[PurePosixPath] = set(current.source_paths)
    return [Path(path.as_posix()) for path in previous.source_paths if path not in current_paths]


def print_check_plan(target: RemoteTarget, files: list[Path], delete_files: list[Path]) -> None:
    print(f"[check] {target.name.value} ({target.host})")
    print("  write:")
    for path in files:
        print(f"    {path.as_posix()} -> {remote_file_path(target, path).as_posix()}")
    if delete_files:
        print("  delete:")
        for path in delete_files:
            print(f"    {remote_file_path(target, path).as_posix()}")


def print_synced_files(target: RemoteTarget, files: list[Path], delete_files: list[Path]) -> None:
    print(f"Updated {target.name.value}:")
    print("  wrote:")
    for path in files:
        print(f"    {path.as_posix()} -> {remote_file_path(target, path).as_posix()}")
    if delete_files:
        print("  deleted:")
        for path in delete_files:
            print(f"    {remote_file_path(target, path).as_posix()}")


def restart_remote(session: RemoteSession) -> None:
    run_checked(
        [
            "ssh",
            *ssh_connection_options(control_path=session.control_path, use_password=False),
            session.target.ssh_destination,
            build_remote_shell_command(
                build_remote_project_command(session.target, build_remote_restart_command(session.target))
            ),
        ],
        password=None,
    )


def restart_delay_seconds(target: RemoteTarget, restart_targets: list[RemoteTarget]) -> int:
    ordered_target_names = [restart_target.name for restart_target in ordered_restart_targets(restart_targets)]
    if target.name not in ordered_target_names:
        raise ValueError(f"{target.name.value} is not included in the restart targets")
    return 0 if ordered_target_names[0] is target.name else RESTART_INTERVAL_SECONDS


def ordered_restart_targets(targets: list[RemoteTarget]) -> list[RemoteTarget]:
    priority: dict[TargetName, int] = {
        TargetName.WAKUSEI: 0,
        TargetName.KOUSEI: 1,
        TargetName.PORTAL: 2,
    }
    return sorted(targets, key=lambda target: priority[target.name])


def configured_targets(*, target_names: list[str], target_file: Path) -> list[RemoteTarget]:
    targets_by_name: dict[TargetName, RemoteTarget] = load_remote_targets(target_file=target_file)
    targets: list[RemoteTarget] = [targets_by_name[TargetName(name)] for name in target_names]
    for target in targets:
        target.validate()
    return targets


def main() -> int:
    args: Namespace = parse_args()
    require_program("git")

    targets: list[RemoteTarget] = configured_targets(target_names=args.targets, target_file=args.targets_file)
    release_metadata_by_target: dict[TargetName, DeploymentMetadata] | None = None
    if args.release:
        if args.check or args.dry:
            raise ValueError("--release cannot be combined with --check or --dry.")
        if not args.restart:
            raise ValueError("--release requires --restart so recorded deployment metadata matches running code.")
        if working_tree_status():
            release_message: str = prompt_release_message()
            commit_release_changes(message=release_message)
        sync_plan = tracked_project_sync_plan()
    else:
        sync_plan = select_sync_plan(sync_all_tracked=args.all_tracked)
    files: list[Path] = planned_sync_files(list[Path](sync_plan.write_files))
    if args.release:
        release_metadata_by_target = {
            target.name: build_deployment_metadata(target_name=target.name, source_files=tuple(files))
            for target in targets
        }
        print(f"Preparing release {release_metadata_by_target[targets[0].name].revision[:7]} to sync {len(files)} files")
    if not args.release:
        print(f"Preparing to sync {len(files)} files")

    if args.dry:
        report_path: Path = write_dry_run_report(targets, sync_plan, files)
        print(f"Wrote dry run report to {report_path}")
        return 0

    if args.check:
        for target in targets:
            print_check_plan(target, files, list[Path](sync_plan.delete_files))
        return 0

    if any(target.password is not None for target in targets):
        require_program("sshpass")
    require_program("ssh")

    run_token: str = f"{os.getpid()}-{time.time_ns():x}"
    sessions_by_name: dict[TargetName, RemoteSession] = {}

    try:
        for target in targets:
            print(f"==> {target.name.value} ({target.host})")
            session: RemoteSession = open_remote_session(target, run_token)
            sessions_by_name[target.name] = session
            require_remote_command_path(session, REMOTE_TAR_PATH)
            require_remote_program(session, "uv")
            delete_files: list[Path] = list[Path](sync_plan.delete_files)
            if release_metadata_by_target is not None:
                previous_metadata: DeploymentMetadata | None = read_deployment_metadata(session)
                if previous_metadata is not None:
                    delete_files.extend(
                        stale_deployment_files(
                            previous=previous_metadata,
                            current=release_metadata_by_target[target.name],
                        )
                    )
            delete_remote_files(session, delete_files)
            sync_files(session, files)
            sync_remote_dependencies(session)
            if release_metadata_by_target is not None:
                write_deployment_metadata(session, release_metadata_by_target[target.name])
            print_synced_files(target, files, delete_files)

        if args.restart:
            for target in ordered_restart_targets(targets):
                delay_seconds: int = restart_delay_seconds(target, targets)
                if delay_seconds > 0:
                    print(f"Waiting {delay_seconds}s before restarting {target.name.value}")
                    time.sleep(delay_seconds)
                restart_remote(sessions_by_name[target.name])
    finally:
        for session in sessions_by_name.values():
            close_remote_session(session)

    return 0


if __name__ == "__main__":
    sys.exit(main())
