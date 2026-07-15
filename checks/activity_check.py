from __future__ import annotations

from datetime import datetime, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hikari

import config
from _activity import Activity_Manager


class _StaticProvider:
    def __init__(
        self,
        status: str | None,
        *,
        prio: int = 0,
        activity_field: config.DiscordActivityField | None = None,
        activity_scope_name: str | None = None,
    ) -> None:
        self._status = status
        self.prio = prio
        self.activity_field = activity_field
        self.activity_scope_name = activity_scope_name

    async def get(self) -> str | None:
        return self._status


class _CpuProvider(_StaticProvider):
    pass


class _PlayerProvider(_StaticProvider):
    pass


class ActivityManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_update_sets_presence_without_throttle_delay(self) -> None:
        bot = SimpleNamespace(update_presence=AsyncMock())
        manager = Activity_Manager(
            bot,
            [_StaticProvider("Ready")],
            activity_settings=config.DiscordActivitySettings(),
        )  # type: ignore[arg-type]

        await manager.update()

        self.assertIsNotNone(manager.last_update)
        self.assertEqual(manager.state, "Ready")
        bot.update_presence.assert_awaited_once()
        sent_activity = bot.update_presence.await_args.kwargs["activity"]
        self.assertIsInstance(sent_activity, hikari.Activity)
        self.assertEqual(sent_activity.name, "Ready")

    async def test_second_immediate_update_is_throttled(self) -> None:
        bot = SimpleNamespace(update_presence=AsyncMock())
        manager = Activity_Manager(
            bot,
            [_StaticProvider("Ready")],
            activity_settings=config.DiscordActivitySettings(),
        )  # type: ignore[arg-type]

        await manager.update()

        with patch.object(manager, "last_update", datetime.now(tz=timezone.utc)):
            await manager.update()

        self.assertEqual(bot.update_presence.await_count, 1)
        self.assertEqual(manager.state, "Ready")

    async def test_custom_activity_settings_control_field_order_and_fallback(self) -> None:
        bot = SimpleNamespace(update_presence=AsyncMock())
        manager = Activity_Manager(
            bot,
            [
                _CpuProvider("42%", activity_field=config.DiscordActivityField.CPU),
                _PlayerProvider("7/20", activity_field=config.DiscordActivityField.PLAYERS),
            ],
            activity_settings=config.DiscordActivitySettings(
                prefix="[",
                separator=" :: ",
                suffix="]",
                fallback_text="Idle",
                fields=(
                    config.DiscordActivityField.PLAYERS,
                    config.DiscordActivityField.CPU,
                ),
            ),
        )  # type: ignore[arg-type]

        await manager.update()

        sent_activity = bot.update_presence.await_args.kwargs["activity"]
        self.assertEqual(sent_activity.name, "[7/20 :: 42%]")

        manager.set_activity_settings(
            config.DiscordActivitySettings(
                fallback_text="Idle",
                fields=(config.DiscordActivityField.APP,),
            )
        )
        await manager.refresh()

        self.assertEqual(manager.state, "Idle")

    async def test_scoped_provider_only_contributes_for_matching_rotation_target(self) -> None:
        bot = SimpleNamespace(update_presence=AsyncMock())
        manager = Activity_Manager(
            bot,
            [
                _PlayerProvider("3/10", activity_field=config.DiscordActivityField.PLAYERS),
                _StaticProvider("D42", prio=10, activity_scope_name="minecraft_alpha"),
            ],
            activity_settings=config.DiscordActivitySettings(
                separator=" :: ",
                fields=(config.DiscordActivityField.PLAYERS,),
            ),
        )  # type: ignore[arg-type]
        manager.set_rotation_target_name_provider(lambda: "satisfactory_main")

        await manager.update()

        sent_activity = bot.update_presence.await_args.kwargs["activity"]
        self.assertEqual(sent_activity.name, "3/10")

        manager.set_rotation_target_name_provider(lambda: "minecraft_alpha")
        await manager.refresh()

        sent_activity = bot.update_presence.await_args.kwargs["activity"]
        self.assertEqual(sent_activity.name, "3/10 :: D42")

    def test_current_rotation_slot_uses_units_and_alt_text_percentage(self) -> None:
        manager = Activity_Manager(
            SimpleNamespace(update_presence=AsyncMock()),
            [],
            activity_settings=config.DiscordActivitySettings(
                units_per_app=4,
                alt_text_percentage=50,
            ),
        )  # type: ignore[arg-type]

        slots = []
        for _ in range(8):
            slots.append(manager.current_rotation_slot(2))
            manager._rotation_unit_index += 1

        self.assertEqual(
            slots,
            [
                (0, False),
                (0, False),
                (0, True),
                (0, True),
                (1, False),
                (1, False),
                (1, True),
                (1, True),
            ],
        )


if __name__ == "__main__":
    unittest.main()
