#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import gettempdir
from time import monotonic, sleep

_STOP_TIMEOUT_SECONDS = 10.0
_TERMINAL_WIDTH = 88
_STATE_DIRECTORY = Path(gettempdir()) / "yukibot-dev-cluster"


class ClusterMember(StrEnum):
    YUKI = "yuki"
    ERIN = "erin"
    PORTAL = "portal"


@dataclass(frozen=True, slots=True)
class ClusterPorts:
    bind_host: str
    portal_port: int
    authority_port: int
    yuki_node_api_port: int
    erin_node_api_port: int

    @property
    def portal_base_url(self) -> str:
        return f"http://{self.bind_host}:{self.portal_port}"

    @property
    def authority_base_url(self) -> str:
        return f"http://{self.bind_host}:{self.authority_port}"

    def node_api_base_url(self, member: ClusterMember) -> str:
        if member is ClusterMember.YUKI:
            return f"http://{self.bind_host}:{self.yuki_node_api_port}"
        if member is ClusterMember.ERIN:
            return f"http://{self.bind_host}:{self.erin_node_api_port}"
        raise ValueError(f"{member.value} does not expose a dedicated node API port.")


@dataclass(frozen=True, slots=True)
class ClusterSettings:
    env_file: Path
    ports: ClusterPorts
    authority_token: str
    yuki_token: str
    erin_token: str
    yuki_started_channel: str | None = None
    erin_started_channel: str | None = None


@dataclass(slots=True)
class RunningProcess:
    member: ClusterMember
    env: dict[str, str]
    process: subprocess.Popen[str]
    output_thread: threading.Thread


@dataclass(frozen=True, slots=True)
class ClusterProcessRecord:
    member: ClusterMember
    pid: int

    def to_json(self) -> dict[str, object]:
        return {"member": self.member.value, "pid": self.pid}

    @classmethod
    def from_json(cls, payload: object) -> "ClusterProcessRecord":
        if not isinstance(payload, dict):
            raise ValueError("Process record must be a JSON object.")
        raw_member = payload.get("member")
        raw_pid = payload.get("pid")
        if not isinstance(raw_member, str):
            raise ValueError("Process record member must be a string.")
        if not isinstance(raw_pid, int):
            raise ValueError("Process record pid must be an integer.")
        return cls(member=ClusterMember(raw_member), pid=raw_pid)


def load_dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, value = stripped.partition("=")
        if separator != "=":
            raise ValueError(f"{path}:{line_number} must use KEY=VALUE syntax.")
        resolved_key = key.strip()
        if not resolved_key:
            raise ValueError(f"{path}:{line_number} is missing an environment variable name.")
        resolved_value = value.strip()
        if len(resolved_value) >= 2 and resolved_value[0] == resolved_value[-1] and resolved_value[0] in {'"', "'"}:
            resolved_value = resolved_value[1:-1]
        values[resolved_key] = resolved_value
    return values


def merged_environment(*, env_file: Path) -> dict[str, str]:
    env = load_dotenv_values(env_file)
    env.update(os.environ)
    return env


def _env_port(env: Mapping[str, str], name: str, default: int) -> int:
    raw_value = env.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value.strip())
    except ValueError as xcp:
        raise ValueError(f"{name} must be an integer port.") from xcp
    if value < 1 or value > 65535:
        raise ValueError(f"{name} must be between 1 and 65535.")
    return value


