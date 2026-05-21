from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
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


def ssh_control_path(target: RemoteTarget) -> Path:
    return Path(tempfile.gettempdir()) / f"yukibot-{target.name.value}.ssh"


def ssh_connection_options(target: RemoteTarget) -> list[str]:
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
        f"ControlPath={ssh_control_path(target).as_posix()}",
    ]


def open_ssh_master(target: RemoteTarget) -> None:
    run_checked(
        [
            "sshpass",
            "-e",
            "ssh",
            "-M",
            "-N",
            "-f",
            *ssh_connection_options(target),
            target.ssh_destination,
        ],
        password=target.password,
    )


def close_ssh_master(target: RemoteTarget) -> None:
    control_path = ssh_control_path(target)
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


def remote_parent_directories(target: RemoteTarget, files: list[Path]) -> list[PurePosixPath]:
    directories = {remote_file_path(target, path).parent for path in files}
    return sorted(directories, key=lambda path: path.as_posix())


def ensure_remote_directories(target: RemoteTarget, files: list[Path]) -> None:
    directories = remote_parent_directories(target, files)
    if not directories:
        raise RuntimeError("No remote directories were derived from tracked Python files")

    quoted_directories = " ".join(shlex.quote(directory.as_posix()) for directory in directories)
    run_checked(
        [
            "sshpass",
            "-e",
            "ssh",
            *ssh_connection_options(target),
            target.ssh_destination,
            f"{REMOTE_MKDIR_PATH} -p -- {quoted_directories}",
        ],
        password=target.password,
    )


def sync_python_files(target: RemoteTarget, files: list[Path]) -> None:
    for path in files:
        local_path = REPO_ROOT / path
        remote_path = remote_file_path(target, path).as_posix()
        remote_command = f"{REMOTE_CAT_PATH} > {shlex.quote(remote_path)}"
        command = [
            "ssh",
            *ssh_connection_options(target),
            target.ssh_destination,
            remote_command,
        ]
        run_checked_bytes(command, password=None, stdin_bytes=local_path.read_bytes())


def print_check_plan(target: RemoteTarget, files: list[Path]) -> None:
    print(f"[check] {target.name.value} ({target.host})")
    for path in files:
        print(f"  {path.as_posix()} -> {remote_file_path(target, path).as_posix()}")


def restart_remote(target: RemoteTarget) -> None:
    if target.restart_command is None:
        raise ValueError(f"{target.name.value} restart_command is not configured")
    run_checked(
        [
            "ssh",
            *ssh_connection_options(target),
            target.ssh_destination,
            target.restart_command,
        ],
        password=None,
    )


def restart_delay_seconds(target: RemoteTarget, restart_targets: list[RemoteTarget]) -> int:
    target_names = {configured_target.name for configured_target in restart_targets}
    if target.name is TargetName.KOUSEI and TargetName.WAKUSEI in target_names and TargetName.KOUSEI in target_names:
        return KOUSEI_RESTART_DELAY_SECONDS
    return 0


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

    for target in targets:
        print(f"==> {target.name.value} ({target.host})")
        open_ssh_master(target)
        try:
            ensure_remote_directories(target, files)
            sync_python_files(target, files)
            if args.restart:
                delay_seconds = restart_delay_seconds(target, targets)
                if delay_seconds > 0:
                    print(f"Waiting {delay_seconds}s before restarting {target.name.value}")
                    time.sleep(delay_seconds)
                restart_remote(target)
        finally:
            close_ssh_master(target)

    return 0


if __name__ == "__main__":
    sys.exit(main())
