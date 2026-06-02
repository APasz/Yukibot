from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    manager.set_current_restart_auto_start_app = Mock(return_value="minecraft_alpha")
    manager.set_restart_auto_start_app = Mock()

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
    manager.set_current_restart_auto_start_app.assert_called_once_with()
    manager.set_restart_auto_start_app.assert_not_called()
    ctx.defer.assert_awaited_once_with()
    restart_mock.assert_awaited_once_with(ctx, bot, manager, RestartTarget.BOT.value, False)


@pytest.mark.anyio
async def test_restart_host_or_bot_clears_auto_restart_state_by_default() -> None:
    ctx = _restart_context()
    acl = SimpleNamespace(perm_check=AsyncMock())
    bot = Mock()
    manager = Mock()
    manager.set_current_restart_auto_start_app = Mock()
    manager.set_restart_auto_start_app = Mock(return_value=None)

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
    manager.set_current_restart_auto_start_app.assert_not_called()
    manager.set_restart_auto_start_app.assert_called_once_with(None)
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
