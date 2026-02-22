from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from dataclasses import dataclass

import emoji
import hikari
import hikariwave
import lightbulb

import config
from _security import Access_Control

log = logging.getLogger(__name__)

group_voice = lightbulb.Group("voice", "Voice commands and TTS")  # type: ignore

TARGET_TTS_USER_ID = hikari.Snowflake(1340971786942025781)
DISCORD_CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
EMOJI_TAG_RE = re.compile(r":[a-z0-9_+\-]+:", re.IGNORECASE)
TOKEN_RE = re.compile(r":[a-z0-9_+\-]+:|[^\s]+", re.IGNORECASE)
VOICE_LINE_RE = re.compile(r"^\s*\d+\s+(\S+)\s+")


@dataclass(slots=True, frozen=True)
class VoiceJob:
    guild_id: hikari.Snowflake
    message_id: hikari.Snowflake
    text: str


class VoiceTTSService:
    _MAX_SPOKEN_CHARS = 550
    _LOG_PREVIEW_CHARS = 120

    def __init__(self, bot: hikari.GatewayBot):
        self.bot = bot
        self.voice_channel = config.VOICE_CHANNEL
        self.tts_channel = config.TTS_CHANNEL

        self._voice_client = hikariwave.VoiceClient(bot)
        self._queue: asyncio.Queue[VoiceJob] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._engine = self._resolve_local_tts_engine()
        self.voice = config.TTS_VOICE
        self._available_voices: list[str] = []

        self._enabled = bool(self.voice_channel and self.tts_channel)
        if not self._enabled:
            log.warning("Voice TTS disabled: VOICE_CHANNEL and TTS_CHANNEL must be configured")
        elif not self._engine:
            log.warning("Voice TTS disabled: local TTS executable not found (espeak-ng/espeak)")

    async def setup(self):
        if self._worker_task:
            return
        self._worker_task = asyncio.create_task(self._worker_loop(), name="voice-tts-worker")
        await self._validate_voice()
        if self._enabled:
            log.info(
                f"Voice TTS ready: {self.tts_channel=} {self.voice_channel=} target_user={TARGET_TTS_USER_ID} "
                f"voice={self.voice} engine={self._engine or 'none'}"
            )

    async def close(self):
        if self._worker_task:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        await self._voice_client.close()

    def get_connection(self, guild_id: hikari.Snowflakeish) -> hikariwave.VoiceConnection | None:
        return self._voice_client.get_connection(guild_id=guild_id)

    async def available_voices(self, force_refresh: bool = False) -> list[str]:
        if self._available_voices and not force_refresh:
            return self._available_voices
        if not self._engine:
            self._available_voices = []
            return self._available_voices

        process = await asyncio.create_subprocess_exec(
            self._engine,
            "--voices",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        if process.returncode != 0:
            error = err.decode(config.STR_ENCODE, "replace").strip() if err else "unknown error"
            log.warning(f"TTS voices lookup failed: code={process.returncode}; {error}")
            self._available_voices = []
            return self._available_voices

        raw = out.decode(config.STR_ENCODE, "replace")
        voices = sorted({m.group(1) for m in [VOICE_LINE_RE.match(line) for line in raw.splitlines()] if m})
        self._available_voices = voices
        return self._available_voices

    async def set_voice(self, voice: str) -> str:
        voice = voice.strip()
        if not voice:
            raise ValueError("voice must not be empty")

        voices = await self.available_voices(force_refresh=True)
        if voices:
            match = next((v for v in voices if v.lower() == voice.lower()), None)
            if not match:
                raise LookupError(f"Unknown voice: {voice}")
            voice = match

        self.voice = voice
        log.info(f"TTS voice changed: {self.voice}")
        return self.voice

    async def _validate_voice(self):
        voices = await self.available_voices(force_refresh=True)
        if not voices:
            return
        if any(self.voice.lower() == v.lower() for v in voices):
            self.voice = next(v for v in voices if self.voice.lower() == v.lower())
            return

        fallback = "en-us" if "en-us" in voices else voices[0]
        log.warning(f"TTS voice '{self.voice}' unavailable; using '{fallback}'")
        self.voice = fallback

    async def on_message(self, event: hikari.GuildMessageCreateEvent):
        if event.channel_id != self.tts_channel:
            return

        raw = (event.content or "").strip()
        preview = self._preview(raw)
        base_log = (
            f"TTS message {event.message_id=} {event.guild_id=} {event.channel_id=} {event.author_id=} "
            f"attachments={len(event.message.attachments)} preview={preview!r}"
        )

        if not self._enabled:
            log.info(f"{base_log} said=no reason=service_disabled")
            return
        if not self._engine:
            log.info(f"{base_log} said=no reason=no_local_tts_engine")
            return
        if not event.is_human:
            log.info(f"{base_log} said=no reason=not_human")
            return
        if event.author_id != TARGET_TTS_USER_ID:
            log.info(f"{base_log} said=no reason=wrong_user")
            return
        if not event.guild_id:
            log.info(f"{base_log} said=no reason=no_guild")
            return
        if raw.startswith(config.CHAT_IGNORE):
            log.info(f"{base_log} said=no reason=chat_ignore_prefix")
            return

        spoken = self._normalise_for_speech(raw)
        if not spoken:
            log.info(f"{base_log} said=no reason=empty_after_normalise")
            return

        self._queue.put_nowait(VoiceJob(hikari.Snowflake(event.guild_id), event.message_id, spoken))
        log.info(
            f"{base_log} said=queued reason=accepted queue_size={self._queue.qsize()} spoken={self._preview(spoken)!r}"
        )

    async def _worker_loop(self):
        while True:
            job = await self._queue.get()
            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(f"Voice TTS failed for {job.message_id=}")
            finally:
                self._queue.task_done()

    async def _process_job(self, job: VoiceJob):
        connection = await self._ensure_connection(job.guild_id)
        if not connection:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=no_voice_connection")
            return

        self._recover_stale_player_task(connection, job.message_id)

        audio = await self._synth_text(job.text)
        if not audio:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=tts_synth_empty")
            return

        source = hikariwave.BufferAudioSource(audio, name=f"tts-{int(job.message_id)}")
        result = await connection.player.add_queue(source)
        if not result.success:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=player_rejected detail={result.reason}")
            return

        log.info(f"TTS job queued-to-player {job.message_id=} queue_len={len(connection.player.queue)}")
        finished = await self._wait_for_completion(job, connection, source)
        if finished:
            log.info(f"TTS job completed {job.message_id=} said=yes")
        else:
            log.warning(f"TTS job dropped {job.message_id=} said=no reason=playback_timeout_or_reset")

    async def _ensure_connection(self, guild_id: hikari.Snowflake) -> hikariwave.VoiceConnection | None:
        if not self.voice_channel:
            return None

        target_channel = hikari.Snowflake(self.voice_channel)
        connection = self._voice_client.get_connection(guild_id=guild_id)

        if connection and connection.channel_id == target_channel:
            log.info(f"TTS voice ready {guild_id=} channel={int(target_channel)} mode=reuse")
            return connection

        if connection:
            log.info(
                f"TTS moving voice {guild_id=} from={int(connection.channel_id)} to={int(target_channel)} mode=move"
            )
            return await self._voice_client.move(target_channel, guild_id=guild_id, deaf=True)

        # Recovery path: if Discord still reports us in voice but hikari-wave lost the connection object,
        # explicitly clear voice state before reconnecting to avoid stale-state connect issues.
        me = self.bot.get_me()
        if me and (state := self.bot.cache.get_voice_state(guild_id, me.id)):
            if state.channel_id:
                log.warning(
                    f"TTS stale voice-state detected {guild_id=} cached_channel={int(state.channel_id)}; resetting"
                )
                with contextlib.suppress(Exception):
                    await self.bot.update_voice_state(guild_id, None)
                await asyncio.sleep(0.35)

        last_xcp: Exception | None = None
        for attempt in range(1, 4):
            try:
                log.info(f"TTS connecting voice {guild_id=} channel={int(target_channel)} attempt={attempt}")
                return await self._voice_client.connect(guild_id, target_channel, deaf=True)
            except Exception as xcp:
                last_xcp = xcp
                log.warning(f"TTS connect attempt failed {guild_id=} attempt={attempt}: {type(xcp).__name__}: {xcp}")
                with contextlib.suppress(Exception):
                    await self.bot.update_voice_state(guild_id, None)
                await asyncio.sleep(0.45)

        if last_xcp:
            log.error(f"TTS unable to establish voice connection {guild_id=}: {type(last_xcp).__name__}: {last_xcp}")
        return None

    async def _synth_text(self, text: str) -> bytes:
        if not self._engine:
            return b""

        process = await asyncio.create_subprocess_exec(
            self._engine,
            "--stdout",
            "-v",
            self.voice,
            "-s",
            "165",
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        if process.returncode != 0:
            error = err.decode(config.STR_ENCODE, "replace").strip() if err else "unknown error"
            log.warning(f"TTS synth failed: code={process.returncode}; {error}")
            return b""
        return out

    async def _wait_for_completion(
        self,
        job: VoiceJob,
        connection: hikariwave.VoiceConnection,
        source: hikariwave.AudioSource,
    ) -> bool:
        timeout = self._playback_timeout_seconds(job.text)

        def pred(e):
            return e.guild_id == job.guild_id and e.audio is source

        # Not fatal if begin is missed (race / event-loop scheduling), but useful diagnostics.
        try:
            await self.bot.wait_for(hikariwave.AudioBeginEvent, 5.0, pred)
            log.info(f"TTS audio begin {job.message_id=} timeout={timeout:.1f}s")
        except asyncio.TimeoutError:
            log.warning(f"TTS audio begin timeout {job.message_id=} continuing wait_for_end")

        try:
            await self.bot.wait_for(hikariwave.AudioEndEvent, timeout, pred)
            return True
        except asyncio.TimeoutError:
            state = getattr(connection.player.state, "name", str(connection.player.state))
            log.warning(
                f"TTS audio end timeout {job.message_id=} state={state} elapsed={connection.player.elapsed:.2f}s "
                f"queue_len={len(connection.player.queue)} timeout={timeout:.1f}s"
            )
            with contextlib.suppress(Exception):
                await connection.player.stop()
            with contextlib.suppress(Exception):
                await self._voice_client.disconnect(guild_id=job.guild_id)
            await asyncio.sleep(0.35)
            return False

    def _recover_stale_player_task(self, connection: hikariwave.VoiceConnection, message_id: hikari.Snowflake):
        player = connection.player
        task = getattr(player, "_player_task", None)
        if not task:
            return
        if not task.done():
            return

        cancelled = task.cancelled()
        error: Exception | None = None
        if not cancelled:
            with contextlib.suppress(Exception):
                error = task.exception()

        setattr(player, "_player_task", None)
        if error:
            log.warning(
                f"TTS recovered stale player task {message_id=} cancelled={cancelled} error={type(error).__name__}: {error}"
            )
        else:
            log.warning(f"TTS recovered stale player task {message_id=} cancelled={cancelled}")

    def _normalise_for_speech(self, content: str) -> str:
        text = content.strip()
        if not text:
            return ""

        text = URL_RE.sub(" ", text)
        text = DISCORD_CUSTOM_EMOJI_RE.sub(lambda m: f":{m.group(1)}:", text)
        text = emoji.demojize(text, language="en")

        spoken = []
        repeat_tag: str | None = None
        repeat_count = 0

        def flush_repeat():
            nonlocal repeat_tag
            nonlocal repeat_count
            if not repeat_tag:
                return
            label = self._emoji_tag_to_words(repeat_tag)
            if repeat_count > 1:
                spoken.append(f"{label} x{repeat_count}")
            else:
                spoken.append(label)
            repeat_tag = None
            repeat_count = 0

        for token in TOKEN_RE.findall(text):
            if EMOJI_TAG_RE.fullmatch(token):
                token = token.lower()
                if token == repeat_tag:
                    repeat_count += 1
                else:
                    flush_repeat()
                    repeat_tag = token
                    repeat_count = 1
                continue

            flush_repeat()
            spoken.append(token)

        flush_repeat()

        text = " ".join(spoken)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""

        if len(text) > self._MAX_SPOKEN_CHARS:
            text = text[: self._MAX_SPOKEN_CHARS].rstrip()
        return text

    @staticmethod
    def _emoji_tag_to_words(tag: str) -> str:
        name = tag.strip(":").replace("_", " ").replace("-", " ")
        return name.strip() or "emoji"

    @staticmethod
    def _resolve_local_tts_engine() -> str | None:
        for executable in ("espeak-ng", "espeak"):
            if path := shutil.which(executable):
                return path
        return None

    def _preview(self, text: str) -> str:
        if len(text) <= self._LOG_PREVIEW_CHARS:
            return text
        return text[: self._LOG_PREVIEW_CHARS].rstrip() + "..."

    @staticmethod
    def _playback_timeout_seconds(text: str) -> float:
        words = max(1, len(text.split()))
        # At ~165 wpm this leaves generous headroom for connect/encode jitter.
        return min(120.0, max(10.0, words * 0.7 + 8.0))


async def ac_tts_voices(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return
    needle = ctx.focused.value.strip().lower()
    voices = await voice_tts.available_voices()
    if needle:
        voices = [voice for voice in voices if needle in voice.lower()]
    await ctx.respond(voices[:25])


@group_voice.register
class CMD_VoiceSet(
    lightbulb.SlashCommand,
    name="set",
    description="Get or set runtime TTS voice",
):
    voice = lightbulb.string(
        "voice",
        "Voice id (leave empty to view current)",
        autocomplete=ac_tts_voices,  # pyright: ignore[reportArgumentType]
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)

        if not self.voice:
            guild_id = hikari.Snowflake(ctx.guild_id or config.DISCORD_GUILD)
            conn = voice_tts.get_connection(guild_id)
            voices = await voice_tts.available_voices()
            conn_chan = f"<#{int(conn.channel_id)}>" if conn else "not connected"
            await ctx.respond(
                "\n".join(
                    [
                        f"voice: `{voice_tts.voice}`",
                        f"engine: `{voice_tts._engine or 'none'}`",
                        f"connected: {conn_chan}",
                        f"available voices: `{len(voices)}` (use autocomplete on `voice` option)",
                    ]
                )
            )
            return

        await acl.perm_check(ctx.user.id, acl.LvL.user)

        try:
            selected = await voice_tts.set_voice(self.voice)
        except LookupError:
            await ctx.respond(f"Unknown voice `{self.voice}`. Use the `voice` autocomplete.")
            return
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            return

        await ctx.respond(
            f"TTS voice set to `{selected}`.\n"
            "Runtime change only; set `TTS_VOICE` in `.env` to persist across restarts."
        )
