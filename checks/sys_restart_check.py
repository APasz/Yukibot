from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import hikari

import _sys
import config
from node_auth import NodeAccessGrant, NodeApiScope
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


class PortalRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_portal_requests_authenticated_process_exit(self) -> None:
        ctx = SimpleNamespace(user=SimpleNamespace(id=1234), respond=AsyncMock())
        response = Mock()
        response.ok = True
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
            json={"restart_kind": "manual_bot"},
            headers={"Authorization": "Bearer restart-token"},
            timeout=10,
        )
        response.raise_for_status.assert_not_called()
        ctx.respond.assert_awaited_once_with("Portal restarting.", flags=hikari.UNDEFINED)

    async def test_restart_portal_uses_portal_registry_node_name(self) -> None:
        ctx = SimpleNamespace(user=SimpleNamespace(id=1234), respond=AsyncMock())
        response = Mock()
        response.ok = True
        portal_config = replace(
            config.MOD_WEB_SERVER,
            public_base_url="https://portal.example",
            token_secret="shared-secret",
        )
        portal_snapshot = config.BotMetadataSnapshot(
            profile=config.BotMetadataProfile(
                id="999",
                label="Portal",
                bot_profile=config.BotProfileName.PORTAL,
            ),
            features=config.BotMetadataFeatures(
                mod_web=config.BotMetadataModWeb(
                    node_name="wakusei",
                    public_base_url="https://portal.example",
                    node_api_base_url="https://portal.example/api/node",
                )
            ),
        )

        with (
            patch.object(config, "MOD_WEB_SERVER", portal_config),
            patch.object(config, "load_known_bot_snapshots", return_value=(portal_snapshot,)),
            patch("_sys.time.time", return_value=1000),
            patch("_sys.issue_node_token", return_value="restart-token") as issue_token_mock,
            patch("_sys.requests.post", return_value=response),
        ):
            await _sys.restart_portal(ctx, silent=False)

        issue_token_mock.assert_called_once_with(
            secret="shared-secret",
            grant=NodeAccessGrant(
                subject="web:1234",
                node="wakusei",
                app=None,
                scopes=frozenset({NodeApiScope.NODE_MANAGE}),
                expires_at=1060,
            ),
        )
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

    def test_request_portal_restart_includes_api_detail_in_http_error(self) -> None:
        response = Mock()
        response.ok = False
        response.text = ""
        response.json.return_value = {"detail": "Node token was issued for a different node."}
        response.raise_for_status.side_effect = _sys.requests.HTTPError(
            "403 Client Error: Forbidden for url: https://portal.example/api/node/restart",
            response=response,
        )

        with (
            patch("_sys.requests.post", return_value=response),
            self.assertRaises(_sys.requests.HTTPError) as raised,
        ):
            _sys._request_portal_restart(
                "https://portal.example/api/node/restart",
                "restart-token",
                RestartKind.MANUAL_BOT,
            )

        self.assertIn("Node token was issued for a different node.", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
