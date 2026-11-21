from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _gateway_bot(*, shard_connections: tuple[bool, ...] = (True,)) -> SimpleNamespace:
    return SimpleNamespace(
        shards={index: SimpleNamespace(is_connected=connected) for index, connected in enumerate(shard_connections)},
        update_presence=AsyncMock(),
    )


class ActivityManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_update_sets_presence_without_throttle_delay(self) -> None:
        bot = _gateway_bot()
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
        bot = _gateway_bot()
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

    async def test_offline_shards_defer_presence_without_consuming_the_refresh_interval(self) -> None:
        for connections in ((), (False,), (True, False)):
            with self.subTest(shard_connections=connections):
                bot = _gateway_bot(shard_connections=connections)
                manager = Activity_Manager(
                    bot,
                    [_StaticProvider("Ready")],
                    activity_settings=config.DiscordActivitySettings(),
                )  # type: ignore[arg-type]

                await manager.update()

                bot.update_presence.assert_not_awaited()
                self.assertIsNone(manager.last_update)
                self.assertIsNone(manager.state)
                self.assertEqual(manager.current_rotation_slot(1), (0, False))

                if not bot.shards:
                    bot.shards[0] = SimpleNamespace(is_connected=True)
                for shard in bot.shards.values():
                    shard.is_connected = True
                await manager.update()

                bot.update_presence.assert_awaited_once()
                self.assertEqual(manager.state, "Ready")

    async def test_reconnect_sends_the_latest_presence(self) -> None:
        bot = _gateway_bot()
        provider = _StaticProvider("Before disconnect")
        manager = Activity_Manager(
            bot,
            [provider],
            activity_settings=config.DiscordActivitySettings(),
        )  # type: ignore[arg-type]
        await manager.update()
        bot.shards[0].is_connected = False
        provider._status = "While disconnected"

        await manager.refresh()

        self.assertEqual(manager.state, "Before disconnect")
        self.assertEqual(bot.update_presence.await_count, 1)
        provider._status = "After reconnect"
        bot.shards[0].is_connected = True
        await manager.update()

        self.assertEqual(manager.state, "After reconnect")
        self.assertEqual(bot.update_presence.await_count, 2)
        self.assertEqual(bot.update_presence.await_args.kwargs["activity"].name, "After reconnect")

    async def test_presence_send_failures_retry_the_same_status_after_the_refresh_interval(self) -> None:
        for error in (
            hikari.ComponentStateConflictError("shard 0 is not connected"),
            RuntimeError("send failed"),
        ):
            with self.subTest(error_type=type(error).__name__):
                bot = _gateway_bot()
                provider = _StaticProvider("Before failure")
                manager = Activity_Manager(
                    bot,
                    [provider],
                    activity_settings=config.DiscordActivitySettings(),
                )  # type: ignore[arg-type]
                now = datetime(2026, 11, 21, tzinfo=timezone.utc)
                with patch("_activity.datetime") as clock, patch("_activity.log.exception") as log_exception:
                    clock.now.return_value = now
                    await manager.update()
                    provider._status = "Retry this status"
                    bot.update_presence.side_effect = [error, None]

                    await manager.refresh()

                    self.assertEqual(manager.state, "Before failure")
                    self.assertEqual(bot.update_presence.await_count, 2)
                    if isinstance(error, hikari.ComponentStateConflictError):
                        log_exception.assert_not_called()
                    else:
                        log_exception.assert_called_once()
                    await manager.update()
                    self.assertEqual(bot.update_presence.await_count, 2)

                    clock.now.return_value = now + timedelta(seconds=manager.refresh_interval_seconds)
                    await manager.update()

                    self.assertEqual(manager.state, "Retry this status")
                    self.assertEqual(bot.update_presence.await_count, 3)
                    self.assertEqual(bot.update_presence.await_args.kwargs["activity"].name, "Retry this status")

    async def test_custom_activity_settings_control_field_order_and_fallback(self) -> None:
        bot = _gateway_bot()
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
        bot = _gateway_bot()
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
            _gateway_bot(),
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
