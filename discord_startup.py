"""Keep local node services available while Discord starts or recovers."""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import random
from collections.abc import Awaitable, Callable

import hikari

from node_api_route_contracts import DiscordServiceState

log = logging.getLogger(__name__)


class _StartupAttemptOutcome(enum.Enum):
    READY = enum.auto()
    RETRY = enum.auto()
    FAILED = enum.auto()


class DiscordClientStartupSupervisor:
    """Supervise Discord command sync and gateway bootstrap independently.

    ``wait_for_gateway`` is intentionally awaited from Hikari's ``StartingEvent``.
    Hikari fetches gateway metadata immediately after that event completes and closes
    the whole bot on a failure. Keeping the event pending during a transient outage
    preserves the local Node API, app manager, and maintenance tasks.
    """

    def __init__(
        self,
        *,
        start_client: Callable[[], Awaitable[None]],
        probe_gateway: Callable[[], Awaitable[object]] | None = None,
        update_state: Callable[[DiscordServiceState], None],
        on_started: Callable[[], Awaitable[None]] | None = None,
        initial_retry_delay_seconds: float = 15.0,
        maximum_retry_delay_seconds: float = 300.0,
    ) -> None:
        if initial_retry_delay_seconds <= 0:
            raise ValueError("Discord startup retry delay must be positive.")
        if maximum_retry_delay_seconds < initial_retry_delay_seconds:
            raise ValueError("Discord startup maximum retry delay must not be less than the initial delay.")
        self._start_client = start_client
        self._probe_gateway = probe_gateway
        self._update_state = update_state
        self._on_started = on_started
        self._initial_retry_delay_seconds = initial_retry_delay_seconds
        self._maximum_retry_delay_seconds = maximum_retry_delay_seconds
        self._start_lock = asyncio.Lock()
        self._retry_task: asyncio.Task[None] | None = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self._commands_ready = False
        self._command_sync_retrying = False
        self._command_retry_after_seconds: float | None = None
        self._gateway_ready = False
        self._gateway_retrying = False
        self._terminal_failure = False

    async def start(self) -> bool:
        """Start command sync and schedule retries only for transient failures."""

        outcome = await self._attempt_command_sync()
        if outcome is _StartupAttemptOutcome.RETRY:
            self._schedule_retry()
        return outcome is _StartupAttemptOutcome.READY

    async def wait_for_gateway(self) -> bool:
        """Keep Hikari startup open until its gateway endpoint is reachable.

        A terminal failure remains visible as ``FAILED`` and keeps local services
        running until an operator shuts the process down. It is never retried as if
        it were a Discord outage.
        """

        probe_gateway = self._probe_gateway
        if probe_gateway is None:
            return True

        retry_delay_seconds = self._initial_retry_delay_seconds
        while not self._closed:
            try:
                await probe_gateway()
            except Exception as xcp:
                if not self._is_retryable(xcp):
                    self._terminal_failure = True
                    self._publish_state()
                    await self._cancel_command_sync_retry()
                    log.error(
                        "Discord gateway bootstrap cannot continue without operator action: %s: %s",
                        type(xcp).__name__,
                        xcp,
                    )
                    await self._closed_event.wait()
                    return False

                self._gateway_retrying = True
                self._publish_state()
                retry_delay_seconds = await self._log_and_wait_to_retry(
                    operation="gateway bootstrap",
                    exception=xcp,
                    retry_delay_seconds=retry_delay_seconds,
                )
                continue

            self._gateway_retrying = False
            self._publish_state()
            log.info("Discord gateway endpoint is reachable; continuing gateway startup")
            return True

        return False

    def mark_gateway_ready(self) -> None:
        """Record Hikari's ``StartedEvent`` after the gateway is genuinely ready."""

        self._gateway_ready = True
        self._gateway_retrying = False
        self._publish_state()

    async def close(self) -> None:
        """Cancel retries and release any startup wait during process shutdown."""

        self._closed = True
        self._closed_event.set()
        await self._cancel_command_sync_retry()

    async def _attempt_command_sync(self) -> _StartupAttemptOutcome:
        async with self._start_lock:
            if self._closed or self._terminal_failure:
                return _StartupAttemptOutcome.FAILED
            self._command_sync_retrying = False
            self._command_retry_after_seconds = None
            self._publish_state()
            try:
                await self._start_client()
            except Exception as xcp:
                if not self._is_retryable(xcp):
                    self._terminal_failure = True
                    self._publish_state()
                    log.error(
                        "Discord command sync cannot continue without operator action: %s: %s",
                        type(xcp).__name__,
                        xcp,
                    )
                    return _StartupAttemptOutcome.FAILED

                self._command_sync_retrying = True
                self._command_retry_after_seconds = self._retry_after_seconds(xcp)
                self._publish_state()
                log.warning(
                    "Discord command sync failed; local node services remain online and retrying: %s: %s",
                    type(xcp).__name__,
                    xcp,
                )
                return _StartupAttemptOutcome.RETRY

            self._commands_ready = True
            self._command_sync_retrying = False
            self._publish_state()
            if self._on_started is not None:
                try:
                    await self._on_started()
                except Exception:
                    log.exception("Discord client started, but post-start setup failed")
            log.info("Discord command service is ready")
            return _StartupAttemptOutcome.READY

    def _publish_state(self) -> None:
        if self._terminal_failure:
            state = DiscordServiceState.FAILED
        elif self._gateway_retrying:
            state = DiscordServiceState.GATEWAY_DEGRADED
        elif self._command_sync_retrying:
            state = DiscordServiceState.DEGRADED
        elif self._commands_ready and self._gateway_ready:
            state = DiscordServiceState.READY
        elif self._commands_ready:
            state = DiscordServiceState.COMMANDS_READY
        else:
            state = DiscordServiceState.STARTING
        self._update_state(state)

    def _schedule_retry(self) -> None:
        retry_task = self._retry_task
        if self._closed or (retry_task is not None and not retry_task.done()):
            return
        self._retry_task = asyncio.create_task(
            self._retry_until_command_sync_succeeds(),
            name="discord-command-sync-retry",
        )

    async def _cancel_command_sync_retry(self) -> None:
        retry_task = self._retry_task
        if retry_task is None or retry_task.done():
            return
        retry_task.cancel()
        try:
            await retry_task
        except asyncio.CancelledError:
            pass

    async def _retry_until_command_sync_succeeds(self) -> None:
        retry_delay_seconds = self._initial_retry_delay_seconds
        while not self._closed:
            delay_seconds = max(
                self._jittered_retry_delay(retry_delay_seconds),
                self._command_retry_after_seconds or 0.0,
            )
            log.info("Retrying Discord command sync in %.1fs", delay_seconds)
            try:
                await asyncio.wait_for(self._closed_event.wait(), timeout=delay_seconds)
            except asyncio.TimeoutError:
                pass
            if self._closed:
                return
            outcome = await self._attempt_command_sync()
            if outcome is not _StartupAttemptOutcome.RETRY:
                return
            retry_delay_seconds = min(retry_delay_seconds * 2, self._maximum_retry_delay_seconds)

    async def _log_and_wait_to_retry(
        self,
        *,
        operation: str,
        exception: Exception,
        retry_delay_seconds: float,
    ) -> float:
        retry_after_seconds = self._retry_after_seconds(exception)
        delay_seconds = max(self._jittered_retry_delay(retry_delay_seconds), retry_after_seconds or 0.0)
        log.warning(
            "Discord %s failed; local node services remain online and retrying in %.1fs: %s: %s",
            operation,
            delay_seconds,
            type(exception).__name__,
            exception,
        )
        try:
            await asyncio.wait_for(self._closed_event.wait(), timeout=delay_seconds)
        except asyncio.TimeoutError:
            pass
        return min(retry_delay_seconds * 2, self._maximum_retry_delay_seconds)

    def _jittered_retry_delay(self, retry_delay_seconds: float) -> float:
        return retry_delay_seconds * random.uniform(0.8, 1.2)

    @staticmethod
    def _is_retryable(exception: Exception) -> bool:
        if isinstance(exception, (asyncio.TimeoutError, OSError, hikari.GatewayError, hikari.HTTPError)):
            if isinstance(exception, hikari.HTTPResponseError):
                status_code = int(exception.status)
                return status_code == 429 or status_code >= 500
            return True
        return False

    @staticmethod
    def _retry_after_seconds(exception: Exception) -> float | None:
        if isinstance(exception, hikari.RateLimitTooLongError):
            return exception.retry_after
        if not isinstance(exception, hikari.HTTPResponseError):
            return None
        raw_retry_after = exception.headers.get("Retry-After")
        if raw_retry_after is None:
            return None
        try:
            retry_after = float(raw_retry_after)
        except (TypeError, ValueError):
            return None
        return retry_after if math.isfinite(retry_after) and retry_after > 0 else None
