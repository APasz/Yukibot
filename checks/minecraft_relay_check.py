from __future__ import annotations

import unittest
from typing import Any, cast
from unittest.mock import patch

from _discord import App_Bound
from apps._app import AM_Receiver
from apps.minecraft import Matchers, Minecraft


class _DummyReceiver(AM_Receiver):
    async def send(self, payload: App_Bound) -> None:
        return None


class MinecraftRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_match_chat_ignores_relay_notice_prefix(self) -> None:
        app = cast(Any, object.__new__(Minecraft))
        app.cfg = type("Cfg", (), {"chat_ignore_symbol": "!"})()
        app._tail_machers = set()
        matcher = Matchers(app)

        with patch("apps.minecraft.DC_Relay.add") as add_mock:
            await matcher.match_chat("[12:00:00] [Server thread/INFO]: <System> !relay notice: unresolved player")

        add_mock.assert_not_called()

    def test_minecraft_supports_relay_system_notices(self) -> None:
        app = object.__new__(Minecraft)
        app.am_receiver = _DummyReceiver()
        app.chat_relay_outbound = True

        self.assertTrue(app.supports_relay_system_notices)


if __name__ == "__main__":
    unittest.main()
