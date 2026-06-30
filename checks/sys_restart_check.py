from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import hikari

import _sys
import config
from node_auth import NodeAccessGrant, NodeApiScope


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


class PortalRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_portal_requests_authenticated_process_exit(self) -> None:
        ctx = SimpleNamespace(user=SimpleNamespace(id=1234), respond=AsyncMock())
        response = Mock()
        portal_config = replace(
            config.MOD_WEB_SERVER,
            public_base_url="https://portal.example",
            token_secret="shared-secret",
        )

        with (
            patch.object(config, "MOD_WEB_SERVER", portal_config),
            patch("_sys.time.time", return_value=1000),
            patch("_sys.issue_node_token", return_value="restart-token") as issue_token_mock,
            patch("_sys.requests.post", return_value=response) as post_mock,
        ):
            await _sys.restart_portal(ctx, silent=False)

        issue_token_mock.assert_called_once_with(
            secret="shared-secret",
            grant=NodeAccessGrant(
                subject="web:1234",
                node="portal",
                app=None,
                scopes=frozenset({NodeApiScope.NODE_MANAGE}),
                expires_at=1060,
            ),
        )
        post_mock.assert_called_once_with(
            "https://portal.example/api/node/restart",
            headers={"Authorization": "Bearer restart-token"},
            timeout=10,
        )
        response.raise_for_status.assert_called_once_with()
        ctx.respond.assert_awaited_once_with("Portal restarting.", flags=hikari.UNDEFINED)

    async def test_restart_portal_reports_request_failure(self) -> None:
        ctx = SimpleNamespace(user=SimpleNamespace(id=1234), respond=AsyncMock())
        portal_config = replace(config.MOD_WEB_SERVER, token_secret="shared-secret")

        with (
            patch.object(config, "MOD_WEB_SERVER", portal_config),
            patch("_sys.issue_node_token", return_value="restart-token"),
            patch("_sys.requests.post", side_effect=_sys.requests.ConnectionError("portal unavailable")),
        ):
            await _sys.restart_portal(ctx, silent=False)

        ctx.respond.assert_awaited_once_with("Unable to restart Portal.", flags=hikari.MessageFlag.EPHEMERAL)


if __name__ == "__main__":
    unittest.main()
