from __future__ import annotations

import asyncio
import threading
import unittest
from typing import Any, cast

from apps._rcon import RconClient


class _FakeAsyncRcon:
    def __init__(self) -> None:
        self.active_commands = 0
        self.max_active_commands = 0
        self.commands: list[str] = []
        self.command_loops: list[asyncio.AbstractEventLoop] = []

    async def send_command(self, command: str) -> str:
        self.commands.append(command)
        self.command_loops.append(asyncio.get_running_loop())
        self.active_commands += 1
        self.max_active_commands = max(self.max_active_commands, self.active_commands)
        await asyncio.sleep(0)
        self.active_commands -= 1
        return command

    async def close(self) -> None:
        return None


class RconClientTests(unittest.IsolatedAsyncioTestCase):
    def _start_worker_loop(self) -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        thread = threading.Thread(target=_run_loop, name="rcon-worker-loop", daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=2))

        def _cleanup() -> None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.addCleanup(_cleanup)
        return loop, thread

    async def test_send_serialises_concurrent_commands(self) -> None:
        relay = cast(RconClient, object.__new__(RconClient))
        relay.app_alive = lambda: True
        relay._host = "localhost"
        relay._port = 25575
        relay._password_env = "APP_COMM_PASS"
        relay._label = "minecraft:test"
        relay._password = "secret"
        relay._max_attempts = 30
        relay._rcon = cast(Any, _FakeAsyncRcon())
        relay._running = True
        relay._connected = True
        relay._command_lock = asyncio.Lock()

        results = await asyncio.gather(
            relay.send("list"),
            relay.send("time query gametime"),
        )

        self.assertEqual(results, ["list", "time query gametime"])
        fake_rcon = cast(_FakeAsyncRcon, relay._rcon)
        self.assertEqual(fake_rcon.commands, ["list", "time query gametime"])
        self.assertEqual(fake_rcon.max_active_commands, 1)

    async def test_send_routes_cross_loop_commands_to_owner_loop(self) -> None:
        owner_loop, _ = self._start_worker_loop()
        relay = cast(RconClient, object.__new__(RconClient))
        relay.app_alive = lambda: True
        relay._host = "localhost"
        relay._port = 25575
        relay._password_env = "APP_COMM_PASS"
        relay._label = "minecraft:test"
        relay._password = "secret"
        relay._max_attempts = 30
        relay._rcon = cast(Any, _FakeAsyncRcon())
        relay._running = True
        relay._connected = True
        relay._command_lock = asyncio.Lock()
        relay._owner_loop = owner_loop

        result = await relay.send("list")

        self.assertEqual(result, "list")
        fake_rcon = cast(_FakeAsyncRcon, relay._rcon)
        self.assertEqual(fake_rcon.commands, ["list"])
        self.assertEqual(fake_rcon.command_loops, [owner_loop])


if __name__ == "__main__":
    unittest.main()
