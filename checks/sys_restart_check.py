from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import hikari

import _sys


class ScheduledRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_restart_suppresses_notifications_when_requested(self) -> None:
        bot = SimpleNamespace(
            update_presence=AsyncMock(),
            rest=SimpleNamespace(create_message=AsyncMock(return_value=SimpleNamespace(id=42))),
        )

        with (
            patch("_sys._prepare_restart", new=AsyncMock()) as prepare_restart,
            patch("_sys._finish_restart", new=AsyncMock()) as finish_restart,
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

        bot.rest.create_message.assert_awaited_once_with(
            1234,
            "Scheduled maintenance restarting `bot` at `04:30`.",
            flags=hikari.UNDEFINED,
        )


if __name__ == "__main__":
    unittest.main()
