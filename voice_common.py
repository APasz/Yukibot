from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import logging
import struct
import wave
from collections.abc import Awaitable, Mapping
from typing import Callable, Protocol, cast

import hikari

log = logging.getLogger(__name__)

_patches_applied = False
_VOICE_UDP_DISCOVERY_ATTEMPTS = 3
_VOICE_UDP_DISCOVERY_TIMEOUT_SECONDS = 5.0
_VOICE_UDP_DISCOVERY_RETRY_DELAY_STEP_SECONDS = 0.25
_VOICE_UDP_DISCOVERY_PACKET_LENGTH = 70
_VOICE_UDP_DISCOVERY_REQUEST_TYPE = 0x0001
_VOICE_UDP_DISCOVERY_RESPONSE_TYPE = 0x0002
_VOICE_UDP_DISCOVERY_RESPONSE_MIN_LENGTH = 74


class _VoiceBotLike(Protocol):
    def get_me(self) -> hikari.OwnUser | None: ...


class _VoiceClientLike(Protocol):
    _bot: _VoiceBotLike


class _HikariWaveCacheLike(Protocol):
    _client: _VoiceClientLike
    _members: Mapping[hikari.Snowflake, hikari.Snowflake]

    async def _Cache__member_join(
        self,
        member: hikari.Member,
        state: hikari.VoiceState,
        new_channel_id: hikari.Snowflake,
    ) -> None: ...

    async def _Cache__member_move(
        self,
        member: hikari.Member,
        old_channel_id: hikari.Snowflake,
        new_channel_id: hikari.Snowflake,
    ) -> None: ...

    async def _Cache__member_leave(
        self,
        member: hikari.Member,
        old_channel_id: hikari.Snowflake,
    ) -> None: ...

    async def _Cache__member_update(
        self,
        member: hikari.Member,
        state: hikari.VoiceState,
        new_channel_id: hikari.Snowflake,
    ) -> None: ...


class _AsyncDisconnectable(Protocol):
    async def disconnect(self) -> object: ...


class _TaskManagerLike(Protocol):
    def create(self, coro: Awaitable[None], /, *, name: str) -> asyncio.Task[None]: ...


class _VoiceConnectionClientLike(Protocol):
    _tasks: _TaskManagerLike


class _VoiceGatewayLike(_AsyncDisconnectable, Protocol):
    _task_listen: asyncio.Task[None] | None

    async def connect(self, endpoint: str) -> object: ...


class _VoiceConnectionLike(Protocol):
    _lock: asyncio.Lock
    _state: object
    _ready: asyncio.Event
    _client: _VoiceConnectionClientLike
    _gateway: _VoiceGatewayLike | None
    _endpoint: str
    _report_task: asyncio.Task[None] | None
    _server: _AsyncDisconnectable | None

    def _VoiceConnection__loop_reports(self) -> Awaitable[None]: ...


class _UdpTransportLike(Protocol):
    def close(self) -> None: ...


class _VoiceServerLike(Protocol):
    _ip: str | None
    _port: int | None
    _ssrc: int | None
    _udp: _UdpTransportLike | None

    def _rtp_packet(self, data: bytes) -> None: ...


class VoiceUdpDiscoveryTimeoutError(asyncio.TimeoutError):
    def __init__(self, *, ip: str | None, port: int | None, attempts: int):
        self.ip = ip
        self.port = port
        self.attempts = attempts
        super().__init__(f"Voice UDP discovery timed out after {attempts} attempts (ip={ip!r} port={port!r})")


class VoiceUdpDiscoveryNetworkError(OSError):
    def __init__(self, *, ip: str | None, port: int | None, attempts: int, cause: OSError):
        self.ip = ip
        self.port = port
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"Voice UDP discovery failed after {attempts} attempts "
            f"(ip={ip!r} port={port!r}): {type(cause).__name__}: {cause}"
        )


def apply_hikariwave_patches() -> None:
    global _patches_applied
    if _patches_applied:
        return

    _patch_hikariwave_cache_state_update_bug()
    _patch_hikariwave_connect_wait_hang()
    _patch_hikariwave_udp_protocol_error_bug()
    _patch_hikariwave_udp_discovery_timeout()
    _patch_hikariwave_player_idle_queue_race()
    _patches_applied = True