def _optional_env_value(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _required_env_value(env: Mapping[str, str], *, names: tuple[str, ...], label: str) -> str:
    value = _optional_env_value(env, *names)
    if value is None:
        expected = ", ".join(names)
        raise ValueError(f"{label} must be set in the environment or .env ({expected}).")
    return value


def settings_from_environment(*, env: Mapping[str, str], env_file: Path) -> ClusterSettings:
    bind_host = _optional_env_value(env, "DEV_CLUSTER_BIND_HOST") or "127.0.0.1"
    ports = ClusterPorts(
        bind_host=bind_host,
        portal_port=_env_port(env, "DEV_CLUSTER_PORTAL_PORT", 3180),
        authority_port=_env_port(env, "DEV_CLUSTER_AUTHORITY_PORT", 8081),
        yuki_node_api_port=_env_port(env, "DEV_CLUSTER_YUKI_NODE_API_PORT", 8082),
        erin_node_api_port=_env_port(env, "DEV_CLUSTER_ERIN_NODE_API_PORT", 8083),
    )
    return ClusterSettings(
        env_file=env_file,
        ports=ports,
        authority_token=_required_env_value(
            env,
            names=("DATA_AUTHORITY_TOKEN",),
            label="The shared authority token",
        ),
        yuki_token=_required_env_value(
            env,
            names=("YUKI_BOT_TOKEN", "BOT_TOKEN"),
            label="The Yuki bot token",
        ),
        erin_token=_required_env_value(
            env,
            names=("ERIN_BOT_TOKEN", "BOT_TOKEN_ERIN"),
            label="The Erin bot token",
        ),
        yuki_started_channel=_optional_env_value(env, "YUKI_STARTED_CHANNEL", "STARTED_CHANNEL"),
        erin_started_channel=_optional_env_value(env, "ERIN_STARTED_CHANNEL"),
    )


def build_process_environment(
    *,
    base_env: Mapping[str, str],
    settings: ClusterSettings,
    member: ClusterMember,
) -> dict[str, str]:
    env = dict(base_env)
    env["DEV_CLUSTER_NODE_LINKS_JSON"] = json.dumps(
        [
            {
                "node_name": ClusterMember.YUKI.value,
                "label": "Yuki",
                "node_api_public_base_url": settings.ports.node_api_base_url(ClusterMember.YUKI),
            },
            {
                "node_name": ClusterMember.ERIN.value,
                "label": "Erin",
                "node_api_public_base_url": settings.ports.node_api_base_url(ClusterMember.ERIN),
            },
        ]
    )
    env["BOT_PROFILE"] = member.value
    env["NODE_NAME"] = member.value
    env["INDEV"] = "true"
    env["PUBLIC_BASE_URL"] = settings.ports.portal_base_url
    env["MOD_WEB_PUBLIC_BASE_URL"] = settings.ports.portal_base_url
    env["MOD_WEB_BIND_HOST"] = settings.ports.bind_host
    env["MOD_WEB_PORT"] = str(settings.ports.portal_port)
    env["DATA_AUTHORITY_HOST"] = settings.ports.authority_base_url
    env["DATA_AUTHORITY_PORT"] = ""
    env["DATA_AUTHORITY_TOKEN"] = settings.authority_token

    if member is ClusterMember.YUKI:
        env["BOT_TOKEN"] = settings.yuki_token
        env["NODE_API_BIND_HOST"] = settings.ports.bind_host
        env["NODE_API_PORT"] = str(settings.ports.yuki_node_api_port)
        env["NODE_API_PUBLIC_BASE_URL"] = settings.ports.node_api_base_url(member)
        env["DATA_AUTHORITY_BIND_HOST"] = settings.ports.bind_host
        env["DATA_AUTHORITY_BIND_PORT"] = str(settings.ports.authority_port)
        env["STARTED_CHANNEL"] = settings.yuki_started_channel or ""
        return env

    env["DATA_AUTHORITY_BIND_HOST"] = ""
    env["DATA_AUTHORITY_BIND_PORT"] = ""
    env["STARTED_CHANNEL"] = settings.erin_started_channel or ""

    if member is ClusterMember.ERIN:
        env["BOT_TOKEN"] = settings.erin_token
        env["NODE_API_BIND_HOST"] = settings.ports.bind_host
        env["NODE_API_PORT"] = str(settings.ports.erin_node_api_port)
        env["NODE_API_PUBLIC_BASE_URL"] = settings.ports.node_api_base_url(member)
        return env

    env["BOT_TOKEN"] = ""
    env["NODE_API_BIND_HOST"] = ""
    env["NODE_API_PORT"] = ""
    env["NODE_API_PUBLIC_BASE_URL"] = ""
    return env


class _ConsolePrinter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def line(self, text: str) -> None:
        with self._lock:
            print(text, flush=True)

    def process_line(self, member: ClusterMember, line: str) -> None:
        label = member.value.upper().ljust(6)
        self.line(f"[{label}] {line}")


class DevClusterManager:
    def __init__(
        self,
        *,
        base_env: Mapping[str, str],
        settings: ClusterSettings,
        command: tuple[str, ...] | None = None,
        cwd: Path | None = None,
        printer: _ConsolePrinter | None = None,
    ) -> None:
        self._base_env = dict(base_env)
        self._settings = settings
        self._command = command or (sys.executable, str(Path(__file__).with_name("main.py")))
        self._cwd = cwd or Path(__file__).resolve().parent
        self._printer = printer or _ConsolePrinter()
        self._processes: dict[ClusterMember, RunningProcess] = {}

    def start(self, member: ClusterMember) -> None:
        self._cleanup_member_record(member)
        current = self._processes.get(member)
        if current is not None and current.process.poll() is None:
            self._printer.line(f"{member.value} is already running (pid {current.process.pid}).")
            return

        env = build_process_environment(base_env=self._base_env, settings=self._settings, member=member)
        process = subprocess.Popen(  # noqa: S603
            list(self._command),
            cwd=self._cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        output_thread = threading.Thread(
            target=self._stream_output,
            args=(member, process),
            name=f"dev-cluster-{member.value}-stdout",
            daemon=True,
        )
        output_thread.start()
        self._processes[member] = RunningProcess(member=member, env=env, process=process, output_thread=output_thread)
        self._write_process_record(ClusterProcessRecord(member=member, pid=process.pid))
        self._printer.line(f"Started {member.value} (pid {process.pid}).")

    def stop(self, member: ClusterMember) -> None:
        running = self._processes.get(member)
        if running is None or running.process.poll() is not None:
            if self._stop_recorded_member(member):
                self._processes.pop(member, None)
                return
            self._printer.line(f"{member.value} is not running.")
            return

        process = running.process
        self._printer.line(f"Stopping {member.value} (pid {process.pid}).")
        self._terminate_process_group(member=member, pid=process.pid)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._printer.line(f"{member.value} process {process.pid} did not exit after group termination.")
        running.output_thread.join(timeout=1.0)
        self._clear_process_record(member, expected_pid=process.pid)
        self._processes.pop(member, None)

    def restart(self, member: ClusterMember) -> None:
        self.stop(member)
        self.start(member)

    def start_all(self) -> None:
        for member in ClusterMember:
            self.start(member)

    def stop_all(self) -> None:
        for member in reversed(tuple(ClusterMember)):
            self.stop(member)

    def status_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for member in ClusterMember:
            running = self._processes.get(member)
            if running is None:
                lines.append(f"{member.value:<6} stopped")
                continue
            return_code = running.process.poll()
            if return_code is None:
                lines.append(f"{member.value:<6} running pid={running.process.pid}")
            else:
                lines.append(f"{member.value:<6} exited code={return_code}")
        return tuple(lines)

    def _stream_output(self, member: ClusterMember, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            self._printer.process_line(member, raw_line.rstrip())
        process.stdout.close()
        return_code = process.wait()
        self._clear_process_record(member, expected_pid=process.pid)
        self._printer.line(f"[{member.value.upper().ljust(6)}] exited with code {return_code}")

    def _record_path(self, member: ClusterMember) -> Path:
        return _STATE_DIRECTORY / f"{member.value}.json"

    def _load_process_record(self, member: ClusterMember) -> ClusterProcessRecord | None:
        path = self._record_path(member)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = ClusterProcessRecord.from_json(payload)
        except (OSError, ValueError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        if record.member is not member:
            path.unlink(missing_ok=True)
            return None
        return record

    def _write_process_record(self, record: ClusterProcessRecord) -> None:
        _STATE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        self._record_path(record.member).write_text(
            json.dumps(record.to_json(), sort_keys=True),
            encoding="utf-8",
        )

    def _clear_process_record(self, member: ClusterMember, *, expected_pid: int | None = None) -> None:
        path = self._record_path(member)
        if expected_pid is not None:
            record = self._load_process_record(member)
            if record is None or record.pid != expected_pid:
                return
        path.unlink(missing_ok=True)

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate_process_group(self, *, member: ClusterMember, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = monotonic() + _STOP_TIMEOUT_SECONDS
        while monotonic() < deadline:
            if not self._pid_exists(pid):
                return
            sleep(0.1)
        self._printer.line(f"{member.value} did not exit in time; killing it.")
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def _stop_recorded_member(self, member: ClusterMember) -> bool:
        record = self._load_process_record(member)
        if record is None:
            return False
        if not self._pid_exists(record.pid):
            self._clear_process_record(member, expected_pid=record.pid)
            return False
        self._printer.line(f"Stopping stale {member.value} process group (pid {record.pid}).")
        self._terminate_process_group(member=member, pid=record.pid)
        self._clear_process_record(member, expected_pid=record.pid)
        return True

    def _cleanup_member_record(self, member: ClusterMember) -> None:
        record = self._load_process_record(member)
        if record is None:
            return
        tracked = self._processes.get(member)
        if tracked is not None and tracked.process.poll() is None and tracked.process.pid == record.pid:
            return
        if not self._pid_exists(record.pid):
            self._clear_process_record(member, expected_pid=record.pid)
            return
        self._printer.line(f"Found stale {member.value} process from an earlier run (pid {record.pid}); stopping it.")
        self._terminate_process_group(member=member, pid=record.pid)
        self._clear_process_record(member, expected_pid=record.pid)


def _parse_command_target(token: str) -> tuple[ClusterMember, ...]:
    if token == "all":
        return tuple(ClusterMember)
    try:
        return (ClusterMember(token),)
    except ValueError as xcp:
        expected = ", ".join(member.value for member in ClusterMember)
        raise ValueError(f"Unknown target {token!r}. Expected one of: {expected}, all.") from xcp


def _print_banner(printer: _ConsolePrinter, settings: ClusterSettings) -> None:
    printer.line("=" * _TERMINAL_WIDTH)
    printer.line("Yukibot Dev Cluster")
    printer.line(f"env file: {settings.env_file}")
    printer.line(f"portal: {settings.ports.portal_base_url}")
    printer.line(f"authority: {settings.ports.authority_base_url}")
    printer.line(f"node api: yuki={settings.ports.node_api_base_url(ClusterMember.YUKI)}")
    printer.line(f"node api: erin={settings.ports.node_api_base_url(ClusterMember.ERIN)}")
    printer.line("commands: status | start <name|all> | stop <name|all> | restart <name|all> | quit")
    printer.line("=" * _TERMINAL_WIDTH)


def _install_signal_handlers(manager: DevClusterManager, printer: _ConsolePrinter) -> None:
    handled_signals: list[signal.Signals] = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    shutting_down = False

    def _handle_signal(signum: int, _frame: object | None) -> None:
        nonlocal shutting_down
        if shutting_down:
            raise SystemExit(128 + signum)
        shutting_down = True
        signal_name = signal.Signals(signum).name
        printer.line(f"{signal_name} received; stopping all processes.")
        manager.stop_all()
        raise SystemExit(128 + signum)

    for signum in handled_signals:
        signal.signal(signum, _handle_signal)


def _run_repl(manager: DevClusterManager, printer: _ConsolePrinter) -> int:
    while True:
        try:
            command_text = input("dev-cluster> ").strip()
        except EOFError:
            printer.line("EOF received; stopping all processes.")
            manager.stop_all()
            return 0
        except KeyboardInterrupt:
            printer.line("")
            printer.line("Interrupt received; stopping all processes.")
            manager.stop_all()
            return 130

        if not command_text:
            continue

        parts = shlex.split(command_text)
        action = parts[0].casefold()
        try:
            if action in {"quit", "exit"}:
                manager.stop_all()
                return 0
            if action == "status":
                for line in manager.status_lines():
                    printer.line(line)
                continue
            if action in {"start", "stop", "restart"}:
                if len(parts) != 2:
                    raise ValueError(f"{action} requires a target.")
                for target in _parse_command_target(parts[1].casefold()):
                    if action == "start":
                        manager.start(target)
                    elif action == "stop":
                        manager.stop(target)
                    else:
                        manager.restart(target)
                continue
            raise ValueError(f"Unknown command {action!r}.")
        except ValueError as xcp:
            printer.line(f"Error: {xcp}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Yuki, Erin, and Portal as separate local dev processes.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file used to seed launcher settings. Defaults to .env.",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Open the control shell without starting processes immediately.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env_file = Path(args.env_file).resolve()
    base_env = merged_environment(env_file=env_file)
    settings = settings_from_environment(env=base_env, env_file=env_file)
    printer = _ConsolePrinter()
    _print_banner(printer, settings)
    manager = DevClusterManager(base_env=base_env, settings=settings, printer=printer)
    _install_signal_handlers(manager, printer)
    if not args.no_start:
        manager.start_all()
    return _run_repl(manager, printer)


if __name__ == "__main__":
    raise SystemExit(main())
