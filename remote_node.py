from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path

import config

log = logging.getLogger(__name__)
_REMOTE_NODE_STOP_TIMEOUT_SECONDS = 10


def _authority_loopback_url() -> str:
    binding = config.DATA_AUTHORITY_SERVER_BINDING
    if binding is None:
        raise RuntimeError("Remote node autostart requires a local data authority binding.")
    host = "127.0.0.1" if binding.host in {"0.0.0.0", "::"} else binding.host
    return f"http://{host}:{binding.port}"


def build_remote_node_environment(
    *,
    base_env: Mapping[str, str],
    node: config.RemoteNodeSpec,
    authority_url: str,
    authority_token: str,
) -> dict[str, str]:
    if not node.bot_token:
        raise RuntimeError(f"REMOTE_NODES entry {node.node_name!r} must include bot_token.")
    if not authority_token:
        raise RuntimeError("DATA_AUTHORITY_TOKEN must be set when REMOTE_NODES is configured.")

    env = dict(base_env)
    env["BOT_PROFILE"] = node.profile.value
    env["NODE_NAME"] = node.node_name
    env["BOT_TOKEN"] = node.bot_token
    env["PUBLIC_BASE_URL"] = node.public_base_url
    env["MOD_WEB_PUBLIC_BASE_URL"] = node.public_base_url
    env["MOD_WEB_BIND_HOST"] = node.mod_web_host
    env["MOD_WEB_PORT"] = str(node.mod_web_port)
    env["DATA_AUTHORITY_HOST"] = authority_url
    env["DATA_AUTHORITY_TOKEN"] = authority_token
    env["REMOTE_NODES"] = "false"
    env["STARTED_CHANNEL"] = env.get("REMOTE_NODE_STARTED_CHANNEL", "")
    return env


class RemoteNodeSupervisor:
    def __init__(
        self,
        *,
        remote: config.RemoteNodeAutostartConfig | None = None,
        base_env: Mapping[str, str] | None = None,
        command: Sequence[str] | None = None,
    ) -> None:
        self._remote = remote or config.REMOTE_NODE_AUTOSTART
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._command = tuple(command or (sys.executable, str(Path(__file__).with_name("main.py"))))
        self._processes: dict[str, subprocess.Popen[str]] = {}

    @property
    def enabled(self) -> bool:
        return self._remote.enabled

    async def start(self) -> int:
        if not self.enabled:
            return 0
        if config.DATA_AUTHORITY_MODE is not config.DataAuthorityMode.LOCAL:
            raise RuntimeError("REMOTE_NODES must be launched from the local data authority bot.")

        authority_token = config.DATA_AUTHORITY_TOKEN
        if authority_token is None:
            raise RuntimeError("DATA_AUTHORITY_TOKEN must be set when REMOTE_NODES is configured.")

        started = 0
        authority_url = _authority_loopback_url()
        for node in self._remote.nodes:
            if node.profile is config.ACTIVE_BOT_PROFILE.name:
                raise RuntimeError(f"REMOTE_NODES entry {node.node_name!r} must differ from the active bot profile.")
            process = self._processes.get(node.node_name)
            if process is not None and process.poll() is None:
                continue

            env = build_remote_node_environment(
                base_env=self._base_env,
                node=node,
                authority_url=authority_url,
                authority_token=authority_token,
            )
            log.info(
                "Starting remote node: profile=%s node=%s bind=%s:%s public=%s",
                node.profile.value,
                node.node_name,
                node.mod_web_host,
                node.mod_web_port,
                node.public_base_url,
            )
            self._processes[node.node_name] = subprocess.Popen(  # noqa: S603
                list(self._command),
                env=_clean_env(env),
                text=True,
            )
            started += 1
        return started

    async def stop(self) -> None:
        for node_name, process in tuple(self._processes.items()):
            if process.poll() is not None:
                del self._processes[node_name]
                continue

            log.info("Stopping remote node: node=%s pid=%s", node_name, process.pid)
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, _REMOTE_NODE_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning(
                    "Remote node %s did not stop within %ss; killing it",
                    node_name,
                    _REMOTE_NODE_STOP_TIMEOUT_SECONDS,
                )
                process.kill()
                await asyncio.to_thread(process.wait)
            finally:
                self._processes.pop(node_name, None)


def _clean_env(env: Mapping[str, str]) -> MutableMapping[str, str]:
    return {key: value for key, value in env.items() if value}