def cached_user_voice_channel(
    bot: hikari.GatewayBot,
    guild_id: hikari.Snowflakeish,
    user_id: hikari.Snowflakeish,
) -> hikari.Snowflake | None:
    voice_state = bot.cache.get_voice_state(hikari.Snowflake(guild_id), hikari.Snowflake(user_id))
    if voice_state is None or voice_state.channel_id is None:
        return None
    return hikari.Snowflake(voice_state.channel_id)


def cached_voice_channel_occupants(
    bot: hikari.GatewayBot,
    guild_id: hikari.Snowflakeish,
    channel_id: hikari.Snowflakeish,
    *,
    exclude_user_id: hikari.Snowflakeish | None = None,
) -> list[hikari.Snowflake]:
    me = bot.get_me()
    excluded_user = hikari.Snowflake(exclude_user_id) if exclude_user_id is not None else None

    occupants: list[hikari.Snowflake] = []
    for user_id in bot.cache.get_voice_states_view_for_channel(
        hikari.Snowflake(guild_id),
        hikari.Snowflake(channel_id),
    ):
        user = hikari.Snowflake(user_id)
        if me and user == me.id:
            continue
        if excluded_user is not None and user == excluded_user:
            continue
        occupants.append(user)
    return occupants


def wav_audio_duration_seconds(audio: bytes) -> float | None:
    if not audio:
        return None

    try:
        with wave.open(io.BytesIO(audio), "rb") as stream:
            frames = stream.getnframes()
            frame_rate = stream.getframerate()
    except (wave.Error, EOFError):
        return None

    if frames < 0 or frame_rate <= 0:
        return None
    return frames / frame_rate


def _patch_hikariwave_cache_state_update_bug() -> None:
    """Work around hikari-wave 0.7.0a1 cache bug calling __member_update without new_channel_id."""
    try:
        from hikariwave.impl import cache as hw_cache
    except Exception as xcp:
        log.warning(f"Voice workaround skipped: couldn't import hikariwave cache module: {xcp}")
        return

    state_name = "_Cache__state_update"
    member_name = "_Cache__member_update"
    state_update = getattr(hw_cache.Cache, state_name, None)
    member_update = getattr(hw_cache.Cache, member_name, None)
    if not callable(state_update) or not callable(member_update):
        return

    if "new_channel_id" not in inspect.signature(member_update).parameters:
        return
    if getattr(state_update, "__name__", "") == "_patched_state_update":
        return

    async def _patched_state_update(self: _HikariWaveCacheLike, event: hikari.VoiceStateUpdateEvent) -> None:
        state: hikari.VoiceState = event.state
        member: hikari.Member | None = state.member

        me = self._client._bot.get_me()
        if (me and state.user_id == me.id) or not member:
            return

        old_channel_id: hikari.Snowflake | None = self._members.get(member.id)
        new_channel_id: hikari.Snowflake | None = state.channel_id

        if not old_channel_id and new_channel_id:
            await self._Cache__member_join(member, state, new_channel_id)
        elif old_channel_id and new_channel_id and old_channel_id != new_channel_id:
            await self._Cache__member_move(member, old_channel_id, new_channel_id)
        elif old_channel_id and not new_channel_id:
            await self._Cache__member_leave(member, old_channel_id)
        elif old_channel_id and new_channel_id and old_channel_id == new_channel_id:
            await self._Cache__member_update(member, state, new_channel_id)

    setattr(hw_cache.Cache, state_name, _patched_state_update)
    log.warning("Applied hikari-wave cache state-update workaround")


