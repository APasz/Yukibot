from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import re
import shutil
import tempfile
from collections import deque
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import ParseResult, unquote, urlparse

import hikari
import hikariwave
import lightbulb
from hikariwave.audio.source.base import AudioSource, validate_duration, validate_name
from hikariwave.audio.source.url import URLAudioSource
from hikariwave.audio.source.youtube import YouTubeAudioSource
from hikariwave.config import validate_volume
from yt_dlp.YoutubeDL import YoutubeDL as YT

import config
from _security import Access_Control
from voice_common import cached_user_voice_channel, cached_voice_channel_occupants, wav_audio_duration_seconds

log = logging.getLogger(__name__)

group_music = lightbulb.Group("music", "Music playback")  # type: ignore

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)


class MusicSourceKind(enum.StrEnum):
    URL = "url"
    YOUTUBE = "youtube"


class PipeAudioSource(AudioSource):
    __slots__ = (
        "_content",
        "_duration",
        "_bitrate",
        "_channels",
        "_name",
        "_volume",
    )

    def __init__(
        self,
        pipe_path: str,
        duration: float | None = None,
        *,
        name: str | None = None,
        volume: float | str | None = None,
    ) -> None:
        if not pipe_path:
            raise ValueError("Provided pipe_path cannot be empty")
        self._content = pipe_path
        self._duration: float | None = validate_duration(duration) if duration is not None else None
        self._bitrate: str | None = None
        self._channels: int | None = None
        self._name: str | None = validate_name(name) if name is not None else None
        self._volume: float | str | None = validate_volume(volume) if volume is not None else None


class ConfiguredYouTubeAudioSource(YouTubeAudioSource):
    __slots__ = ("_yt_cookiefile", "_yt_extractor_args")

    def __init__(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        extractor_args: Mapping[str, Sequence[str]] | None = None,
        bitrate: str | None = None,
        channels: int | None = None,
        name: str | None = None,
        volume: float | str | None = None,
    ) -> None:
        self._yt_cookiefile = str(cookiefile) if cookiefile is not None else None
        self._yt_extractor_args = {
            key: tuple(value for value in values if value)
            for key, values in (extractor_args or {}).items()
            if key and any(values)
        }
        super().__init__(
            url,
            bitrate=bitrate,
            channels=channels,
            name=name,
            volume=volume,
        )

    async def _extract_media(self) -> None:
        def extract() -> dict[str, Any]:
            with self._new_yt_dlp(self._yt_dlp_options(metadata_only=False)) as ydl:
                return self._extract_info_dict(ydl, self._url)

        info: dict[str, Any] = await asyncio.to_thread(extract)

        self._content = info["url"]
        self._headers = self._info_headers(info)
        self._metadata = info
        self._duration = self._info_duration(info)

    async def _extract_metadata(self) -> None:
        def extract() -> dict[str, Any]:
            with self._new_yt_dlp(self._yt_dlp_options(metadata_only=True)) as ydl:
                return self._extract_info_dict(ydl, self._url)

        info: dict[str, Any] = await asyncio.to_thread(extract)

        self._metadata = info
        self._duration = self._info_duration(info)

    def _yt_dlp_options(self, *, metadata_only: bool) -> dict[str, object]:
        options: dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        if self._yt_cookiefile:
            options["cookiefile"] = self._yt_cookiefile
        if self._yt_extractor_args:
            options["extractor_args"] = {
                "youtube": {key: list(values) for key, values in self._yt_extractor_args.items()}
            }

        if metadata_only:
            options.update(
                {
                    "extract_flat": True,
                    "simulate": True,
                    "skip_download": True,
                }
            )
        else:
            options.update(
                {
                    "format": "bestaudio/best",
                    "extract_flat": False,
                    "retries": 3,
                    "fragment_retries": 3,
                    "skip_unavailable_fragments": True,
                    "rm_cache_dir": True,
                    "player_js_variant": "main",
                }
            )

        return options

    @staticmethod
    def _new_yt_dlp(options: Mapping[str, object]) -> YT:
        return YT(cast(Any, dict(options)))

    @staticmethod
    def _extract_info_dict(ydl: YT, url: str) -> dict[str, Any]:
        return cast(dict[str, Any], ydl.extract_info(url, False))

    @staticmethod
    def _info_duration(info: Mapping[str, Any]) -> float | None:
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            return float(duration)
        return None

    @staticmethod
    def _info_headers(info: Mapping[str, Any]) -> dict[str, str]:
        raw_headers = info.get("http_headers")
        if not isinstance(raw_headers, Mapping):
            return {}
        headers: dict[str, str] = {}
        for key, value in raw_headers.items():
            if isinstance(key, str) and isinstance(value, str):
                headers[key] = value
        return headers


@dataclass(slots=True)
class GeneratedSourceHandle:
    source: AudioSource
    directory: Path
    pipe_path: Path
    writer_task: asyncio.Task[None]


@dataclass(slots=True, frozen=True)
class MusicTrack:
    requestor_id: hikari.Snowflake
    source_text: str
    display_name: str
    base_display_name: str
    source_kind: MusicSourceKind
    audio_source: AudioSource
    duration_seconds: float | None = None
    seek_seconds: float = 0.0


