from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from remote_node import RemoteNodeSupervisor, build_remote_node_environment


def _remote_node(
    *,
    node_name: str = "erin",
    bot_token: str | None = "remote-token",
    port: int = 3181,
) -> config.RemoteNodeSpec:
    return config.RemoteNodeSpec(
        profile=config.BotProfileName.ERIN,
        node_name=node_name,
        bot_token=bot_token,
        mod_web_host="127.0.0.1",
        mod_web_port=port,
        public_base_url=f"http://127.0.0.1:{port}",
    )


def _remote_config(*, enabled: bool = True, nodes: tuple[config.RemoteNodeSpec, ...] | None = None) -> config.RemoteNodeAutostartConfig:
    return config.RemoteNodeAutostartConfig(
        enabled=enabled,
        path=Path("remote_nodes.json"),
        nodes=nodes or (_remote_node(),),
    )


def test_build_remote_node_environment_overrides_parent_identity_and_ports() -> None:
    env = build_remote_node_environment(
        base_env={
            "BOT_PROFILE": "yuki",
            "BOT_TOKEN": "parent-token",
            "REMOTE_NODE_STARTED_CHANNEL": "123",
        },
        node=_remote_node(),
        authority_url="http://127.0.0.1:8081",
        authority_token="authority-secret",
    )

    assert env["BOT_PROFILE"] == "erin"
    assert env["NODE_NAME"] == "erin"
    assert env["BOT_TOKEN"] == "remote-token"
    assert env["PUBLIC_BASE_URL"] == "http://127.0.0.1:3181"
    assert env["MOD_WEB_PUBLIC_BASE_URL"] == "http://127.0.0.1:3181"
    assert env["MOD_WEB_BIND_HOST"] == "127.0.0.1"
    assert env["MOD_WEB_PORT"] == "3181"
    assert env["DATA_AUTHORITY_HOST"] == "http://127.0.0.1:8081"
    assert env["DATA_AUTHORITY_TOKEN"] == "authority-secret"
    assert env["REMOTE_NODES"] == "false"
    assert env["STARTED_CHANNEL"] == "123"


def test_build_remote_node_environment_requires_remote_bot_token() -> None:
    try:
        build_remote_node_environment(
            base_env={},
            node=_remote_node(bot_token=None),
            authority_url="http://127.0.0.1:8081",
            authority_token="authority-secret",
        )
    except RuntimeError as xcp:
        assert "bot_token" in str(xcp)
    else:
        raise AssertionError("Expected missing remote bot token to fail")


def test_disabled_remote_node_supervisor_is_noop() -> None:
    supervisor = RemoteNodeSupervisor(remote=_remote_config(enabled=False), base_env={}, command=("python", "main.py"))

    assert asyncio.run(supervisor.start()) == 0


def test_remote_node_supervisor_starts_each_configured_node() -> None:
    started: list[dict[str, str]] = []

    class _FakeProcess:
        pid = 42

        def poll(self) -> None:
            return None

    def fake_popen(command: list[str], *, env: dict[str, str], text: bool) -> _FakeProcess:
        del command, text
        started.append(env)
        return _FakeProcess()

    remote = _remote_config(
        nodes=(
            _remote_node(node_name="erin", bot_token="erin-token", port=3181),
            _remote_node(node_name="momo", bot_token="momo-token", port=3182),
        )
    )
    supervisor = RemoteNodeSupervisor(remote=remote, base_env={}, command=("python", "main.py"))

    with (
        patch("remote_node._authority_loopback_url", return_value="http://127.0.0.1:8081"),
        patch("remote_node.config.DATA_AUTHORITY_TOKEN", "authority-secret"),
        patch("remote_node.subprocess.Popen", fake_popen),
    ):
        assert asyncio.run(supervisor.start()) == 2
    assert [env["NODE_NAME"] for env in started] == ["erin", "momo"]
    assert [env["BOT_TOKEN"] for env in started] == ["erin-token", "momo-token"]


def test_load_remote_node_specs_reads_json_file() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "remote_nodes.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "profile": "erin",
                        "node_name": "erin",
                        "bot_token": "remote-token",
                        "mod_web_host": "127.0.0.1",
                        "mod_web_port": 3181,
                        "public_base_url": "http://127.0.0.1:3181",
                    }
                ]
            ),
            encoding="utf-8",
        )

        with patch.object(config, "INDEV", True):
            nodes = config.load_remote_node_specs(path)

    assert len(nodes) == 1
    assert nodes[0].node_name == "erin"
    assert nodes[0].bot_token == "remote-token"


def test_load_remote_node_specs_resolves_bot_token_env() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "remote_nodes.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "profile": "erin",
                        "node_name": "erin",
                        "bot_token_env": "BOT_TOKEN",
                        "mod_web_host": "127.0.0.1",
                        "mod_web_port": 3181,
                        "public_base_url": "http://127.0.0.1:3181",
                    }
                ]
            ),
            encoding="utf-8",
        )

        with patch("config.env_opt", return_value="resolved-token"):
            with patch.object(config, "INDEV", True):
                nodes = config.load_remote_node_specs(path)

    assert len(nodes) == 1
    assert nodes[0].bot_token == "resolved-token"
