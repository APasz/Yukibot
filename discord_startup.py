"""Keep local node services available while Discord starts or recovers."""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import hikari

from node_api_route_contracts import (
    DiscordHealthComponentState,
    DiscordHealthSnapshot,
    DiscordServiceState,
)

log = logging.getLogger(__name__)


class _DiscordProbeOutcome(enum.Enum):
    READY = enum.auto()
    RETRY = enum.auto()
    FAILED = enum.auto()


class DiscordClientStartupSupervisor:
    """Supervise Discord command sync, gateway bootstrap, and ongoing health.

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
        probe_rest: Callable[[], Awaitable[object]] | None = None,
        heartbeat_latency: Callable[[], float] | None = None,
        update_state: Callable[[DiscordServiceState], None] | None = None,
        update_health: Callable[[DiscordHealthSnapshot], None] | None = None,
        on_started: Callable[[], Awaitable[None]] | None = None,
        initial_retry_delay_seconds: float = 15.0,
        maximum_retry_delay_seconds: float = 300.0,
        health_probe_interval_seconds: float = 300.0,
        maximum_heartbeat_latency_seconds: float = 10.0,
        unhealthy_heartbeat_sample_limit: int = 2,
    ) -> None:
        if initial_retry_delay_seconds <= 0:
            raise ValueError("Discord startup retry delay must be positive.")
        if maximum_retry_delay_seconds < initial_retry_delay_seconds:
            raise ValueError("Discord startup maximum retry delay must not be less than the initial delay.")
        if health_probe_interval_seconds <= 0:
            raise ValueError("Discord health probe interval must be positive.")
        if maximum_heartbeat_latency_seconds <= 0:
            raise ValueError("Discord heartbeat latency threshold must be positive.")
        if unhealthy_heartbeat_sample_limit <= 0:
            raise ValueError("Discord unhealthy heartbeat sample limit must be positive.")
        if update_state is None and update_health is None:
            raise ValueError("Discord health supervisor requires a state or health update callback.")

        self._start_client = start_client
        self._probe_gateway = probe_gateway
        self._probe_rest = probe_rest
        self._heartbeat_latency = heartbeat_latency
        self._update_state = update_state
        self._update_health = update_health
        self._on_started = on_started
        self._initial_retry_delay_seconds = initial_retry_delay_seconds
        self._maximum_retry_delay_seconds = maximum_retry_delay_seconds
        self._health_probe_interval_seconds = health_probe_interval_seconds
        self._maximum_heartbeat_latency_seconds = maximum_heartbeat_latency_seconds
        self._unhealthy_heartbeat_sample_limit = unhealthy_heartbeat_sample_limit
        self._start_lock = asyncio.Lock()
        self._retry_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self._command_state = DiscordHealthComponentState.STARTING
        self._gateway_state = DiscordHealthComponentState.STARTING
        self._rest_state = DiscordHealthComponentState.UNKNOWN
        self._gateway_started = False
        self._reconnecting_shard_ids: set[int] = set()
        self._heartbeat_degraded = False
        self._unhealthy_heartbeat_samples = 0
        self._gateway_latency_ms: int | None = None
        self._command_error: str | None = None
        self._gateway_error: str | None = None
        self._rest_error: str | None = None
        self._command_retry_after_seconds: float | None = None
        self._last_rest_success_at: datetime | None = None
        self._last_rest_failure_at: datetime | None = None
        self._command_retry_at: datetime | None = None
        self._gateway_retry_at: datetime | None = None
        self._rest_retry_at: datetime | None = None
        self._service_state = DiscordServiceState.STARTING
        self._state_changed_at = self._utc_now()
        self._state_has_been_published = False

    async def start(self) -> bool:
        """Start command sync and schedule retries only for transient failures."""

        outcome = await self._attempt_command_sync()
        if outcome is _DiscordProbeOutcome.RETRY:
            self._schedule_command_sync_retry()
        return outcome is _DiscordProbeOutcome.READY

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
                error_text = self._format_error(xcp)
                retryable = self._is_retryable(xcp)
                self._record_rest_failure(error_text, terminal=not retryable)
                if not retryable:
                    self._gateway_state = DiscordHealthComponentState.FAILED
                    self._gateway_error = error_text
                    self._clear_retry_times()
                    self._publish_state()
                    await self._cancel_command_sync_retry()
                    log.error("Discord gateway bootstrap cannot continue without operator action: %s", error_text)
                    await self._closed_event.wait()
                    return False

                self._gateway_state = DiscordHealthComponentState.DEGRADED
                self._gateway_error = error_text
                delay_seconds = self._retry_delay_seconds(retry_delay_seconds, xcp)
                self._gateway_retry_at = self._retry_at(delay_seconds)
                self._publish_state()
                log.warning(
                    "Discord gateway bootstrap failed; local node services remain online and retrying in %.1fs: %s",
                    delay_seconds,
                    error_text,
                )
                retry_delay_seconds = min(retry_delay_seconds * 2, self._maximum_retry_delay_seconds)
                await self._wait_for_close_or_timeout(delay_seconds)
                continue

            self._gateway_state = DiscordHealthComponentState.STARTING
            self._gateway_error = None
            self._gateway_retry_at = None
            self._record_rest_success()
            self._publish_state()
            log.info("Discord gateway endpoint is reachable; continuing gateway startup")
            return True

        return False

    def mark_gateway_ready(self) -> None:
        """Record Hikari's ``StartedEvent`` after the gateway is genuinely ready."""

        if self._closed:
            return
        self._gateway_started = True
        self._reconnecting_shard_ids.clear()
        self._heartbeat_degraded = False
        self._unhealthy_heartbeat_samples = 0
        if self._gateway_state is not DiscordHealthComponentState.FAILED:
            self._gateway_state = DiscordHealthComponentState.READY
            self._gateway_error = None
        self._publish_state()
        self._schedule_ongoing_health_monitor()

    def mark_gateway_shard_disconnected(self, shard_id: int) -> None:
        """Record a runtime gateway disconnect without disturbing local services."""

        if self._closed or not self._gateway_started or self._gateway_state is DiscordHealthComponentState.FAILED:
            return
        self._reconnecting_shard_ids.add(shard_id)
        self._gateway_state = DiscordHealthComponentState.DEGRADED
        self._gateway_error = self._gateway_reconnect_error()
        self._publish_state()
        log.warning("Discord gateway shard disconnected; waiting for reconnect: shard=%s", shard_id)

    def mark_gateway_shard_ready(self, shard_id: int) -> None:
        """Record a shard that is ready again after a reconnect or resume."""

        if self._closed:
            return
        was_reconnecting = shard_id in self._reconnecting_shard_ids
        self._reconnecting_shard_ids.discard(shard_id)
        if not self._gateway_started and not was_reconnecting:
            return
        if (
            self._gateway_started
            and not self._reconnecting_shard_ids
            and not self._heartbeat_degraded
            and self._gateway_state is not DiscordHealthComponentState.FAILED
        ):
            self._gateway_state = DiscordHealthComponentState.READY
            self._gateway_error = None
        self._publish_state()

    async def close(self) -> None:
        """Cancel retries and release any startup wait during process shutdown."""

        self._closed = True
        self._closed_event.set()
        self._clear_retry_times()
        await self._cancel_task(self._retry_task)
        await self._cancel_task(self._health_task)

    async def _attempt_command_sync(self) -> _DiscordProbeOutcome:
        async with self._start_lock:
            if self._closed or self._has_terminal_failure():
                return _DiscordProbeOutcome.FAILED
            self._command_state = DiscordHealthComponentState.STARTING
            self._command_error = None
            self._command_retry_after_seconds = None
            self._command_retry_at = None
            self._publish_state()
            try:
                await self._start_client()
            except Exception as xcp:
                error_text = self._format_error(xcp)
                terminal = not self._is_retryable(xcp)
                self._command_state = (
                    DiscordHealthComponentState.FAILED if terminal else DiscordHealthComponentState.DEGRADED
                )
                self._command_error = error_text
                self._command_retry_after_seconds = self._retry_after_seconds(xcp)
                self._record_rest_failure(error_text, terminal=terminal)
                if terminal:
                    self._clear_retry_times()
                self._publish_state()
                if terminal:
                    log.error("Discord command sync cannot continue without operator action: %s", error_text)
                    return _DiscordProbeOutcome.FAILED
                log.warning("Discord command sync failed; local node services remain online and retrying: %s", error_text)
                return _DiscordProbeOutcome.RETRY

            self._command_state = DiscordHealthComponentState.READY
            self._command_error = None
            self._record_rest_success()
            self._publish_state()
            if self._on_started is not None:
                try:
                    await self._on_started()
                except Exception:
                    log.exception("Discord client started, but post-start setup failed")
            log.info("Discord command service is ready")
            return _DiscordProbeOutcome.READY

    def _schedule_command_sync_retry(self) -> None:
        retry_task = self._retry_task
        if self._closed or (retry_task is not None and not retry_task.done()):
            return
        self._retry_task = asyncio.create_task(
            self._retry_until_command_sync_succeeds(),
            name="discord-command-sync-retry",
        )

    def _schedule_ongoing_health_monitor(self) -> None:
        health_task = self._health_task
        if (
            self._closed
            or self._has_terminal_failure()
            or (self._probe_rest is None and self._heartbeat_latency is None)
            or (health_task is not None and not health_task.done())
        ):
            return
        self._health_task = asyncio.create_task(
            self._monitor_ongoing_health(),
            name="discord-health-monitor",
        )

    async def _cancel_command_sync_retry(self) -> None:
        await self._cancel_task(self._retry_task)

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _retry_until_command_sync_succeeds(self) -> None:
        retry_delay_seconds = self._initial_retry_delay_seconds
        while not self._closed and not self._has_terminal_failure():
            delay_seconds = max(
                self._retry_delay_seconds(retry_delay_seconds),
                self._command_retry_after_seconds or 0.0,
            )
            self._command_retry_at = self._retry_at(delay_seconds)
            self._publish_state()
            log.info("Retrying Discord command sync in %.1fs", delay_seconds)
            if await self._wait_for_close_or_timeout(delay_seconds):
                return
            outcome = await self._attempt_command_sync()
            if outcome is not _DiscordProbeOutcome.RETRY:
                return
            retry_delay_seconds = min(retry_delay_seconds * 2, self._maximum_retry_delay_seconds)

    async def _monitor_ongoing_health(self) -> None:
        retry_delay_seconds = self._initial_retry_delay_seconds
        delay_seconds = self._health_probe_interval_seconds
        while not self._closed and not self._has_terminal_failure():
            if await self._wait_for_close_or_timeout(delay_seconds):
                return
            self._rest_retry_at = None
            self._sample_gateway_heartbeat()
            probe_outcome, retry_after_seconds = await self._probe_ongoing_rest_health()
            if probe_outcome is _DiscordProbeOutcome.FAILED:
                return
            if probe_outcome is _DiscordProbeOutcome.READY:
                retry_delay_seconds = self._initial_retry_delay_seconds
                delay_seconds = self._health_probe_interval_seconds
                continue

            delay_seconds = max(
                self._jittered_retry_delay(retry_delay_seconds),
                retry_after_seconds or 0.0,
            )
            retry_delay_seconds = min(retry_delay_seconds * 2, self._maximum_retry_delay_seconds)
            self._rest_retry_at = self._retry_at(delay_seconds)
            self._publish_state()
            log.warning(
                "Discord REST health probe will retry in %.1fs; local node services remain online",
                delay_seconds,
            )

    async def _probe_ongoing_rest_health(self) -> tuple[_DiscordProbeOutcome, float | None]:
        probe_rest = self._probe_rest
        if probe_rest is None:
            return (_DiscordProbeOutcome.READY, None)
        try:
            await probe_rest()
        except Exception as xcp:
            error_text = self._format_error(xcp)
            terminal = not self._is_retryable(xcp)
            self._record_rest_failure(error_text, terminal=terminal)
            if terminal:
                self._clear_retry_times()
            self._publish_state()
            if terminal:
                log.error("Discord REST health probe requires operator action: %s", error_text)
                return (_DiscordProbeOutcome.FAILED, None)
            log.warning("Discord REST health probe failed: %s", error_text)
            return (_DiscordProbeOutcome.RETRY, self._retry_after_seconds(xcp))

        recovered = self._rest_state is DiscordHealthComponentState.DEGRADED
        self._record_rest_success()
        self._publish_state()
        if recovered:
            log.info("Discord REST health recovered")
        return (_DiscordProbeOutcome.READY, None)

    def _sample_gateway_heartbeat(self) -> None:
        heartbeat_latency = self._heartbeat_latency
        if heartbeat_latency is None or not self._gateway_started:
            return
        latency_seconds = heartbeat_latency()
        is_healthy = math.isfinite(latency_seconds) and latency_seconds <= self._maximum_heartbeat_latency_seconds
        if is_healthy:
            self._gateway_latency_ms = max(0, round(latency_seconds * 1000))
            self._unhealthy_heartbeat_samples = 0
            if self._heartbeat_degraded:
                self._heartbeat_degraded = False
                if not self._reconnecting_shard_ids and self._gateway_state is not DiscordHealthComponentState.FAILED:
                    self._gateway_state = DiscordHealthComponentState.READY
                    self._gateway_error = None
                    log.info("Discord gateway heartbeat health recovered")
            self._publish_state()
            return

        self._gateway_latency_ms = None if not math.isfinite(latency_seconds) else round(latency_seconds * 1000)
        self._unhealthy_heartbeat_samples += 1
        if self._unhealthy_heartbeat_samples >= self._unhealthy_heartbeat_sample_limit:
            self._heartbeat_degraded = True
            if self._gateway_state is not DiscordHealthComponentState.FAILED:
                self._gateway_state = DiscordHealthComponentState.DEGRADED
                self._gateway_error = "Gateway heartbeat is unavailable or exceeding the latency threshold."
        self._publish_state()

    def _service_state_from_components(self) -> DiscordServiceState:
        if self._has_terminal_failure():
            return DiscordServiceState.FAILED
        if self._gateway_state is DiscordHealthComponentState.DEGRADED:
            return DiscordServiceState.GATEWAY_DEGRADED
        if (
            self._command_state is DiscordHealthComponentState.DEGRADED
            or self._rest_state is DiscordHealthComponentState.DEGRADED
        ):
            return DiscordServiceState.DEGRADED
        if (
            self._command_state is DiscordHealthComponentState.READY
            and self._gateway_state is DiscordHealthComponentState.READY
        ):
            return DiscordServiceState.READY
        if self._command_state is DiscordHealthComponentState.READY:
            return DiscordServiceState.COMMANDS_READY
        return DiscordServiceState.STARTING

    def _has_terminal_failure(self) -> bool:
        return DiscordHealthComponentState.FAILED in {
            self._command_state,
            self._gateway_state,
            self._rest_state,
        }

    def _publish_state(self) -> None:
        state = self._service_state_from_components()
        state_changed = state is not self._service_state
        if state_changed:
            self._service_state = state
            self._state_changed_at = self._utc_now()
        if state_changed or not self._state_has_been_published:
            if self._update_state is not None:
                try:
                    self._update_state(state)
                except Exception:
                    log.exception("Discord service-state update callback failed")
            self._state_has_been_published = True
        if self._update_health is not None:
            try:
                self._update_health(
                    DiscordHealthSnapshot(
                        service_state=state,
                        command_state=self._command_state,
                        gateway_state=self._gateway_state,
                        rest_state=self._rest_state,
                        state_changed_at=self._state_changed_at,
                        last_rest_success_at=self._last_rest_success_at,
                        last_rest_failure_at=self._last_rest_failure_at,
                    next_retry_at=self._next_retry_at(),
                        last_error=self._current_error(),
                        gateway_latency_ms=self._gateway_latency_ms,
                        reconnecting_shard_ids=tuple(sorted(self._reconnecting_shard_ids)),
                    )
                )
            except Exception:
                log.exception("Discord health update callback failed")

    def _record_rest_success(self) -> None:
        if self._rest_state is not DiscordHealthComponentState.FAILED:
            self._rest_state = DiscordHealthComponentState.READY
            self._rest_error = None
        self._last_rest_success_at = self._utc_now()

    def _record_rest_failure(self, error_text: str, *, terminal: bool) -> None:
        self._rest_state = DiscordHealthComponentState.FAILED if terminal else DiscordHealthComponentState.DEGRADED
        self._rest_error = error_text
        self._last_rest_failure_at = self._utc_now()

    def _next_retry_at(self) -> datetime | None:
        retry_times = (
            retry_at
            for retry_at in (self._command_retry_at, self._gateway_retry_at, self._rest_retry_at)
            if retry_at is not None
        )
        return min(retry_times, default=None)

    def _clear_retry_times(self) -> None:
        self._command_retry_at = None
        self._gateway_retry_at = None
        self._rest_retry_at = None

    def _retry_at(self, delay_seconds: float) -> datetime:
        return self._utc_now() + timedelta(seconds=delay_seconds)

    def _current_error(self) -> str | None:
        return self._gateway_error or self._command_error or self._rest_error

    def _gateway_reconnect_error(self) -> str:
        count = len(self._reconnecting_shard_ids)
        suffix = "" if count == 1 else "s"
        return f"{count} gateway shard{suffix} reconnecting."

    def _retry_delay_seconds(self, retry_delay_seconds: float, exception: Exception | None = None) -> float:
        retry_after_seconds = self._retry_after_seconds(exception) if exception is not None else None
        return max(self._jittered_retry_delay(retry_delay_seconds), retry_after_seconds or 0.0)

    async def _wait_for_close_or_timeout(self, delay_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._closed_event.wait(), timeout=delay_seconds)
        except asyncio.TimeoutError:
            return False
        return True

    def _jittered_retry_delay(self, retry_delay_seconds: float) -> float:
        return retry_delay_seconds * random.uniform(0.8, 1.2)

    @staticmethod
    def _format_error(exception: Exception) -> str:
        return f"{type(exception).__name__}: {exception}"

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

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
