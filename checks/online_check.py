from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import hikari

from online import ActivityChange, Online_Tracker, PresenceSnapshot


def _presence(
    *,
    user_id: int,
    status: str,
    desktop: str = "offline",
    mobile: str = "offline",
    web: str = "offline",
    activities: list[object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=hikari.Snowflake(user_id),
        visible_status=status,
        client_status=SimpleNamespace(desktop=desktop, mobile=mobile, web=web),
        activities=list(activities or []),
    )


class OnlineTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_observed_transition_is_flushed_after_ready_delay(self) -> None:
        with TemporaryDirectory() as tmp:
            tracker = Online_Tracker(Path(tmp) / "online_watch.json")
            watcher_id = hikari.Snowflake(100)
            target_id = hikari.Snowflake(200)
            tracker.ensure_rule(watcher_id, target_id)
            tracker._notify = AsyncMock()
            tracker.ready_at = datetime.now(timezone.utc) + timedelta(milliseconds=40)

            event = SimpleNamespace(
                presence=_presence(user_id=int(target_id), status="online", desktop="online"),
                old_presence=_presence(user_id=int(target_id), status="offline"),
            )

            await tracker.on_presence_update(
                cast(hikari.PresenceUpdateEvent, event),
                cast(hikari.GatewayBot, SimpleNamespace()),
                None,
            )
            await asyncio.sleep(0.08)

            tracker._notify.assert_awaited_once()
            await_args = tracker._notify.await_args
            self.assertIsNotNone(await_args)
            assert await_args is not None
            args = await_args.args
            kwargs = await_args.kwargs
            self.assertEqual(args[1], watcher_id)
            self.assertEqual(args[2], [f"🟢 🖥️ <@{int(target_id)}> online-desktop"])
            self.assertTrue(kwargs["silent"])

    def test_ignored_games_do_not_suppress_unrelated_stop_events(self) -> None:
        with TemporaryDirectory() as tmp:
            tracker = Online_Tracker(Path(tmp) / "online_watch.json")
            user_id = hikari.Snowflake(200)
            snapshot = PresenceSnapshot(
                status="online",
                platforms={"desktop": "online"},
                activities={},
                game_starts={},
                ignored_games={"wordle"},
                ignored_activities={"wordle"},
            )

            changes = [ActivityChange("stopped", "games", "Portal 2")]

            stable = tracker._stabilise_activity_changes(user_id, snapshot, changes)

            self.assertEqual(stable, changes)
            self.assertNotIn(user_id, tracker._suppressed_game_stops)


if __name__ == "__main__":
    unittest.main()