async def _await_voice_connect_ready(
    ready_event: asyncio.Event,
    listener_task: asyncio.Task[None] | None,
) -> None:
    if ready_event.is_set():
        return

    if listener_task is None:
        await ready_event.wait()
        return

    if listener_task.done():
        listener_task.result()
        if ready_event.is_set():
            return
        raise RuntimeError("Voice gateway listener exited before the connection became ready.")

    ready_wait = asyncio.create_task(ready_event.wait(), name="voice-connect-ready")
    try:
        done, _ = await asyncio.wait({ready_wait, listener_task}, return_when=asyncio.FIRST_COMPLETED)
        if ready_wait in done:
            return

        listener_task.result()
        if ready_event.is_set():
            return
        raise RuntimeError("Voice gateway listener exited before the connection became ready.")
    finally:
        if not ready_wait.done():
            ready_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_wait


async def _cleanup_failed_voice_connect(connection: _VoiceConnectionLike) -> None:
    server = connection._server
    if server is not None:
        with contextlib.suppress(Exception):
            await server.disconnect()

    gateway = connection._gateway
    if gateway is not None:
        with contextlib.suppress(Exception):
            await gateway.disconnect()

    report_task = connection._report_task
    if report_task is None:
        return

    report_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await report_task
    connection._report_task = None


def _patch_hikariwave_connect_wait_hang() -> None:
    """Propagate gateway listener failures instead of hanging until outer voice connect timeouts."""
    try:
        from hikariwave.connection import ConnectionState, VoiceConnection
        from hikariwave.internal.constants import Constants
    except Exception as xcp:
        log.warning(f"Voice workaround skipped: couldn't import hikariwave connection module: {xcp}")
        return

    connect_name = "_connect"
    connect_obj = getattr(VoiceConnection, connect_name, None)
    if not callable(connect_obj):
        return
    if getattr(connect_obj, "__name__", "") == "_patched_connect":
        return

    async def _patched_connect(self: _VoiceConnectionLike) -> None:
        async with self._lock:
            if self._state != ConnectionState.DISCONNECTED:
                return

            self._ready.clear()
            self._state = ConnectionState.CONNECTING

            self._report_task = self._client._tasks.create(
                self._VoiceConnection__loop_reports(),
                name="connection-reports",
            )

            try:
                gateway = self._gateway
                if gateway is None:
                    raise RuntimeError("Voice connection gateway is not available.")
                await gateway.connect(f"{self._endpoint}/?v={Constants.GATEWAY_VERSION}")
                listener_task = gateway._task_listen
                await _await_voice_connect_ready(self._ready, listener_task)
                self._state = ConnectionState.CONNECTED
            except Exception:
                self._state = ConnectionState.DISCONNECTED
                await _cleanup_failed_voice_connect(self)
                logging.exception("Exception occurred while connecting to gateway")
                raise

    setattr(VoiceConnection, connect_name, _patched_connect)
    log.warning("Applied hikari-wave connect wait workaround")


