from __future__ import annotations

import argparse
import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent
PLACEHOLDER_USER = "your-ssh-user"
PLACEHOLDER_PASSWORD = "replace-me"
PLACEHOLDER_REMOTE_ROOT = PurePosixPath("/path/to/Yukibot")
SSH_CONNECTION_TIMEOUT_SECONDS = 5
SSH_CONTROL_PERSIST_SECONDS = 30
KOUSEI_RESTART_DELAY_SECONDS = 5
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


class TargetName(StrEnum):
    WAKUSEI = "wakusei"
    KOUSEI = "kousei"


@dataclass(frozen=True, slots=True)
class RemoteTarget:
    name: TargetName
    host: str
    user: str
    password: str
    remote_root: PurePosixPath
    restart_command: str | None = None

    @property
    def ssh_destination(self) -> str:
        return f"{self.user}@{self.host}"

    def validate(self) -> None:
        if self.user == PLACEHOLDER_USER:
            raise ValueError(f"{self.name.value} user is still the placeholder value")
        if self.password == PLACEHOLDER_PASSWORD:
            raise ValueError(f"{self.name.value} password is still the placeholder value")
        if self.remote_root == PLACEHOLDER_REMOTE_ROOT:
            raise ValueError(f"{self.name.value} remote_root is still the placeholder value")


@dataclass(frozen=True, slots=True)
class RemoteSession:
    target: RemoteTarget
    control_path: Path


@dataclass(frozen=True, slots=True)
class SyncPlan:
    write_files: tuple[Path, ...]
    delete_files: tuple[Path, ...]


