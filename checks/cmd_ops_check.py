from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import lightbulb
import pytest

import cmd_ops
from restart_targets import RestartTarget


def _restart_context() -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=1234, display_name="Tester"),
        defer=AsyncMock(),
    )


@pytest.mark.anyio
async def test_restart_host_or_bot_persists_current_app_when_requested() -> None:
    ctx = _restart_context()
    acl = SimpleNamespace(perm_check=AsyncMock())
    bot = Mock()
    manager = Mock()
    manager.set_running_restart_auto_start_apps = Mock(return_value=("minecraft_alpha",))
    manager.set_restart_auto_start_apps = Mock()

    with patch("cmd_ops._sys.restart", new=AsyncMock()) as restart_mock:
        await cmd_ops.restart_host_or_bot(
            ctx,
            acl,
            bot,
            manager,
            RestartTarget.BOT,
            False,
            True,
        )

    acl.perm_check.assert_awaited_once_with(1234, cmd_ops.restart_required_level(RestartTarget.BOT))
    manager.set_running_restart_auto_start_apps.assert_called_once_with()
    manager.set_restart_auto_start_apps.assert_not_called()
    ctx.defer.assert_awaited_once_with()
    restart_mock.assert_awaited_once_with(ctx, bot, manager, RestartTarget.BOT.value, False)


@pytest.mark.anyio
async def test_restart_host_or_bot_clears_auto_restart_state_by_default() -> None:
    ctx = _restart_context()
    acl = SimpleNamespace(perm_check=AsyncMock())
    bot = Mock()
    manager = Mock()
    manager.set_running_restart_auto_start_apps = Mock()
    manager.set_restart_auto_start_apps = Mock(return_value=())

    with patch("cmd_ops._sys.restart", new=AsyncMock()) as restart_mock:
        await cmd_ops.restart_host_or_bot(
            ctx,
            acl,
            bot,
            manager,
            RestartTarget.SYSTEM,
            True,
            False,
        )

    acl.perm_check.assert_awaited_once_with(1234, cmd_ops.restart_required_level(RestartTarget.SYSTEM))
    manager.set_running_restart_auto_start_apps.assert_not_called()
    manager.set_restart_auto_start_apps.assert_called_once_with(())
    ctx.defer.assert_awaited_once_with()
    restart_mock.assert_awaited_once_with(ctx, bot, manager, RestartTarget.SYSTEM.value, True)


@pytest.mark.anyio
async def test_restart_command_invocation_passes_auto_restart_flag_to_helper() -> None:
    command = object.__new__(cmd_ops.CMD_OpsRestart)
    command.target = RestartTarget.SYSTEM.value
    command.silent = True
    command.auto_restart_running_app = True
    ctx = _restart_context()
    acl = SimpleNamespace(perm_check=AsyncMock())
    bot = Mock()
    manager = Mock()

    with patch("cmd_ops.restart_host_or_bot", new=AsyncMock()) as restart_mock:
        await cmd_ops.CMD_OpsRestart.invoke(command, ctx, acl, bot, manager)

    acl.perm_check.assert_awaited_once_with(1234, cmd_ops.restart_required_level(RestartTarget.SYSTEM))
    restart_mock.assert_awaited_once_with(
        ctx,
        acl,
        bot,
        manager,
        RestartTarget.SYSTEM,
        True,
        True,
    )


class _FakeDiContext:
    def __init__(self, voice_tts: object, music: object | None) -> None:
        self._voice_tts = voice_tts
        self._music = music

    async def __aenter__(self) -> "_FakeDiContext":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    async def get(self, cls: object) -> object:
        if cls is cmd_ops.VoiceTTSService:
            return self._voice_tts
        if cls is cmd_ops.MusicService and self._music is not None:
            return self._music
        raise LookupError(cls)


@pytest.mark.anyio
async def test_reset_voice_runtime_responds_without_runtime_stats() -> None:
    voice_tts = SimpleNamespace(reset_runtime=AsyncMock())
    music = SimpleNamespace(
        active_guild_ids=Mock(return_value=[111, 222]),
        reset_runtime=AsyncMock(),
    )
    di_context = _FakeDiContext(voice_tts=voice_tts, music=music)
    ctx = SimpleNamespace(
        client=SimpleNamespace(
            di=SimpleNamespace(enter_context=Mock(return_value=di_context)),
        ),
        respond=AsyncMock(),
    )

    await cmd_ops.reset_voice_runtime(ctx)

    ctx.client.di.enter_context.assert_called_once_with(lightbulb.di.Contexts.DEFAULT)
    music.active_guild_ids.assert_called_once_with()
    music.reset_runtime.assert_awaited_once_with()
    voice_tts.reset_runtime.assert_awaited_once_with(extra_guild_ids=[111, 222])
    ctx.respond.assert_awaited_once_with("Voice restart complete.")
