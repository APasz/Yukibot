from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import logging
import wave
from typing import Awaitable, Callable, cast

import hikari

log = logging.getLogger(__name__)

_PATCHES_APPLIED = False


class VoiceUdpDiscoveryTimeoutError(asyncio.TimeoutError):
    def __init__(self, *, ip: str | None, port: int | None, attempts: int):
        self.ip = ip
        self.port = port
        self.attempts = attempts
        super().__init__(f"Voice UDP discovery timed out after {attempts} attempts (ip={ip!r} port={port!r})")


def apply_hikariwave_patches() -> None:
    global _PATCHES_APPLIED
    if _PATCHES_APPLIED:
        return

    _patch_hikariwave_cache_state_update_bug()
    _patch_hikariwave_udp_discovery_timeout()
    _patch_hikariwave_player_idle_queue_race()
    _PATCHES_APPLIED = True


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

    async def _patched_state_update(self, event: hikari.VoiceStateUpdateEvent) -> None:
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


def _patch_hikariwave_udp_discovery_timeout() -> None:
    """Retry UDP IP discovery to avoid transient 3s timeout failures during voice connect."""
    try:
        from hikariwave.networking.server import VoiceServer
    except Exception as xcp:
        log.warning(f"Voice workaround skipped: couldn't import hikariwave voice server module: {xcp}")
        return

    discover_name = "_discover_ip"
    discover_ip_obj = getattr(VoiceServer, discover_name, None)
    if not callable(discover_ip_obj):
        return
    if getattr(discover_ip_obj, "__name__", "") == "_patched_discover_ip":
        return
    discover_ip = cast(Callable[[object], Awaitable[tuple[str, int]]], discover_ip_obj)

    async def _patched_discover_ip(self):
        last_timeout: asyncio.TimeoutError | None = None
        for attempt in range(1, 4):
            try:
                return await discover_ip(self)
            except asyncio.TimeoutError as xcp:
                last_timeout = xcp
                log.warning(
                    f"Voice UDP discovery timeout attempt={attempt}/3 "
                    f"ip={getattr(self, '_ip', None)!r} port={getattr(self, '_port', None)!r}"
                )
                udp = getattr(self, "_udp", None)
                if udp:
                    with contextlib.suppress(Exception):
                        udp.close()
                    setattr(self, "_udp", None)
                if attempt < 3:
                    await asyncio.sleep(0.25 * attempt)

        if last_timeout:
            raise VoiceUdpDiscoveryTimeoutError(
                ip=getattr(self, "_ip", None),
                port=getattr(self, "_port", None),
                attempts=3,
            ) from last_timeout
        raise VoiceUdpDiscoveryTimeoutError(
            ip=getattr(self, "_ip", None),
            port=getattr(self, "_port", None),
            attempts=3,
        )

    setattr(VoiceServer, discover_name, _patched_discover_ip)
    log.warning("Applied hikari-wave UDP discovery timeout workaround")


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
