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
REMOTE_TAR_CANDIDATES = (
    PurePosixPath("/bin/tar"),
    PurePosixPath("/usr/bin/tar"),
)
REMOTE_TAR_EXTRACT_FLAGS = "--overwrite -xf -"


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
    tar_path: PurePosixPath


REMOTE_TARGETS: dict[TargetName, RemoteTarget] = {
    TargetName.WAKUSEI: RemoteTarget(
        name=TargetName.WAKUSEI,
        host="wakusei.apasz.com",
        user="debian",
        password="scheme-python-dingo",
        remote_root=PurePosixPath("/home/debian"),
        restart_command="/usr/bin/sudo /usr/bin/systemctl restart yukibot.service",
    ),
    TargetName.KOUSEI: RemoteTarget(
        name=TargetName.KOUSEI,
        host="kousei.apasz.com",
        user="debian",
        password="scheme-python-taiga",
        remote_root=PurePosixPath("/home/debian"),
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


def dry_run_report_path() -> Path:
    return REPO_ROOT / "update_remotes_dry.txt"


def write_dry_run_report(targets: list[RemoteTarget], files: list[Path]) -> Path:
    report_path = dry_run_report_path()
    report_lines = [
        "Planned Yukibot sync",
        f"Targets: {', '.join(target.name.value for target in targets)}",
        f"File count: {len(files)}",
        "",
    ]
    report_lines.extend(path.as_posix() for path in files)
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return report_path


def run_checked(
    command: list[str],
    *,
    password: str | None,
    stdin_text: str | None = None,
) -> None:
    env = os.environ.copy()
    if password is not None:
        env["SSHPASS"] = password
    print(shlex.join(command))
    subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        text=True,
        input=stdin_text,
        env=env,
    )


def run_checked_bytes(
    command: list[str],
    *,
    password: str | None,
    stdin_bytes: bytes,
) -> None:
    env = os.environ.copy()
    if password is not None:
        env["SSHPASS"] = password
    print(shlex.join(command))
    subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=True,
        input=stdin_bytes,
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


def run_quiet(
    command: list[str],
    *,
    password: str | None,
) -> int:
    env = os.environ.copy()
    if password is not None:
        env["SSHPASS"] = password
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return result.returncode


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


def resolve_remote_tar_path(target: RemoteTarget, control_path: Path) -> PurePosixPath:
    for candidate in REMOTE_TAR_CANDIDATES:
        return_code = run_quiet(
            [
                "ssh",
                *ssh_connection_options(control_path),
                target.ssh_destination,
                f"test -x {shlex.quote(candidate.as_posix())}",
            ],
            password=None,
        )
        if return_code == 0:
            return candidate
    raise RuntimeError(f"No remote tar binary found for {target.name.value}")


def open_remote_session(target: RemoteTarget, run_token: str) -> RemoteSession:
    control_path = ssh_control_path(target, run_token)
    open_ssh_master(target, control_path)
    tar_path = resolve_remote_tar_path(target, control_path)
    return RemoteSession(target=target, control_path=control_path, tar_path=tar_path)


def close_remote_session(session: RemoteSession) -> None:
    close_ssh_master(session.target, session.control_path)


def build_tar_archive(files: list[Path]) -> bytes:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        for path in files:
            archive.add(REPO_ROOT / path, arcname=path.as_posix(), recursive=False)
    return archive_buffer.getvalue()


def sync_python_files(session: RemoteSession, archive_bytes: bytes) -> None:
    remote_root = shlex.quote(session.target.remote_root.as_posix())
    remote_command = (
        f"{REMOTE_MKDIR_PATH} -p -- {remote_root} && "
        f"cd {remote_root} && "
        f"{session.tar_path.as_posix()} {REMOTE_TAR_EXTRACT_FLAGS}"
    )
    run_checked_bytes(
        [
            "ssh",
            *ssh_connection_options(session.control_path),
            session.target.ssh_destination,
            remote_command,
        ],
        password=None,
        stdin_bytes=archive_bytes,
    )


def print_check_plan(target: RemoteTarget, files: list[Path]) -> None:
    print(f"[check] {target.name.value} ({target.host})")
    for path in files:
        print(f"  {path.as_posix()} -> {remote_file_path(target, path).as_posix()}")


def print_synced_files(target: RemoteTarget, files: list[Path]) -> None:
    print(f"Updated {target.name.value}:")
    for path in files:
        print(f"  {path.as_posix()}")


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

    files = tracked_python_files()
    targets = configured_targets(args.targets)
    print(f"Preparing to sync {len(files)} tracked Python files")

    if args.dry:
        report_path = write_dry_run_report(targets, files)
        print(f"Wrote dry run report to {report_path}")
        return 0

    if args.check:
        for target in targets:
            print_check_plan(target, files)
        return 0

    require_program("sshpass")
    require_program("ssh")

    archive_bytes = build_tar_archive(files)
    run_token = f"{os.getpid()}-{time.time_ns():x}"
    sessions_by_name: dict[TargetName, RemoteSession] = {}

    try:
        for target in targets:
            print(f"==> {target.name.value} ({target.host})")
            session = open_remote_session(target, run_token)
            sessions_by_name[target.name] = session
            sync_python_files(session, archive_bytes)
            print_synced_files(target, files)

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