def _patch_hikariwave_udp_discovery_timeout() -> None:
    """Retry UDP IP discovery and surface UDP socket failures instead of silent timeouts."""
    try:
        from hikariwave.internal.error import ServerError
        from hikariwave.networking.server import Protocol as VoiceProtocol, VoiceServer
    except Exception as xcp:
        log.warning(f"Voice workaround skipped: couldn't import hikariwave voice server module: {xcp}")
        return

    discover_name = "_discover_ip"
    discover_ip_obj = getattr(VoiceServer, discover_name, None)
    if not callable(discover_ip_obj):
        return
    if getattr(discover_ip_obj, "__name__", "") == "_patched_discover_ip":
        return
    discover_ip = cast(Callable[[_VoiceServerLike], Awaitable[tuple[str, int]]], discover_ip_obj)

    def _close_udp_transport(server: _VoiceServerLike) -> None:
        udp = server._udp
        if udp is None:
            return
        with contextlib.suppress(Exception):
            udp.close()
        server._udp = None

    async def _discover_ip_once(self: _VoiceServerLike) -> tuple[str, int]:
        ip = self._ip
        port = self._port
        ssrc = self._ssrc
        if not ip or port is None or ssrc is None:
            raise RuntimeError("Voice UDP discovery requires IP, port, and SSRC to be populated.")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        rtp_listener = cast(Callable[[int], None], cast(object, self._rtp_packet))

        udp, _ = await loop.create_datagram_endpoint(
            lambda: VoiceProtocol(future, rtp_listener),
            remote_addr=(ip, port),
        )
        self._udp = cast(_UdpTransportLike, udp)

        packet = struct.pack(
            "!HHI",
            _VOICE_UDP_DISCOVERY_REQUEST_TYPE,
            _VOICE_UDP_DISCOVERY_PACKET_LENGTH,
            ssrc,
        ) + bytes(_VOICE_UDP_DISCOVERY_PACKET_LENGTH)
        udp.sendto(packet)

        data = await asyncio.wait_for(future, _VOICE_UDP_DISCOVERY_TIMEOUT_SECONDS)
        if len(data) < _VOICE_UDP_DISCOVERY_RESPONSE_MIN_LENGTH:
            raise ServerError(
                "Expected IP discovery packet "
                f"of at least {_VOICE_UDP_DISCOVERY_RESPONSE_MIN_LENGTH} bytes, got {len(data)}"
            )

        packet_type = cast(int, struct.unpack("!H", data[0:2])[0])
        if packet_type != _VOICE_UDP_DISCOVERY_RESPONSE_TYPE:
            raise ServerError(f"Expected packet type {_VOICE_UDP_DISCOVERY_RESPONSE_TYPE}, got {packet_type}")

        packet_length = cast(int, struct.unpack("!H", data[2:4])[0])
        if packet_length != _VOICE_UDP_DISCOVERY_PACKET_LENGTH:
            raise ServerError(
                f"Expected packet length of {_VOICE_UDP_DISCOVERY_PACKET_LENGTH}, got {packet_length}"
            )

        external_ip = data[8:72].split(b"\x00", 1)[0].decode("ascii")
        external_port = cast(int, struct.unpack("!H", data[72:74])[0])
        return external_ip, external_port

    async def _patched_discover_ip(self: _VoiceServerLike) -> tuple[str, int]:
        last_timeout: asyncio.TimeoutError | None = None
        last_network_error: OSError | None = None
        for attempt in range(1, _VOICE_UDP_DISCOVERY_ATTEMPTS + 1):
            attempt_failed = False
            try:
                return await _discover_ip_once(self)
            except asyncio.TimeoutError as xcp:
                attempt_failed = True
                last_timeout = xcp
                log.warning(
                    f"Voice UDP discovery timeout attempt={attempt}/{_VOICE_UDP_DISCOVERY_ATTEMPTS} "
                    f"ip={getattr(self, '_ip', None)!r} port={getattr(self, '_port', None)!r}"
                )
            except OSError as xcp:
                attempt_failed = True
                last_network_error = xcp
                log.warning(
                    f"Voice UDP discovery socket failure attempt={attempt}/{_VOICE_UDP_DISCOVERY_ATTEMPTS} "
                    f"ip={getattr(self, '_ip', None)!r} port={getattr(self, '_port', None)!r} "
                    f"error={type(xcp).__name__}: {xcp}"
                )
            finally:
                if attempt_failed and self._udp is not None:
                    _close_udp_transport(self)

            if attempt < _VOICE_UDP_DISCOVERY_ATTEMPTS:
                await asyncio.sleep(_VOICE_UDP_DISCOVERY_RETRY_DELAY_STEP_SECONDS * attempt)

        if last_timeout:
            raise VoiceUdpDiscoveryTimeoutError(
                ip=getattr(self, "_ip", None),
                port=getattr(self, "_port", None),
                attempts=_VOICE_UDP_DISCOVERY_ATTEMPTS,
            ) from last_timeout
        if last_network_error:
            raise VoiceUdpDiscoveryNetworkError(
                ip=getattr(self, "_ip", None),
                port=getattr(self, "_port", None),
                attempts=_VOICE_UDP_DISCOVERY_ATTEMPTS,
                cause=last_network_error,
            ) from last_network_error
        return await discover_ip(self)

    setattr(VoiceServer, discover_name, _patched_discover_ip)
    log.warning("Applied hikari-wave UDP discovery timeout workaround")


