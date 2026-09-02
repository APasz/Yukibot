from __future__ import annotations

import asyncio
import unittest
from collections.abc import Mapping

import hikari

from discord_startup import DiscordClientStartupSupervisor
from node_api.route_contracts import DiscordHealthComponentState, DiscordHealthSnapshot, DiscordServiceState


def _discord_unavailable_error(
    *,
    url: str = "https://discord.test/commands",
    headers: Mapping[str, str] | None = None,
) -> hikari.InternalServerError:
    return hikari.InternalServerError(
        message="Discord API returned 503",
        url=url,
        status=503,
        headers={} if headers is None else headers,
        raw_body=b"",
    )


def _invalid_token_error() -> hikari.UnauthorizedError:
    return hikari.UnauthorizedError(
        message="Bot token is invalid",
        url="https://discord.test/gateway/bot",
        headers={},
        raw_body=b"",
    )


class DiscordClientStartupSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_command_sync_without_failing_local_startup(self) -> None:
        attempts = 0
        states: list[DiscordServiceState] = []
        started = asyncio.Event()

        async def start_client() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _discord_unavailable_error()

        async def on_started() -> None:
            started.set()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            update_state=states.append,
            on_started=on_started,
            initial_retry_delay_seconds=0.001,
            maximum_retry_delay_seconds=0.001,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertFalse(await supervisor.start())
        await asyncio.wait_for(started.wait(), timeout=0.5)

        self.assertEqual(attempts, 2)
        self.assertEqual(
            states,
            [
                DiscordServiceState.STARTING,
                DiscordServiceState.DEGRADED,
                DiscordServiceState.COMMANDS_READY,
            ],
        )

    async def test_close_cancels_pending_retry(self) -> None:
        attempts = 0

        async def start_client() -> None:
            nonlocal attempts
            attempts += 1
            raise _discord_unavailable_error()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            update_state=lambda _state: None,
            initial_retry_delay_seconds=30.0,
            maximum_retry_delay_seconds=30.0,
        )

        self.assertFalse(await supervisor.start())
        await supervisor.close()
        await asyncio.sleep(0)

        self.assertEqual(attempts, 1)

    async def test_does_not_retry_terminal_command_sync_failure(self) -> None:
        attempts = 0
        states: list[DiscordServiceState] = []

        async def start_client() -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Bot token is invalid")

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            update_state=states.append,
            initial_retry_delay_seconds=0.001,
            maximum_retry_delay_seconds=0.001,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertFalse(await supervisor.start())
        await asyncio.sleep(0.01)

        self.assertEqual(attempts, 1)
        self.assertEqual(states, [DiscordServiceState.STARTING, DiscordServiceState.FAILED])

    async def test_waits_for_transient_gateway_failure_before_startup_can_continue(self) -> None:
        gateway_attempts = 0
        states: list[DiscordServiceState] = []

        async def start_client() -> None:
            return None

        async def probe_gateway() -> object:
            nonlocal gateway_attempts
            gateway_attempts += 1
            if gateway_attempts == 1:
                raise _discord_unavailable_error(url="https://discord.test/gateway/bot")
            return object()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            probe_gateway=probe_gateway,
            update_state=states.append,
            initial_retry_delay_seconds=0.001,
            maximum_retry_delay_seconds=0.001,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertTrue(await supervisor.start())
        self.assertTrue(await supervisor.wait_for_gateway())
        supervisor.mark_gateway_ready()

        self.assertEqual(gateway_attempts, 2)
        self.assertEqual(
            states,
            [
                DiscordServiceState.STARTING,
                DiscordServiceState.COMMANDS_READY,
                DiscordServiceState.GATEWAY_DEGRADED,
                DiscordServiceState.COMMANDS_READY,
                DiscordServiceState.READY,
            ],
        )

    async def test_runtime_shard_disconnect_recovers_without_affecting_local_services(self) -> None:
        states: list[DiscordServiceState] = []
        health_states: list[DiscordHealthComponentState] = []

        async def start_client() -> None:
            return None

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            update_state=states.append,
            update_health=lambda health: health_states.append(health.gateway_state),
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertTrue(await supervisor.start())
        supervisor.mark_gateway_ready()
        supervisor.mark_gateway_shard_disconnected(0)
        supervisor.mark_gateway_shard_ready(0)

        self.assertEqual(
            states,
            [
                DiscordServiceState.STARTING,
                DiscordServiceState.COMMANDS_READY,
                DiscordServiceState.READY,
                DiscordServiceState.GATEWAY_DEGRADED,
                DiscordServiceState.READY,
            ],
        )
        self.assertEqual(health_states[-1], DiscordHealthComponentState.READY)

    async def test_ongoing_rest_probe_marks_degraded_then_recovers(self) -> None:
        gateway_attempts = 0
        probe_attempts = 0
        states: list[DiscordServiceState] = []
        health_snapshots: list[DiscordHealthSnapshot] = []
        recovered = asyncio.Event()

        async def start_client() -> None:
            return None

        async def probe_gateway() -> object:
            nonlocal gateway_attempts
            gateway_attempts += 1
            return object()

        async def probe_rest() -> object:
            nonlocal probe_attempts
            probe_attempts += 1
            if probe_attempts == 1:
                raise _discord_unavailable_error(url="https://discord.test/users/@me")
            return object()

        def update_state(state: DiscordServiceState) -> None:
            states.append(state)
            if state is DiscordServiceState.READY and DiscordServiceState.DEGRADED in states:
                recovered.set()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            probe_gateway=probe_gateway,
            probe_rest=probe_rest,
            update_state=update_state,
            update_health=health_snapshots.append,
            initial_retry_delay_seconds=0.001,
            maximum_retry_delay_seconds=0.001,
            health_probe_interval_seconds=0.001,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertTrue(await supervisor.start())
        self.assertTrue(await supervisor.wait_for_gateway())
        supervisor.mark_gateway_ready()
        await asyncio.wait_for(recovered.wait(), timeout=0.5)

        self.assertEqual(gateway_attempts, 1)
        self.assertEqual(probe_attempts, 2)
        self.assertEqual(states[-3:], [DiscordServiceState.READY, DiscordServiceState.DEGRADED, DiscordServiceState.READY])
        self.assertEqual(health_snapshots[-1].rest_state, DiscordHealthComponentState.READY)
        self.assertIsNone(health_snapshots[-1].next_retry_at)

    async def test_ongoing_heartbeat_failures_mark_gateway_degraded_then_recover(self) -> None:
        heartbeat_samples = iter((float("nan"), float("nan"), 0.025))
        states: list[DiscordServiceState] = []
        recovered = asyncio.Event()

        async def start_client() -> None:
            return None

        def heartbeat_latency() -> float:
            try:
                return next(heartbeat_samples)
            except StopIteration:
                return 0.025

        def update_state(state: DiscordServiceState) -> None:
            states.append(state)
            if state is DiscordServiceState.READY and DiscordServiceState.GATEWAY_DEGRADED in states:
                recovered.set()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            heartbeat_latency=heartbeat_latency,
            update_state=update_state,
            health_probe_interval_seconds=0.001,
            unhealthy_heartbeat_sample_limit=2,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertTrue(await supervisor.start())
        supervisor.mark_gateway_ready()
        await asyncio.wait_for(recovered.wait(), timeout=0.5)

        self.assertEqual(
            states[-3:],
            [
                DiscordServiceState.READY,
                DiscordServiceState.GATEWAY_DEGRADED,
                DiscordServiceState.READY,
            ],
        )

    async def test_rest_health_updates_preserve_the_pending_command_retry_timestamp(self) -> None:
        health_snapshots: list[DiscordHealthSnapshot] = []
        rest_health_observed = asyncio.Event()

        async def start_client() -> None:
            raise _discord_unavailable_error()

        async def probe_rest() -> object:
            return object()

        def update_health(health: DiscordHealthSnapshot) -> None:
            health_snapshots.append(health)
            if (
                health.command_state is DiscordHealthComponentState.DEGRADED
                and health.rest_state is DiscordHealthComponentState.READY
            ):
                rest_health_observed.set()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            probe_rest=probe_rest,
            update_health=update_health,
            initial_retry_delay_seconds=1.0,
            maximum_retry_delay_seconds=1.0,
            health_probe_interval_seconds=0.001,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertFalse(await supervisor.start())
        supervisor.mark_gateway_ready()
        await asyncio.wait_for(rest_health_observed.wait(), timeout=0.5)

        self.assertIsNotNone(health_snapshots[-1].next_retry_at)

    async def test_terminal_gateway_failure_keeps_startup_open_until_shutdown(self) -> None:
        states: list[DiscordServiceState] = []

        async def start_client() -> None:
            return None

        async def probe_gateway() -> object:
            raise _invalid_token_error()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            probe_gateway=probe_gateway,
            update_state=states.append,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertTrue(await supervisor.start())
        gateway_wait = asyncio.create_task(supervisor.wait_for_gateway())
        await asyncio.sleep(0)

        self.assertFalse(gateway_wait.done())
        self.assertEqual(states[-1], DiscordServiceState.FAILED)

        await supervisor.close()
        self.assertFalse(await gateway_wait)

    async def test_command_sync_honours_retry_after_response_header(self) -> None:
        attempts = 0

        async def start_client() -> None:
            nonlocal attempts
            attempts += 1
            raise _discord_unavailable_error(headers={"Retry-After": "0.1"})

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            update_state=lambda _state: None,
            initial_retry_delay_seconds=0.001,
            maximum_retry_delay_seconds=0.001,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertFalse(await supervisor.start())
        await asyncio.sleep(0.01)

        self.assertEqual(attempts, 1)

    async def test_command_sync_does_not_reuse_a_previous_retry_after(self) -> None:
        attempts = 0
        second_failure_at: float | None = None
        command_sync_complete = asyncio.Event()

        async def start_client() -> None:
            nonlocal attempts, second_failure_at
            attempts += 1
            if attempts == 1:
                raise _discord_unavailable_error(headers={"Retry-After": "0.05"})
            if attempts == 2:
                second_failure_at = asyncio.get_running_loop().time()
                raise _discord_unavailable_error()
            command_sync_complete.set()

        supervisor = DiscordClientStartupSupervisor(
            start_client=start_client,
            update_state=lambda _state: None,
            initial_retry_delay_seconds=0.01,
            maximum_retry_delay_seconds=0.01,
        )
        self.addAsyncCleanup(supervisor.close)

        self.assertFalse(await supervisor.start())
        await asyncio.wait_for(command_sync_complete.wait(), timeout=0.25)

        self.assertEqual(attempts, 3)
        if second_failure_at is None:
            self.fail("Second command-sync failure was not recorded")
        self.assertLess(asyncio.get_running_loop().time() - second_failure_at, 0.04)


if __name__ == "__main__":
    unittest.main()