REMOTE_TARGETS: dict[TargetName, RemoteTarget] = {
    TargetName.WAKUSEI: RemoteTarget(
        name=TargetName.WAKUSEI,
        host="wakusei.apasz.com",
        user="debian",
        password="scheme-python-dingo",
        remote_root=PurePosixPath("/home/debian/yukibot2"),
        restart_command="/usr/bin/sudo /usr/bin/systemctl restart yukibot.service",
    ),
    TargetName.KOUSEI: RemoteTarget(
        name=TargetName.KOUSEI,
        host="kousei.apasz.com",
        user="debian",
        password="scheme-python-taiga",
        remote_root=PurePosixPath("/home/debian/erinbot"),
        restart_command="/usr/bin/sudo /usr/bin/systemctl restart erinbot.service",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy tracked Python files from this repo to one or more remote Yukibot hosts.",
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
    return parser.parse_args()


def require_program(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Required program not found on PATH: {name}")


def parse_tracked_python_files(stdout: str) -> list[Path]:
    files = [Path(line.strip()) for line in stdout.splitlines() if line.strip()]
    if not files:
        raise RuntimeError("git ls-files returned no tracked Python files")
    return files


def tracked_python_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_tracked_python_files(result.stdout)


def planned_sync_files(python_files: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for path in [*python_files, *ALWAYS_SYNCED_FILES]:
        if path in seen:
            continue
        local_path = REPO_ROOT / path
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
    seen_write: set[Path] = set()
    seen_delete: set[Path] = set()
    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        status = raw_line[:2]
        path_text = raw_line[3:]
        old_path: Path | None = None
        is_rename = "R" in status
        if is_rename and " -> " in path_text:
            old_text, path_text = path_text.rsplit(" -> ", 1)
            old_path = Path(old_text)
        path = Path(path_text)
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
        raise RuntimeError("git status returned no changed Python files")
    return SyncPlan(write_files=tuple(write_files), delete_files=tuple(delete_files))


def parse_changed_python_files(stdout: str) -> list[Path]:
    files = list(parse_changed_python_plan(stdout).write_files)
    if not files:
        raise RuntimeError("git status returned no changed Python files")
    return files


def changed_python_plan() -> SyncPlan:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", "*.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_changed_python_plan(result.stdout)


def changed_python_files() -> list[Path]:
    return list(changed_python_plan().write_files)


def dry_run_report_path() -> Path:
    return REPO_ROOT / "update_remotes_dry.txt"


def write_dry_run_report(targets: list[RemoteTarget], plan: SyncPlan, files: list[Path]) -> Path:
    report_path = dry_run_report_path()
    report_lines = [
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
    env = os.environ.copy()
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


def remote_file_path(target: RemoteTarget, relative_path: Path) -> PurePosixPath:
    return target.remote_root / PurePosixPath(relative_path.as_posix())


def ssh_control_path(target: RemoteTarget, run_token: str) -> Path:
    return Path(tempfile.gettempdir()) / f"yukibot-{target.name.value}-{run_token}.ssh"


def ssh_connection_options(control_path: Path) -> list[str]:
    return [
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
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


def open_ssh_master(target: RemoteTarget, control_path: Path) -> None:
    run_checked(
        [
            "sshpass",
            "-e",
            "ssh",
            "-M",
            "-N",
            "-f",
            *ssh_connection_options(control_path),
            target.ssh_destination,
        ],
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
    control_path = ssh_control_path(target, run_token)
    open_ssh_master(target, control_path)
    return RemoteSession(target=target, control_path=control_path)


def close_remote_session(session: RemoteSession) -> None:
    close_ssh_master(session.target, session.control_path)


def remote_parent_directories(target: RemoteTarget, files: list[Path]) -> list[PurePosixPath]:
    directories = {remote_file_path(target, path).parent for path in files}
    return sorted(directories, key=lambda path: path.as_posix())


def heredoc_delimiter(path: Path, content: str) -> str:
    base = f"__YUKIBOT_SYNC_{path.as_posix().replace('/', '_').replace('.', '_').upper()}__"
    delimiter = base
    suffix = 0
    while delimiter in content:
        suffix += 1
        delimiter = f"{base}_{suffix}"
    return delimiter


def build_remote_sync_script(target: RemoteTarget, files: list[Path]) -> str:
    directories = remote_parent_directories(target, files)
    if not directories:
        raise RuntimeError("No remote directories were derived from the selected files")

    lines: list[str] = ["set -eu"]
    quoted_directories = " ".join(shlex.quote(directory.as_posix()) for directory in directories)
    lines.append(f"{REMOTE_MKDIR_PATH} -p -- {quoted_directories}")

    for path in files:
        remote_path = remote_file_path(target, path).as_posix()
        content = (REPO_ROOT / path).read_text(encoding="utf-8")
        delimiter = heredoc_delimiter(path, content)
        lines.append(f"{REMOTE_CAT_PATH} > {shlex.quote(remote_path)} <<'{delimiter}'")
        lines.append(content)
        if not content.endswith("\n"):
            lines.append("")
        lines.append(delimiter)

    return "\n".join(lines) + "\n"


def build_sync_archive(files: list[Path]) -> bytes:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        for path in files:
            archive.add(REPO_ROOT / path, arcname=path.as_posix(), recursive=False)
    return archive_buffer.getvalue()


def build_remote_extract_command(target: RemoteTarget) -> str:
    remote_root = shlex.quote(target.remote_root.as_posix())
    return f"{REMOTE_MKDIR_PATH} -p -- {remote_root} && {REMOTE_TAR_PATH} -xf - -C {remote_root}"


def sync_python_files(session: RemoteSession, files: list[Path]) -> None:
    archive_bytes = build_sync_archive(files)
    run_checked(
        [
            "ssh",
            *ssh_connection_options(session.control_path),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_extract_command(session.target)),
        ],
        password=None,
        stdin_bytes=archive_bytes,
    )


def delete_remote_files(session: RemoteSession, files: list[Path]) -> None:
    if not files:
        return
    quoted_paths = " ".join(shlex.quote(remote_file_path(session.target, path).as_posix()) for path in files)
    run_checked(
        [
            "ssh",
            *ssh_connection_options(session.control_path),
            session.target.ssh_destination,
            build_remote_shell_command(f"{REMOTE_RM_PATH} -f -- {quoted_paths}"),
        ],
        password=None,
    )


def build_remote_project_command(target: RemoteTarget, command: str) -> str:
    return f"{build_remote_path_setup_command()}cd {shlex.quote(target.remote_root.as_posix())} && {command}"


def build_remote_program_check_command(program_name: str) -> str:
    quoted_program_name = shlex.quote(program_name)
    return (
        f"{build_remote_path_setup_command()}"
        f"command -v {quoted_program_name} >/dev/null 2>&1 || "
        f"{{ echo 'Required program not found on remote PATH: {quoted_program_name}' >&2; exit 1; }}"
    )


def build_remote_command_path_check_command(command_path: str) -> str:
    quoted_command_path = shlex.quote(command_path)
    return (
        f"[ -x {quoted_command_path} ] || "
        f"{{ echo 'Required remote command is missing or not executable: {quoted_command_path}' >&2; exit 1; }}"
    )


def build_remote_path_setup_command() -> str:
    prefix = ":".join((*REMOTE_USER_PATH_PREFIXES, *REMOTE_SYSTEM_PATH_PREFIXES))
    return f'export PATH="{prefix}${{PATH:+:$PATH}}"; '


def build_remote_shell_command(command: str) -> str:
    return f"{REMOTE_SH_PATH} -c {shlex.quote(command)}"


def run_remote_project_command(session: RemoteSession, command: str) -> None:
    run_checked(
        [
            "ssh",
            *ssh_connection_options(session.control_path),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_project_command(session.target, command)),
        ],
        password=None,
    )


def require_remote_program(session: RemoteSession, program_name: str) -> None:
    run_checked(
        [
            "ssh",
            *ssh_connection_options(session.control_path),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_program_check_command(program_name)),
        ],
        password=None,
    )


def require_remote_command_path(session: RemoteSession, command_path: str) -> None:
    run_checked(
        [
            "ssh",
            *ssh_connection_options(session.control_path),
            session.target.ssh_destination,
            build_remote_shell_command(build_remote_command_path_check_command(command_path)),
        ],
        password=None,
    )


def sync_remote_dependencies(session: RemoteSession) -> None:
    run_remote_project_command(session, REMOTE_UV_SYNC_COMMAND)


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
    if session.target.restart_command is None:
        raise ValueError(f"{session.target.name.value} restart_command is not configured")
    run_checked(
        [
            "ssh",
            *ssh_connection_options(session.control_path),
            session.target.ssh_destination,
            session.target.restart_command,
        ],
        password=None,
    )


def restart_delay_seconds(target: RemoteTarget, restart_targets: list[RemoteTarget]) -> int:
    target_names = {configured_target.name for configured_target in restart_targets}
    if target.name is TargetName.KOUSEI and TargetName.WAKUSEI in target_names and TargetName.KOUSEI in target_names:
        return KOUSEI_RESTART_DELAY_SECONDS
    return 0


def ordered_restart_targets(targets: list[RemoteTarget]) -> list[RemoteTarget]:
    priority = {
        TargetName.WAKUSEI: 0,
        TargetName.KOUSEI: 1,
    }
    return sorted(targets, key=lambda target: priority[target.name])


def configured_targets(target_names: list[str]) -> list[RemoteTarget]:
    targets = [REMOTE_TARGETS[TargetName(name)] for name in target_names]
    for target in targets:
        target.validate()
    return targets


def main() -> int:
    args = parse_args()
    require_program("git")

    sync_plan = SyncPlan(write_files=tuple(tracked_python_files()), delete_files=()) if args.all_tracked else changed_python_plan()
    files = planned_sync_files(list(sync_plan.write_files))
    targets = configured_targets(args.targets)
    print(f"Preparing to sync {len(files)} files")

    if args.dry:
        report_path = write_dry_run_report(targets, sync_plan, files)
        print(f"Wrote dry run report to {report_path}")
        return 0

    if args.check:
        for target in targets:
            print_check_plan(target, files, list(sync_plan.delete_files))
        return 0

    require_program("sshpass")
    require_program("ssh")

    run_token = f"{os.getpid()}-{time.time_ns():x}"
    sessions_by_name: dict[TargetName, RemoteSession] = {}

    try:
        for target in targets:
            print(f"==> {target.name.value} ({target.host})")
            session = open_remote_session(target, run_token)
            sessions_by_name[target.name] = session
            require_remote_command_path(session, REMOTE_TAR_PATH)
            require_remote_program(session, "uv")
            delete_remote_files(session, list(sync_plan.delete_files))
            sync_python_files(session, files)
            sync_remote_dependencies(session)
            print_synced_files(target, files, list(sync_plan.delete_files))

        if args.restart:
            for target in ordered_restart_targets(targets):
                delay_seconds = restart_delay_seconds(target, targets)
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
