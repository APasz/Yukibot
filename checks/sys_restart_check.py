from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import hikari

import _sys
from restart_state import RestartKind


class ScheduledRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_restart_suppresses_notifications_when_requested(self) -> None:
        bot = SimpleNamespace(
            update_presence=AsyncMock(),
            rest=SimpleNamespace(create_message=AsyncMock(return_value=SimpleNamespace(id=42))),
        )

        with (
            patch("_sys._prepare_restart", new=AsyncMock()) as prepare_restart,
            patch("_sys._finish_restart", new=AsyncMock()) as finish_restart,
            patch("_sys.mark_pending_process_restart") as mark_restart,
            patch("_sys.Path.write_text", new=Mock()),
            patch("_sys.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        ):
            await _sys.scheduled_restart(
                bot=bot,
                manager=object(),
                restart_type="system",
                reason="Scheduled maintenance restarting `system` at `04:30`.",
                message_channel_id=1234,
                suppress_notifications=True,
            )

        mark_restart.assert_called_once_with(RestartKind.SCHEDULED_SYS)
        prepare_restart.assert_awaited_once()
        finish_restart.assert_awaited_once_with(restart_sys=True, ctx=None)
        bot.rest.create_message.assert_awaited_once_with(
            1234,
            "Scheduled maintenance restarting `system` at `04:30`.",
            flags=hikari.MessageFlag.SUPPRESS_NOTIFICATIONS,
        )
        sleep_mock.assert_awaited_once_with(0.1)

    async def test_scheduled_restart_leaves_notifications_enabled_by_default(self) -> None:
        bot = SimpleNamespace(
            update_presence=AsyncMock(),
            rest=SimpleNamespace(create_message=AsyncMock(return_value=SimpleNamespace(id=43))),
        )

        with (
            patch("_sys._prepare_restart", new=AsyncMock()),
            patch("_sys._finish_restart", new=AsyncMock()),
            patch("_sys.mark_pending_process_restart") as mark_restart,
            patch("_sys.Path.write_text", new=Mock()),
            patch("_sys.asyncio.sleep", new=AsyncMock()),
        ):
            await _sys.scheduled_restart(
                bot=bot,
                manager=object(),
                restart_type="bot",
                reason="Scheduled maintenance restarting `bot` at `04:30`.",
                message_channel_id=1234,
            )

        mark_restart.assert_called_once_with(RestartKind.SCHEDULED_BOT)
        bot.rest.create_message.assert_awaited_once_with(
            1234,
            "Scheduled maintenance restarting `bot` at `04:30`.",
            flags=hikari.UNDEFINED,
        )

    async def test_scheduled_restart_writes_silent_sentinel_when_requested(self) -> None:
        bot = SimpleNamespace(
            update_presence=AsyncMock(),
            rest=SimpleNamespace(create_message=AsyncMock()),
        )

        with (
            patch("_sys._prepare_restart", new=AsyncMock()),
            patch("_sys._finish_restart", new=AsyncMock()) as finish_restart,
            patch("_sys.mark_pending_process_restart") as mark_restart,
            patch("_sys.Path") as path_cls,
        ):
            await _sys.scheduled_restart(
                bot=bot,
                manager=object(),
                restart_type="bot",
                reason="Web dashboard requested restart_process.",
                message_channel_id=None,
                scheduled=False,
                silent=True,
            )

        mark_restart.assert_called_once_with(RestartKind.MANUAL_BOT)
        path_cls.assert_called_once_with("silent_restart")
        path_cls.return_value.touch.assert_called_once_with()
        finish_restart.assert_awaited_once_with(restart_sys=False, ctx=None)
        bot.rest.create_message.assert_not_called()

    async def test_scheduled_restart_proceeds_when_discord_requests_fail(self) -> None:
        bot = SimpleNamespace(
            update_presence=AsyncMock(side_effect=RuntimeError("Discord API returned 503")),
            rest=SimpleNamespace(create_message=AsyncMock(side_effect=RuntimeError("Discord API returned 503"))),
        )

        with (
            patch("_sys._prepare_restart", new=AsyncMock()),
            patch("_sys._finish_restart", new=AsyncMock()) as finish_restart,
            patch("_sys.mark_pending_process_restart"),
        ):
            await _sys.scheduled_restart(
                bot=bot,
                manager=object(),
                restart_type="bot",
                reason="Scheduled maintenance restarting `bot` at `04:30`.",
                message_channel_id=1234,
            )

        finish_restart.assert_awaited_once_with(restart_sys=False, ctx=None)


if __name__ == "__main__":
    unittest.main()