@dataclass(slots=True)
class GuildMusicSession:
    guild_id: hikari.Snowflake
    channel_id: hikari.Snowflake
    tracks: deque[MusicTrack] = field(default_factory=deque)
    current_source: AudioSource | None = None
    volume: float = 1.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def current_track(self) -> MusicTrack | None:
        return self.tracks[0] if self.tracks else None

    def queued_tracks(self) -> list[MusicTrack]:
        return list(self.tracks)[1:]


@dataclass(slots=True, frozen=True)
class MusicResetResult:
    session_count: int
    track_count: int
    managed_source_count: int


class MusicService:
    _QUEUE_PREVIEW_LIMIT = 10
    _DUCKED_MUSIC_VOLUME = 0.35

    def __init__(self, bot: hikari.GatewayBot, voice_client: hikariwave.VoiceClient):
        self.bot = bot
        self._voice_client = voice_client
        self._sessions: dict[hikari.Snowflake, GuildMusicSession] = {}
        self._managed_sources: dict[int, GeneratedSourceHandle] = {}
        self._ffmpeg_path = shutil.which("ffmpeg")
        self._youtube_cookie_file = config.MUSIC_YTDLP_COOKIE_FILE
        self._youtube_extractor_args = self._parse_youtube_extractor_args(config.MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS)

    async def setup(self, client: lightbulb.Client | None = None):
        del client
        if not self._ffmpeg_path:
            log.warning("Music playback may fail: ffmpeg is not available in PATH.")
        if self._youtube_cookie_file is None:
            log.info("Music YouTube auth: no cookie file configured")
        elif self._youtube_cookie_file.exists():
            log.info(f"Music YouTube auth: cookie file enabled path={self._youtube_cookie_file}")
        else:
            log.warning(f"Music YouTube auth: configured cookie file does not exist path={self._youtube_cookie_file}")
        if self._youtube_extractor_args:
            keys = ",".join(sorted(self._youtube_extractor_args))
            log.info(f"Music YouTube extractor args enabled keys={keys}")
        log.info("Music playback ready")

    async def close(self):
        for guild_id in list(self._sessions):
            await self._disconnect_session(guild_id, reason="service_close")
        for handle in list(self._managed_sources.values()):
            await self._cleanup_generated_source_handle(handle)
        self._managed_sources.clear()

    def active_guild_ids(self) -> list[hikari.Snowflake]:
        return sorted(self._sessions, key=int)

    async def reset_runtime(self) -> MusicResetResult:
        result = MusicResetResult(
            session_count=len(self._sessions),
            track_count=sum(len(session.tracks) for session in self._sessions.values()),
            managed_source_count=len(self._managed_sources),
        )
        await self.close()
        log.warning(
            "Music runtime reset session_count=%s track_count=%s managed_sources=%s",
            result.session_count,
            result.track_count,
            result.managed_source_count,
        )
        return result

    def get_connection(self, guild_id: hikari.Snowflakeish) -> hikariwave.VoiceConnection | None:
        return self._voice_client.get_connection(guild_id=guild_id)

    def active_channel_id(self, guild_id: hikari.Snowflakeish) -> hikari.Snowflake | None:
        session = self._sessions.get(hikari.Snowflake(guild_id))
        if session is not None and session.tracks:
            return session.channel_id
        return None

    def has_active_music(self, guild_id: hikari.Snowflakeish) -> bool:
        session = self._sessions.get(hikari.Snowflake(guild_id))
        if session is not None and session.tracks:
            return True
        connection = self.get_connection(guild_id)
        return bool(connection and (connection.player.current or connection.player.queue))

    def author_voice_channel(
        self,
        guild_id: hikari.Snowflakeish,
        user_id: hikari.Snowflakeish,
    ) -> hikari.Snowflake | None:
        return cached_user_voice_channel(self.bot, guild_id, user_id)

    def session(self, guild_id: hikari.Snowflakeish) -> GuildMusicSession | None:
        return self._sessions.get(hikari.Snowflake(guild_id))

    async def enqueue(
        self,
        guild_id: hikari.Snowflakeish,
        channel_id: hikari.Snowflakeish,
        source_text: str,
        *,
        requestor_id: hikari.Snowflakeish,
    ) -> tuple[MusicTrack, int]:
        guild = hikari.Snowflake(guild_id)
        target_channel = hikari.Snowflake(channel_id)
        requester = hikari.Snowflake(requestor_id)

        track = await self._build_track(source_text, requester)
        session = self._sessions.get(guild)
        if session is None:
            session = GuildMusicSession(guild_id=guild, channel_id=target_channel)
            self._sessions[guild] = session

        disconnect_empty_session = False
        try:
            async with session.lock:
                try:
                    connection = await self._ensure_connection(session, target_channel)
                    had_tracks = bool(session.tracks)
                    session.tracks.append(track)
                    if not had_tracks:
                        session.current_source = track.audio_source

                    connection.player.set_volume(session.volume)
                    result = await connection.player.add_queue(track.audio_source)
                    if not result.success:
                        session.tracks.pop()
                        if not session.tracks:
                            session.current_source = None
                        raise RuntimeError(f"Music queue rejected the track ({result.reason}).")

                    queue_position = len(session.tracks) - 1
                    log.info(
                        f"Music queued guild={guild} channel={target_channel} position={queue_position} "
                        f"kind={track.source_kind} name={track.display_name!r} requester={requester}"
                    )
                    return track, queue_position
                except Exception:
                    disconnect_empty_session = not session.tracks
                    raise
        except Exception:
            if disconnect_empty_session:
                await self._disconnect_session(guild, reason="queue_rejected")
            raise

    async def pause(self, guild_id: hikari.Snowflakeish) -> None:
        connection = self.get_connection(guild_id)
        if connection is None:
            raise RuntimeError("Music is not connected in this server.")

        result = await connection.player.pause()
        if not result.success:
            raise RuntimeError(f"Music pause failed ({result.reason}).")

    async def resume(self, guild_id: hikari.Snowflakeish) -> None:
        connection = self.get_connection(guild_id)
        if connection is None:
            raise RuntimeError("Music is not connected in this server.")

        result = await connection.player.resume()
        if not result.success:
            raise RuntimeError(f"Music resume failed ({result.reason}).")

    async def skip(self, guild_id: hikari.Snowflakeish) -> None:
        guild = hikari.Snowflake(guild_id)
        session = self._sessions.get(guild)
        connection = self.get_connection(guild)
        if connection is None or session is None or not session.tracks:
            raise RuntimeError("Music queue is empty in this server.")

        async with session.lock:
            current_tracks = list(session.tracks)
            is_last_track = len(session.tracks) == 1
            if is_last_track:
                result = await connection.player.stop()
            else:
                result = await connection.player.next()

            if not result.success:
                raise RuntimeError(f"Music skip failed ({result.reason}).")

            if is_last_track:
                session.tracks.clear()
                session.current_source = None

        if is_last_track:
            await self._cleanup_tracks(current_tracks)
            await self._disconnect_session(guild, reason="skip_last_track")

    async def stop(self, guild_id: hikari.Snowflakeish) -> None:
        guild = hikari.Snowflake(guild_id)
        session = self._sessions.get(guild)
        if session is None and self.get_connection(guild) is None:
            raise RuntimeError("Music is not active in this server.")

        tracks_to_cleanup: list[MusicTrack] = []
        if session is not None:
            async with session.lock:
                tracks_to_cleanup = list(session.tracks)
                await self._stop_player(guild)
                session.tracks.clear()
                session.current_source = None
        else:
            await self._stop_player(guild)

        await self._cleanup_tracks(tracks_to_cleanup)
        await self._disconnect_session(guild, reason="stop_command")

    async def set_volume(self, guild_id: hikari.Snowflakeish, volume: float) -> float:
        guild = hikari.Snowflake(guild_id)
        if volume <= 0:
            raise ValueError("volume must be greater than 0")

        session = self._sessions.get(guild)
        if session is None:
            raise RuntimeError("Music is not active in this server.")

        async with session.lock:
            session.volume = volume
            if connection := self.get_connection(guild):
                connection.player.set_volume(volume)
        return volume

    def volume(self, guild_id: hikari.Snowflakeish) -> float:
        session = self._sessions.get(hikari.Snowflake(guild_id))
        return session.volume if session is not None else 1.0

    def now_playing(self, guild_id: hikari.Snowflakeish) -> tuple[hikari.Snowflake, MusicTrack] | None:
        guild = hikari.Snowflake(guild_id)
        session = self._sessions.get(guild)
        if session is None:
            return None

        track = session.current_track()
        if track is None:
            return None
        return session.channel_id, track

    def queue_snapshot(
        self,
        guild_id: hikari.Snowflakeish,
    ) -> tuple[hikari.Snowflake, MusicTrack | None, list[MusicTrack]]:
        guild = hikari.Snowflake(guild_id)
        session = self._sessions.get(guild)
        if session is None:
            raise RuntimeError("Music is not active in this server.")
        return session.channel_id, session.current_track(), session.queued_tracks()

    async def duck_tts_playback(
        self,
        guild_id: hikari.Snowflakeish,
        message_id: hikari.Snowflakeish,
        audio: bytes,
    ) -> tuple[hikariwave.VoiceConnection, AudioSource] | None:
        guild = hikari.Snowflake(guild_id)
        session = self._sessions.get(guild)
        connection = self.get_connection(guild)
        if connection is None or session is None or not session.tracks:
            return None
        if not self._ffmpeg_path:
            log.warning(f"TTS ducking unavailable guild={guild} reason=no_ffmpeg")
            return None

        tts_duration = wav_audio_duration_seconds(audio)
        if tts_duration is None or tts_duration <= 0:
            log.warning(f"TTS ducking unavailable guild={guild} reason=unknown_tts_duration")
            return None

        old_tracks: list[MusicTrack] = []
        replacement_tracks: list[MusicTrack] | None = None
        overlay_result: MusicTrack | None = None
        failure_reason: str | None = None
        disconnect_after_failure = False

        async with session.lock:
            current_audio = connection.player.current
            if current_audio is None:
                return None
            current_track = self._align_session_to_current_audio(session, current_audio)
            if current_track is None:
                log.warning(f"TTS ducking unavailable guild={guild} reason=player_track_desync")
                return None

            absolute_seek = current_track.seek_seconds + max(connection.player.elapsed, 0.0)
            remaining_tracks = self._duck_remainder_tracks(current_track, session.queued_tracks())
            old_tracks = list(session.tracks)

            generated_tracks: list[MusicTrack] = []
            try:
                overlay_track = await self._build_duck_overlay_track(
                    current_track,
                    audio,
                    seek_seconds=absolute_seek,
                    overlay_duration=tts_duration,
                    message_id=hikari.Snowflake(message_id),
                )
                generated_tracks.append(overlay_track)
                if self._should_resume_track(current_track, absolute_seek, tts_duration):
                    resume_track = await self._build_resumed_track(
                        current_track,
                        seek_seconds=absolute_seek + tts_duration,
                        message_id=hikari.Snowflake(message_id),
                    )
                    generated_tracks.append(resume_track)
                else:
                    resume_track = None
            except Exception:
                await self._cleanup_tracks(generated_tracks)
                raise

            rebuilt_tracks = [overlay_track]
            if resume_track is not None:
                rebuilt_tracks.append(resume_track)
            rebuilt_tracks.extend(remaining_tracks)

            await self._stop_player(guild)
            connection.player.set_volume(session.volume)
            result = await connection.player.add_queue_bulk([track.audio_source for track in rebuilt_tracks])
            if not result.success:
                await self._cleanup_tracks([overlay_track, *([resume_track] if resume_track is not None else [])])
                replacement_tracks = await self._restore_ducked_queue(
                    connection,
                    session,
                    current_track,
                    remaining_tracks,
                    absolute_seek=absolute_seek,
                    message_id=hikari.Snowflake(message_id),
                )
                disconnect_after_failure = replacement_tracks is None
                failure_reason = str(result.reason)
            else:
                session.tracks.clear()
                session.tracks.extend(rebuilt_tracks)
                session.current_source = overlay_track.audio_source
                replacement_tracks = rebuilt_tracks
                overlay_result = overlay_track

        if replacement_tracks is not None:
            await self._cleanup_replaced_tracks(old_tracks, replacement_tracks)
        if disconnect_after_failure:
            await self._disconnect_session(guild, reason="duck_rebuild_failed")
        if failure_reason is not None:
            raise RuntimeError(f"Music duck rebuild failed ({failure_reason}).")
        if overlay_result is None:
            return None

        log.info(
            f"TTS ducked into music guild={guild} message_id={message_id} "
            f"track={overlay_result.base_display_name!r} offset={absolute_seek:.2f}s duration={tts_duration:.2f}s"
        )
        return connection, overlay_result.audio_source

    @staticmethod
    def _duck_remainder_tracks(current_track: MusicTrack, queued_tracks: list[MusicTrack]) -> list[MusicTrack]:
        remaining_tracks = list(queued_tracks)
        if (
            remaining_tracks
            and remaining_tracks[0].source_text == current_track.source_text
            and remaining_tracks[0].base_display_name == current_track.base_display_name
            and remaining_tracks[0].seek_seconds > current_track.seek_seconds
        ):
            remaining_tracks.pop(0)
        return remaining_tracks

    def _align_session_to_current_audio(
        self,
        session: GuildMusicSession,
        current_audio: AudioSource,
    ) -> MusicTrack | None:
        for index, track in enumerate(session.tracks):
            if track.audio_source is current_audio:
                while index > 0:
                    dropped = session.tracks.popleft()
                    log.warning(
                        f"Music queue realigned guild={session.guild_id} "
                        f"dropped_track={dropped.display_name!r} reason=current_audio_out_of_order"
                    )
                    index -= 1
                session.current_source = current_audio
                return session.current_track()
        return None

    async def _restore_ducked_queue(
        self,
        connection: hikariwave.VoiceConnection,
        session: GuildMusicSession,
        current_track: MusicTrack,
        remaining_tracks: list[MusicTrack],
        *,
        absolute_seek: float,
        message_id: hikari.Snowflake,
    ) -> list[MusicTrack] | None:
        restored_tracks = list(remaining_tracks)
        resumed_track: MusicTrack | None = None
        restored = False
        try:
            if self._should_resume_track(current_track, absolute_seek, 0.0):
                resumed_track = await self._build_resumed_track(
                    current_track,
                    seek_seconds=absolute_seek,
                    message_id=message_id,
                )
                restored_tracks.insert(0, resumed_track)

            if not restored_tracks:
                return None

            connection.player.set_volume(session.volume)
            result = await connection.player.add_queue_bulk([track.audio_source for track in restored_tracks])
            if not result.success:
                return None

            session.tracks.clear()
            session.tracks.extend(restored_tracks)
            session.current_source = restored_tracks[0].audio_source
            restored = True
            log.warning(
                f"Music duck rollback restored guild={session.guild_id} "
                f"track_count={len(restored_tracks)} offset={absolute_seek:.2f}s"
            )
            return restored_tracks
        except Exception:
            log.exception(f"Music duck rollback failed guild={session.guild_id}")
            return None
        finally:
            if resumed_track is not None and not restored:
                await self._cleanup_generated_source(resumed_track.audio_source)

    async def on_audio_begin(self, event: hikariwave.AudioBeginEvent):
        session = self._sessions.get(hikari.Snowflake(event.guild_id))
        if session is None:
            return

        async with session.lock:
            for index, track in enumerate(session.tracks):
                if track.audio_source is event.audio:
                    while index > 0:
                        dropped = session.tracks.popleft()
                        log.warning(
                            f"Music queue realigned guild={event.guild_id} "
                            f"dropped_track={dropped.display_name!r} reason=audio_begin_out_of_order"
                        )
                        index -= 1
                    session.current_source = event.audio
                    return

    async def on_audio_end(self, event: hikariwave.AudioEndEvent):
        guild = hikari.Snowflake(event.guild_id)
        session = self._sessions.get(guild)
        if session is None:
            await self._cleanup_generated_source(event.audio)
            return

        should_disconnect = False
        async with session.lock:
            ended_index: int | None = None
            for index, track in enumerate(session.tracks):
                if track.audio_source is event.audio:
                    ended_index = index
                    break

            if ended_index is None:
                if session.current_source is event.audio:
                    session.current_source = session.tracks[0].audio_source if session.tracks else None
            else:
                if ended_index == 0:
                    session.tracks.popleft()
                else:
                    del session.tracks[ended_index]

                session.current_source = session.tracks[0].audio_source if session.tracks else None
                should_disconnect = not session.tracks

        await self._cleanup_generated_source(event.audio)
        if should_disconnect:
            await self._disconnect_session(guild, reason="queue_empty")

    async def on_voice_state_update(self, event: hikari.VoiceStateUpdateEvent):
        if event.guild_id is None:
            return

        session = self._sessions.get(hikari.Snowflake(event.guild_id))
        if session is None:
            return

        me = self.bot.get_me()
        if me and event.state.user_id == me.id:
            return

        old_channel_id = event.old_state.channel_id if event.old_state else None
        if old_channel_id != session.channel_id:
            return
        if event.state.channel_id == session.channel_id:
            return

        occupants = cached_voice_channel_occupants(
            self.bot,
            event.guild_id,
            session.channel_id,
            exclude_user_id=event.state.user_id,
        )
        if occupants:
            return

        await self._disconnect_session(event.guild_id, reason="channel_empty")

    async def _build_track(self, source_text: str, requestor_id: hikari.Snowflake) -> MusicTrack:
        source = source_text.strip()
        if not source:
            raise ValueError("source must not be empty")

        parsed = urlparse(source)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only http(s) URLs are supported for music playback right now.")

        source_kind = self._source_kind(parsed)
        base_display_name = self._display_name_from_url(parsed)
        display_name = base_display_name
        duration_seconds: float | None = None
        try:
            if source_kind is MusicSourceKind.YOUTUBE:
                audio_source = self._new_youtube_audio_source(source, name=display_name)
                metadata = await audio_source.resolve_metadata()
                title = metadata.get("title")
                if isinstance(title, str) and title.strip():
                    display_name = title.strip()
                duration_seconds = audio_source.duration
            else:
                audio_source = URLAudioSource(source, name=display_name)
        except Exception as xcp:
            raise RuntimeError(self._music_source_error(source_kind, xcp)) from xcp

        return MusicTrack(
            requestor_id=requestor_id,
            source_text=source,
            display_name=display_name,
            base_display_name=display_name,
            source_kind=source_kind,
            audio_source=audio_source,
            duration_seconds=duration_seconds,
        )

    async def _build_duck_overlay_track(
        self,
        track: MusicTrack,
        tts_audio: bytes,
        *,
        seek_seconds: float,
        overlay_duration: float,
        message_id: hikari.Snowflake,
    ) -> MusicTrack:
        music_input = await self._resolved_media_input(track)
        source = await self._create_pipe_source(
            name=f"duck-{message_id}",
            duration=overlay_duration,
            writer=self._write_duck_overlay(
                music_input,
                tts_audio,
                seek_seconds=seek_seconds,
                overlay_duration=overlay_duration,
            ),
        )
        return MusicTrack(
            requestor_id=track.requestor_id,
            source_text=track.source_text,
            display_name=track.base_display_name,
            base_display_name=track.base_display_name,
            source_kind=track.source_kind,
            audio_source=source,
            duration_seconds=overlay_duration,
            seek_seconds=seek_seconds,
        )

    async def _build_resumed_track(
        self,
        track: MusicTrack,
        *,
        seek_seconds: float,
        message_id: hikari.Snowflake,
    ) -> MusicTrack:
        music_input = await self._resolved_media_input(track)
        source = await self._create_pipe_source(
            name=f"resume-{message_id}",
            duration=None if track.duration_seconds is None else max(track.duration_seconds - seek_seconds, 0.0),
            writer=self._write_resumed_music(music_input, seek_seconds=seek_seconds),
        )
        resumed_duration = None if track.duration_seconds is None else max(track.duration_seconds - seek_seconds, 0.0)
        return MusicTrack(
            requestor_id=track.requestor_id,
            source_text=track.source_text,
            display_name=f"{track.base_display_name} (continued)",
            base_display_name=track.base_display_name,
            source_kind=track.source_kind,
            audio_source=source,
            duration_seconds=resumed_duration,
            seek_seconds=seek_seconds,
        )

    async def _resolved_media_input(self, track: MusicTrack) -> str:
        if track.source_kind is MusicSourceKind.URL:
            return track.source_text

        if isinstance(track.audio_source, YouTubeAudioSource):
            return await track.audio_source.resolve_media()

        source = self._new_youtube_audio_source(track.source_text, name=track.base_display_name)
        return await source.resolve_media()

    def _new_youtube_audio_source(self, url: str, *, name: str) -> ConfiguredYouTubeAudioSource:
        return ConfiguredYouTubeAudioSource(
            url,
            name=name,
            cookiefile=self._existing_youtube_cookie_file(),
            extractor_args=self._youtube_extractor_args,
        )

    def _existing_youtube_cookie_file(self) -> Path | None:
        if self._youtube_cookie_file is None or not self._youtube_cookie_file.exists():
            return None
        return self._youtube_cookie_file

    def _music_source_error(self, source_kind: MusicSourceKind, error: Exception) -> str:
        message = f"Unable to load that music source: {type(error).__name__}: {error}"
        if source_kind is not MusicSourceKind.YOUTUBE:
            return message

        detail = str(error)
        if "confirm you're not a bot" in detail or "Sign in to confirm" in detail:
            if self._youtube_cookie_file is None:
                return f"{message}. Configure MUSIC_YTDLP_COOKIE_FILE with a Netscape-format YouTube cookie export."
            if not self._youtube_cookie_file.exists():
                return (
                    f"{message}. MUSIC_YTDLP_COOKIE_FILE is set but the file does not exist: "
                    f"{self._youtube_cookie_file}"
                )
            return (
                f"{message}. The configured cookie file was used, so this video may also need "
                "MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS."
            )

        return message

    @staticmethod
    def _parse_youtube_extractor_args(raw: str | None) -> dict[str, tuple[str, ...]]:
        if raw is None:
            return {}

        value = raw.strip()
        if not value:
            return {}
        if value.lower().startswith("youtube:"):
            value = value.split(":", 1)[1].strip()

        parsed: dict[str, tuple[str, ...]] = {}
        for segment in value.split(";"):
            item = segment.strip()
            if not item:
                continue

            key, separator, values = item.partition("=")
            if not separator:
                raise ValueError("MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS must use 'key=value1,value2;other=value'.")

            normalised_key = key.strip().lower().replace("-", "_")
            if not normalised_key:
                raise ValueError("MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS contains an empty key.")

            parsed_values = tuple(
                part.replace(r"\,", ",").strip()
                for part in re.split(r"(?<!\\),", values)
                if part.replace(r"\,", ",").strip()
            )
            if not parsed_values:
                raise ValueError(
                    f"MUSIC_YTDLP_YOUTUBE_EXTRACTOR_ARGS[{normalised_key}] must contain at least one value."
                )

            parsed[normalised_key] = parsed_values

        return parsed

    def _should_resume_track(
        self,
        track: MusicTrack,
        seek_seconds: float,
        overlay_duration: float,
    ) -> bool:
        if track.duration_seconds is None:
            return True
        return (seek_seconds + overlay_duration) < max(track.duration_seconds - 0.05, 0.0)

    @staticmethod
    def _source_kind(parsed_url: ParseResult) -> MusicSourceKind:
        host = parsed_url.netloc.lower()
        if host in YOUTUBE_HOSTS or host.endswith(".youtube.com") or host.endswith(".youtu.be"):
            return MusicSourceKind.YOUTUBE
        return MusicSourceKind.URL

    @staticmethod
    def _display_name_from_url(parsed_url: ParseResult) -> str:
        candidate = parsed_url.path.rsplit("/", 1)[-1].strip()
        if candidate:
            return unquote(candidate)
        return parsed_url.netloc

    async def _ensure_connection(
        self,
        session: GuildMusicSession,
        target_channel: hikari.Snowflake,
    ) -> hikariwave.VoiceConnection:
        connection = self.get_connection(session.guild_id)
        if connection is not None:
            if connection.channel_id != target_channel:
                if session.tracks or connection.player.current or connection.player.queue:
                    raise RuntimeError(f"Voice playback is already active in <#{connection.channel_id}>.")
                moved = await self._voice_client.move(target_channel, guild_id=session.guild_id, deaf=True)
                session.channel_id = target_channel
                return moved

            session.channel_id = target_channel
            return connection

        connected = await self._voice_client.connect(session.guild_id, target_channel, deaf=True)
        session.channel_id = target_channel
        connected.player.set_volume(session.volume)
        return connected

    async def _stop_player(self, guild_id: hikari.Snowflake) -> None:
        connection = self.get_connection(guild_id)
        if connection is None:
            return

        with contextlib.suppress(Exception):
            await connection.player.clear_queue()
        with contextlib.suppress(Exception):
            await connection.player.stop()

    async def _disconnect_session(self, guild_id: hikari.Snowflakeish, *, reason: str) -> None:
        guild = hikari.Snowflake(guild_id)
        session = self._sessions.pop(guild, None)
        if session is not None:
            await self._cleanup_tracks(list(session.tracks))

        if connection := self.get_connection(guild):
            with contextlib.suppress(Exception):
                await self._voice_client.disconnect(guild_id=guild)
            log.info(f"Music disconnected guild={guild} channel={connection.channel_id} reason={reason}")

    async def _cleanup_tracks(self, tracks: list[MusicTrack]) -> None:
        for track in tracks:
            await self._cleanup_generated_source(track.audio_source)

    async def _cleanup_replaced_tracks(self, old_tracks: list[MusicTrack], new_tracks: list[MusicTrack]) -> None:
        keep_ids = {id(track.audio_source) for track in new_tracks}
        for track in old_tracks:
            if id(track.audio_source) in keep_ids:
                continue
            await self._cleanup_generated_source(track.audio_source)

    async def _cleanup_generated_source(self, source: AudioSource) -> None:
        handle = self._managed_sources.pop(id(source), None)
        if handle is None:
            return
        await self._cleanup_generated_source_handle(handle)

    async def _cleanup_generated_source_handle(self, handle: GeneratedSourceHandle) -> None:
        if not handle.writer_task.done():
            handle.writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await handle.writer_task
        self._remove_pipe_path(handle.pipe_path, handle.directory)

    async def _create_pipe_source(
        self,
        *,
        name: str,
        duration: float | None,
        writer: Callable[[Path], Coroutine[Any, Any, None]],
    ) -> AudioSource:
        directory = Path(tempfile.mkdtemp(prefix="yukibot-music-"))
        pipe_path = directory / f"{name}.wav"
        os.mkfifo(pipe_path)
        source = PipeAudioSource(str(pipe_path), duration=duration, name=name)
        writer_task = asyncio.create_task(writer(pipe_path), name=f"music-pipe-{name}")
        self._managed_sources[id(source)] = GeneratedSourceHandle(source, directory, pipe_path, writer_task)
        return source

    def _write_duck_overlay(
        self,
        music_input: str,
        tts_audio: bytes,
        *,
        seek_seconds: float,
        overlay_duration: float,
    ) -> Callable[[Path], Coroutine[Any, Any, None]]:
        async def writer(pipe_path: Path) -> None:
            args = [
                self._ffmpeg_path or "ffmpeg",
                "-y",
                "-loglevel",
                "warning",
                "-ss",
                f"{seek_seconds:.3f}",
                "-t",
                f"{overlay_duration:.3f}",
                "-i",
                music_input,
                "-i",
                "pipe:0",
                "-filter_complex",
                (
                    f"[0:a]aresample=48000,volume={self._DUCKED_MUSIC_VOLUME}[music];"
                    "[1:a]aresample=48000[tts];"
                    "[music][tts]amix=inputs=2:duration=first:dropout_transition=0[mix]"
                ),
                "-map",
                "[mix]",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-f",
                "wav",
                str(pipe_path),
            ]
            await self._run_ffmpeg_writer(args, pipe_path.parent, pipe_path, stdin_payload=tts_audio)

        return writer

    def _write_resumed_music(
        self,
        music_input: str,
        *,
        seek_seconds: float,
    ) -> Callable[[Path], Coroutine[Any, Any, None]]:
        async def writer(pipe_path: Path) -> None:
            args = [
                self._ffmpeg_path or "ffmpeg",
                "-y",
                "-loglevel",
                "warning",
                "-ss",
                f"{seek_seconds:.3f}",
                "-i",
                music_input,
                "-map",
                "0:a",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-f",
                "wav",
                str(pipe_path),
            ]
            await self._run_ffmpeg_writer(args, pipe_path.parent, pipe_path)

        return writer

    async def _run_ffmpeg_writer(
        self,
        args: list[str],
        directory: Path,
        pipe_path: Path,
        *,
        stdin_payload: bytes | None = None,
    ) -> None:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await process.communicate(stdin_payload)
            if process.returncode != 0:
                error = err.decode("utf-8", "replace").strip() if err else "unknown error"
                log.warning(f"Music ffmpeg writer failed pipe={pipe_path.name!r}: code={process.returncode}; {error}")
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), 1.5)
            raise
        except Exception:
            log.exception(f"Music ffmpeg writer crashed pipe={pipe_path.name!r}")
        finally:
            self._remove_pipe_path(pipe_path, directory)

    @staticmethod
    def _remove_pipe_path(pipe_path: Path, directory: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            pipe_path.unlink()
        with contextlib.suppress(OSError):
            directory.rmdir()

    @classmethod
    def _queue_lines(cls, current: MusicTrack | None, queued: list[MusicTrack]) -> list[str]:
        if current is None and not queued:
            return ["queue: `(empty)`"]

        lines: list[str] = []
        if current is not None:
            lines.append(f"now: `{current.display_name}`")
        else:
            lines.append("now: `(buffering)`")

        preview = queued[: cls._QUEUE_PREVIEW_LIMIT]
        if preview:
            for index, track in enumerate(preview, start=1):
                lines.append(f"{index}. `{track.display_name}`")
        else:
            lines.append("up next: `(none)`")

        hidden = len(queued) - len(preview)
        if hidden > 0:
            lines.append(f"... and `{hidden}` more")

        return lines


async def _respond_music_error(ctx: lightbulb.Context, message: str) -> None:
    await ctx.respond(message, flags=hikari.MessageFlag.SUPPRESS_EMBEDS)


@group_music.register
class CMD_MusicPlay(
    lightbulb.SlashCommand,
    name="play",
    description="Queue music from a URL or YouTube link",
):
    source = lightbulb.string("source", "HTTP(S) URL or YouTube link")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return

        channel_id = music.author_voice_channel(ctx.guild_id, ctx.user.id)
        if channel_id is None:
            await ctx.respond("Join a voice channel first.")
            return

        await ctx.defer()
        try:
            track, queue_position = await music.enqueue(
                ctx.guild_id,
                channel_id,
                self.source,
                requestor_id=ctx.user.id,
            )
        except (RuntimeError, ValueError) as xcp:
            await _respond_music_error(ctx, str(xcp))
            return

        if queue_position == 0:
            await ctx.respond(
                "\n".join(
                    [
                        f"channel: <#{channel_id}>",
                        f"playing: `{track.display_name}`",
                    ]
                )
            )
            return

        await ctx.respond(
            "\n".join(
                [
                    f"channel: <#{channel_id}>",
                    f"queued: `{track.display_name}`",
                    f"position: `{queue_position}`",
                ]
            )
        )


@group_music.register
class CMD_MusicPause(
    lightbulb.SlashCommand,
    name="pause",
    description="Pause the current music track",
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return
        try:
            await music.pause(ctx.guild_id)
        except RuntimeError as xcp:
            await _respond_music_error(ctx, str(xcp))
            return
        await ctx.respond("Music paused.")


@group_music.register
class CMD_MusicResume(
    lightbulb.SlashCommand,
    name="resume",
    description="Resume the current music track",
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return
        try:
            await music.resume(ctx.guild_id)
        except RuntimeError as xcp:
            await _respond_music_error(ctx, str(xcp))
            return
        await ctx.respond("Music resumed.")


@group_music.register
class CMD_MusicSkip(
    lightbulb.SlashCommand,
    name="skip",
    description="Skip the current music track",
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return
        try:
            await music.skip(ctx.guild_id)
        except RuntimeError as xcp:
            await _respond_music_error(ctx, str(xcp))
            return
        await ctx.respond("Skipped the current track.")


@group_music.register
class CMD_MusicStop(
    lightbulb.SlashCommand,
    name="stop",
    description="Stop music playback and disconnect",
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return
        try:
            await music.stop(ctx.guild_id)
        except RuntimeError as xcp:
            await _respond_music_error(ctx, str(xcp))
            return
        await ctx.respond("Music stopped and disconnected.")


@group_music.register
class CMD_MusicNow(
    lightbulb.SlashCommand,
    name="now",
    description="Show the current music track",
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return

        current = music.now_playing(ctx.guild_id)
        if current is None:
            await ctx.respond("Nothing is playing in this server.")
            return

        channel_id, track = current
        await ctx.respond(
            "\n".join(
                [
                    f"channel: <#{channel_id}>",
                    f"now: `{track.display_name}`",
                    f"kind: `{track.source_kind}`",
                ]
            )
        )


@group_music.register
class CMD_MusicQueue(
    lightbulb.SlashCommand,
    name="queue",
    description="Show the music queue",
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return

        try:
            channel_id, current, queued = music.queue_snapshot(ctx.guild_id)
        except RuntimeError as xcp:
            await _respond_music_error(ctx, str(xcp))
            return

        await ctx.respond("\n".join([f"channel: <#{channel_id}>", *music._queue_lines(current, queued)]))


@group_music.register
class CMD_MusicVolume(
    lightbulb.SlashCommand,
    name="volume",
    description="Get or set the music volume",
):
    value = lightbulb.number(
        "value",
        "Volume percent, e.g. 50, 100, 150",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, music: MusicService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        if ctx.guild_id is None:
            await ctx.respond("Music commands only work in servers.")
            return

        if self.value is None:
            await ctx.respond(f"music volume: `{music.volume(ctx.guild_id):.2f}`")
            return

        try:
            volume = await music.set_volume(ctx.guild_id, float(self.value) / 100)
        except (RuntimeError, ValueError) as xcp:
            await _respond_music_error(ctx, str(xcp))
            return

        await ctx.respond(f"music volume: `{volume:.2f}`")
# AiviA APasz