def _patch_hikariwave_udp_protocol_error_bug() -> None:
    """Propagate UDP socket errors to discovery callers instead of stalling until timeout."""
    try:
        from hikariwave.networking.server import Protocol as VoiceProtocol
    except Exception as xcp:
        log.warning(f"Voice workaround skipped: couldn't import hikariwave voice protocol module: {xcp}")
        return

    error_received_name = "error_received"
    error_received_obj = getattr(VoiceProtocol, error_received_name, None)
    if not callable(error_received_obj):
        return
    if getattr(error_received_obj, "__name__", "") == "_patched_error_received":
        return

    def _patched_error_received(self: object, exc: Exception) -> None:
        future = cast(asyncio.Future[bytes] | None, getattr(self, "_ip_discover_future", None))
        if future is None or future.done():
            return
        future.set_exception(exc)

    setattr(VoiceProtocol, error_received_name, _patched_error_received)
    log.warning("Applied hikari-wave UDP discovery socket-error workaround")


def _patch_hikariwave_player_idle_queue_race() -> None:
    """Restart queued playback if hikari-wave leaves audio queued on an idle player."""
    try:
        from hikariwave.audio.player import AudioPlaybackState, AudioPlayer
        from hikariwave.internal.result import Result
    except Exception as xcp:
        log.warning(f"Voice workaround skipped: couldn't import hikariwave audio player module: {xcp}")
        return

    add_queue_name = "add_queue"
    add_queue_bulk_name = "add_queue_bulk"
    add_queue_obj = getattr(AudioPlayer, add_queue_name, None)
    add_queue_bulk_obj = getattr(AudioPlayer, add_queue_bulk_name, None)
    if not callable(add_queue_obj) or not callable(add_queue_bulk_obj):
        return

    if getattr(add_queue_obj, "__name__", "") == "_patched_add_queue":
        return

    add_queue = cast(Callable[..., Awaitable[Result]], add_queue_obj)
    add_queue_bulk = cast(Callable[..., Awaitable[Result]], add_queue_bulk_obj)

    def _queue_stuck(player: AudioPlayer) -> bool:
        return player.state == AudioPlaybackState.IDLE and player.current is None and bool(player.queue)

    async def _repair_idle_queue(player: AudioPlayer) -> None:
        if not _queue_stuck(player):
            return

        for delay_seconds in (0.0, 0.0, 0.02):
            await asyncio.sleep(delay_seconds)
            if not _queue_stuck(player):
                return

        lock = cast(asyncio.Lock | None, getattr(player, "_lock", None))
        if lock is None:
            return

        async with lock:
            if not _queue_stuck(player):
                return

            task = cast(asyncio.Task[None] | None, getattr(player, "_player_task", None))
            previous_task_state = "missing"
            if task is not None:
                previous_task_state = "done" if task.done() else "pending"
                if not task.done():
                    task.cancel()

            setattr(player, "_player_task", None)
            ensure_loop = cast(Callable[[bool | None], None] | None, getattr(player, "_AudioPlayer__ensure_loop", None))
            if not callable(ensure_loop):
                return

            ensure_loop(True)
            log.warning(
                "Recovered stuck hikari-wave player loop "
                f"state={getattr(player.state, 'name', player.state)} queue_len={len(player.queue)} "
                f"previous_task={previous_task_state}"
            )

    async def _patched_add_queue(self: AudioPlayer, source: object, *, autoplay: bool = True) -> Result:
        result = await add_queue(self, source, autoplay=autoplay)
        if result.success and autoplay:
            await _repair_idle_queue(self)
        return result

    async def _patched_add_queue_bulk(self: AudioPlayer, sources: object, *, autoplay: bool = True) -> Result:
        result = await add_queue_bulk(self, sources, autoplay=autoplay)
        if result.success and autoplay:
            await _repair_idle_queue(self)
        return result

    setattr(AudioPlayer, add_queue_name, _patched_add_queue)
    setattr(AudioPlayer, add_queue_bulk_name, _patched_add_queue_bulk)
    log.warning("Applied hikari-wave idle queue restart workaround")


apply_hikariwave_patches()
# AiviA APasz
