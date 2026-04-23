from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import emoji
import hikari
import lightbulb
import requests

import config
from _security import Access_Control
from cmd_voice_common import DISCORD_CUSTOM_EMOJI_RE, EMOJI_TAG_RE
from cmd_voice_service import (
    HFRepoRef,
    PiperPythonVoiceRuntime,
    SpeechContent,
    VoiceJob,
    VoiceRuntimeResetResult,
    VoiceTTSService,
    group_voice,
    log,
)


@dataclass(slots=True, frozen=True)
class NormalizedVoiceSource:
    raw: str
    key: str
    emoji_token: str | None = None

    @property
    def is_emoji(self) -> bool:
        return self.emoji_token is not None

    def display(self) -> str:
        if self.emoji_token is None:
            return f"`{self.key}`"
        return f"`{self.emoji_token}` (`{self.key}`)"


def _normalise_voice_source(source: str) -> NormalizedVoiceSource:
    value = source.strip()
    if not value:
        return NormalizedVoiceSource(raw=source, key="")

    tag: str | None = None
    if match := DISCORD_CUSTOM_EMOJI_RE.fullmatch(value):
        tag = f":{match.group(1).lower()}:"
    elif EMOJI_TAG_RE.fullmatch(value):
        tag = value.lower()
    else:
        demojized = emoji.demojize(value, language="en")
        if demojized != value and EMOJI_TAG_RE.fullmatch(demojized):
            tag = demojized.lower()

    if tag is None:
        return NormalizedVoiceSource(raw=value, key=value)
    return NormalizedVoiceSource(raw=value, key=tag.strip(":"), emoji_token=value)


async def ac_tts_voices(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return
    needle = ctx.focused.value.strip().lower()
    voices = await voice_tts.available_voices()
    if needle:
        voices = [voice for voice in voices if needle in voice.lower()]
    await ctx.respond(voices[:25])


async def ac_tts_variants(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    voice_opt = ctx.get_option("voice")
    selected_voice: str | None = None
    if voice_opt and isinstance(voice_opt.value, str):
        selected_voice = voice_opt.value.strip() or None
    if not selected_voice:
        selected_voice, _ = voice_tts.user_voice_variant(ctx.interaction.user.id)

    voices = await voice_tts.available_voices()
    if voices:
        match = next((voice for voice in voices if voice.lower() == selected_voice.lower()), None)
        if match:
            selected_voice = match

    variants = ["none", *await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)]
    await ctx.respond(voice_tts.variant_autocomplete_choices(selected_voice, variants, needle))


async def ac_tts_custom_models(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    models = voice_tts.available_custom_voices()
    if needle:
        models = [model for model in models if needle in model.lower()]
    await ctx.respond(models[:25])


async def ac_tts_substitution_sources(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    sources = list(voice_tts.user_text_substitutions(ctx.interaction.user.id))
    if needle:
        sources = [source for source in sources if needle in source.lower()]
    await ctx.respond(sources[:25])


async def ac_tts_pronunciation_sources(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    sources = list(voice_tts.user_pronunciations(ctx.interaction.user.id))
    if needle:
        sources = [source for source in sources if needle in source.lower()]
    await ctx.respond(sources[:25])


async def ac_tts_global_substitution_sources(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    category = "slang"
    category_opt = ctx.get_option("category")
    if category_opt and isinstance(category_opt.value, str):
        category = category_opt.value

    try:
        sources = list(voice_tts.global_text_substitutions(category))
    except ValueError:
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    if needle:
        sources = [source for source in sources if needle in source.lower()]
    await ctx.respond(sources[:25])


async def ac_tts_protected_tokens(ctx: lightbulb.AutocompleteContext, voice_tts: VoiceTTSService):
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    tokens = voice_tts.global_protected_text_tokens()
    if needle:
        tokens = [token for token in tokens if needle in token.lower()]
    await ctx.respond(tokens[:25])


@group_voice.register
class CMD_VoiceSay(
    lightbulb.SlashCommand,
    name="say",
    description="Queue TTS text from any channel",
):
    text = lightbulb.string("text", "What the bot should say")
    target = lightbulb.string(
        "target",
        "Configured voice target (defaults to the primary guild)",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        try:
            guild_id = voice_tts.resolve_voice_target_selection(self.target)
        except LookupError as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd say rejected unknown_target user={ctx.user.id} target={self.target!r}")
            return
        if guild_id is None:
            await ctx.respond("Voice TTS is not configured for any server.")
            log.info(f"Voice cmd say rejected no_targets user={ctx.user.id}")
            return

        target_label = await voice_tts.describe_voice_target(guild_id)

        log.info(
            f"Voice cmd say invoked user={ctx.user.id} guild={ctx.guild_id} "
            f"resolved_guild={guild_id} target={target_label!r} text={voice_tts._preview(self.text)!r}"
        )

        try:
            spoken, queue_len = voice_tts.queue_say(guild_id, ctx.interaction.id, self.text, user_id=ctx.user.id)
        except (RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd say rejected user={ctx.user.id} reason={xcp}")
            return

        selected_voice, selected_variant = voice_tts.user_voice_variant_for_say(ctx.user.id)
        voice_spec = voice_tts._voice_spec(selected_voice, selected_variant)
        await ctx.respond(
            "\n".join(
                [
                    f"target: `{target_label}`",
                    f"says `{voice_tts._preview(spoken)}`",
                ]
            )
        )
        log.info(
            f"Voice cmd say success user={ctx.user.id} guild={ctx.guild_id} resolved_guild={guild_id} "
            f"target={target_label!r} queue_size={queue_len} voice={voice_spec} spoken={voice_tts._preview(spoken)!r}"
        )


@group_voice.register
class CMD_VoiceSet(
    lightbulb.SlashCommand,
    name="set",
    description="Get or set your TTS voice and variant",
):
    _VARIANT_PREVIEW_LIMIT = 15

    voice = lightbulb.string(
        "voice",
        "Voice id (leave empty to view current)",
        autocomplete=ac_tts_voices,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    variant = lightbulb.string(
        "variant",
        "Variant id (optional; use `none` to disable)",
        autocomplete=ac_tts_variants,  # pyright: ignore[reportArgumentType]
        default=None,
    )

    @classmethod
    def _variant_preview(cls, variants: list[str]) -> str:
        if not variants:
            return "`none`"
        shown = ", ".join(f"`{variant}`" for variant in variants[: cls._VARIANT_PREVIEW_LIMIT])
        if len(variants) > cls._VARIANT_PREVIEW_LIMIT:
            shown += f", ... (+{len(variants) - cls._VARIANT_PREVIEW_LIMIT} more)"
        return shown

    @staticmethod
    def _connection_status(ctx: lightbulb.Context, voice_tts: VoiceTTSService) -> str:
        if ctx.guild_id:
            connection = voice_tts.get_connection(hikari.Snowflake(ctx.guild_id))
            return f"<#{connection.channel_id}>" if connection else "not connected"

        connections = voice_tts.active_voice_connections()
        if not connections:
            return "not connected"
        if len(connections) == 1:
            connection = connections[0]
            return f"<#{connection.channel_id}> in `{connection.guild_id}`"
        return ", ".join(f"`{connection.guild_id}` -> <#{connection.channel_id}>" for connection in connections)

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        current_voice, current_variant = voice_tts.user_voice_variant(ctx.user.id)
        is_listening = voice_tts.is_user_listening(ctx.user.id)
        log.info(
            f"Voice cmd set invoked by user={ctx.user.id} requested_voice={self.voice!r} requested_variant={self.variant!r} "
            f"current_voice={current_voice!r} current_variant={current_variant!r} listening={is_listening}"
        )

        if not self.voice and not self.variant:
            voices = await voice_tts.available_voices()
            variants = await voice_tts.available_variants_for_voice(current_voice, force_refresh=True)
            await ctx.respond(
                "\n".join(
                    [
                        f"listen: `{'enabled' if is_listening else 'disabled'}`",
                        f"autocorrect: `{'enabled' if voice_tts.user_autocorrect_enabled(ctx.user.id) else 'disabled'}`",
                        f"voice: `{current_voice}`",
                        f"variant: `{current_variant or 'none'}`",
                        f"engine: `{voice_tts._engine_display()}`",
                        f"connected: {self._connection_status(ctx, voice_tts)}",
                        f"available voices: `{len(voices)}` (use autocomplete on `voice` option)",
                        f"available variants for `{current_voice}`: `{len(variants)}` (use autocomplete on `variant` option)",
                        f"variants: {self._variant_preview(variants)}",
                    ]
                )
            )
            log.info(
                f"Voice cmd set status user={ctx.user.id} current_voice={current_voice!r} "
                f"current_variant={current_variant!r} listening={is_listening} voices={len(voices)} variants={len(variants)}"
            )
            return

        try:
            selected_voice, selected_variant = await voice_tts.set_user_voice_variant(
                ctx.user.id,
                voice=self.voice,
                variant=self.variant,
            )
        except LookupError as xcp:
            message = str(xcp)
            if message.startswith("Unknown variant:"):
                await ctx.respond(f"Unknown variant `{self.variant}`. Use the `variant` autocomplete.")
                log.info(
                    f"Voice cmd set rejected unknown variant user={ctx.user.id} requested_variant={self.variant!r}"
                )
                return
            if message.startswith("Voice has no variants:"):
                voice_name = message.split(": ", 1)[1] if ": " in message else (self.voice or current_voice)
                await ctx.respond(f"Voice `{voice_name}` has no variants.")
                log.info(
                    f"Voice cmd set rejected unavailable variant user={ctx.user.id} "
                    f"requested_voice={self.voice!r} requested_variant={self.variant!r}"
                )
                return

            await ctx.respond(f"Unknown voice `{self.voice}`. Use the `voice` autocomplete.")
            log.info(f"Voice cmd set rejected unknown voice user={ctx.user.id} requested_voice={self.voice!r}")
            return
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd set rejected invalid user={ctx.user.id} requested_voice={self.voice!r} "
                f"requested_variant={self.variant!r} reason={xcp}"
            )
            return

        variants = await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)
        await ctx.respond(
            "\n".join(
                [
                    f"TTS voice: `{selected_voice}`",
                    f"TTS variant: `{selected_variant or 'none'}`",
                    f"listen: `{'enabled' if voice_tts.is_user_listening(ctx.user.id) else 'disabled'}`",
                    f"autocorrect: `{'enabled' if voice_tts.user_autocorrect_enabled(ctx.user.id) else 'disabled'}`",
                    f"available variants for `{selected_voice}`: `{len(variants)}`",
                    f"variants: {self._variant_preview(variants)}",
                    "Applies to your user only. Use `/voice listen enabled:true` to read your messages.",
                ]
            )
        )
        log.info(
            f"Voice cmd set success user={ctx.user.id} selected_voice={selected_voice!r} "
            f"selected_variant={selected_variant!r}"
        )


@group_voice.register
class CMD_VoiceList(
    lightbulb.SlashCommand,
    name="list",
    description="List variants for a voice",
):
    _MAX_MESSAGE_CHARS = 1850

    voice = lightbulb.string(
        "voice",
        "Voice id to list variants for (defaults to your current voice)",
        autocomplete=ac_tts_voices,  # pyright: ignore[reportArgumentType]
        default=None,
    )

    @classmethod
    def _chunk_variant_messages(cls, voice: str, variants: list[str]) -> list[str]:
        header = [f"voice: `{voice}`", f"available variants: `{len(variants)}`"]
        if not variants:
            return ["\n".join([*header, "variants: `none`"])]

        messages: list[str] = []
        current = "\n".join([*header, "variants:"])
        for variant in variants:
            line = f"`{variant}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join([f"voice: `{voice}`", "variants (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        current_voice, _ = voice_tts.user_voice_variant(ctx.user.id)
        requested_voice = (self.voice or current_voice).strip()

        if not requested_voice:
            await ctx.respond("voice must not be empty")
            log.info(f"Voice cmd list rejected empty_voice user={ctx.user.id}")
            return

        voices = await voice_tts.available_voices(force_refresh=True)
        selected_voice = requested_voice
        if voices:
            match = next((voice for voice in voices if voice.lower() == requested_voice.lower()), None)
            if not match:
                if voice_tts._engine_kind != "piper" or not voice_tts._piper_model_path(requested_voice):
                    await ctx.respond(f"Unknown voice `{requested_voice}`. Use the `voice` autocomplete.")
                    log.info(
                        f"Voice cmd list rejected unknown_voice user={ctx.user.id} requested_voice={requested_voice!r}"
                    )
                    return
            else:
                selected_voice = match

        variants = await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)
        messages = self._chunk_variant_messages(selected_voice, variants)
        for message in messages:
            await ctx.respond(message)

        log.info(
            f"Voice cmd list success user={ctx.user.id} selected_voice={selected_voice!r} variants={len(variants)}"
        )


@group_voice.register
class CMD_VoiceListen(
    lightbulb.SlashCommand,
    name="listen",
    description="Enable or disable TTS listening for your messages",
):
    enabled = lightbulb.boolean(
        "enabled",
        "Enable or disable listening (leave empty to view current state)",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        current_state = voice_tts.is_user_listening(ctx.user.id)
        current_voice, current_variant = voice_tts.user_voice_variant(ctx.user.id)
        autocorrect_enabled = voice_tts.user_autocorrect_enabled(ctx.user.id)

        if self.enabled is None:
            await ctx.respond(
                "\n".join(
                    [
                        f"listen: `{'enabled' if current_state else 'disabled'}`",
                        f"autocorrect: `{'enabled' if autocorrect_enabled else 'disabled'}`",
                        f"voice: `{current_voice}`",
                        f"variant: `{current_variant or 'none'}`",
                        "Use `/voice listen enabled:true` to enable reading your messages.",
                    ]
                )
            )
            log.info(
                f"Voice cmd listen status user={ctx.user.id} enabled={current_state} "
                f"autocorrect={autocorrect_enabled} voice={current_voice!r} variant={current_variant!r}"
            )
            return

        updated_state = voice_tts.set_user_listening(ctx.user.id, self.enabled)
        updated_voice, updated_variant = voice_tts.user_voice_variant(ctx.user.id)
        await ctx.respond(
            "\n".join(
                [
                    f"listen: `{'enabled' if updated_state else 'disabled'}`",
                    f"autocorrect: `{'enabled' if autocorrect_enabled else 'disabled'}`",
                    f"voice: `{updated_voice}`",
                    f"variant: `{updated_variant or 'none'}`",
                ]
            )
        )
        log.info(
            f"Voice cmd listen success user={ctx.user.id} old_enabled={current_state} new_enabled={updated_state} "
            f"autocorrect={autocorrect_enabled} voice={updated_voice!r} variant={updated_variant!r}"
        )


@group_voice.register
class CMD_VoiceAutocorrect(
    lightbulb.SlashCommand,
    name="autocorrect",
    description="Enable or disable fuzzy typo correction for your TTS text",
):
    enabled = lightbulb.boolean(
        "enabled",
        "Enable or disable fuzzy typo correction (leave empty to view current state)",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        current_state = voice_tts.user_autocorrect_enabled(ctx.user.id)
        current_voice, current_variant = voice_tts.user_voice_variant(ctx.user.id)
        listening_enabled = voice_tts.is_user_listening(ctx.user.id)

        if self.enabled is None:
            await ctx.respond(
                "\n".join(
                    [
                        f"autocorrect: `{'enabled' if current_state else 'disabled'}`",
                        f"listen: `{'enabled' if listening_enabled else 'disabled'}`",
                        f"voice: `{current_voice}`",
                        f"variant: `{current_variant or 'none'}`",
                        "Fuzzy typo correction applies to lowercase words only and is enabled by default.",
                    ]
                )
            )
            log.info(
                f"Voice cmd autocorrect status user={ctx.user.id} enabled={current_state} "
                f"listen={listening_enabled} voice={current_voice!r} variant={current_variant!r}"
            )
            return

        updated_state = voice_tts.set_user_autocorrect(ctx.user.id, self.enabled)
        await ctx.respond(
            "\n".join(
                [
                    f"autocorrect: `{'enabled' if updated_state else 'disabled'}`",
                    f"listen: `{'enabled' if listening_enabled else 'disabled'}`",
                    f"voice: `{current_voice}`",
                    f"variant: `{current_variant or 'none'}`",
                ]
            )
        )
        log.info(
            f"Voice cmd autocorrect success user={ctx.user.id} old_enabled={current_state} "
            f"new_enabled={updated_state} listen={listening_enabled} voice={current_voice!r} variant={current_variant!r}"
        )


@group_voice.register
class CMD_VoicePron(
    lightbulb.SlashCommand,
    name="pron",
    description="Manage your TTS pronunciation overrides",
):
    _MAX_MESSAGE_CHARS = 1850

    source = lightbulb.string(
        "source",
        "Word or emoji to pronounce differently (leave empty to list)",
        autocomplete=ac_tts_pronunciation_sources,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    target = lightbulb.string(
        "target",
        "How it should be spoken (omit to remove source)",
        default=None,
    )

    @classmethod
    def _chunk_messages(cls, pronunciations: dict[str, str]) -> list[str]:
        header = [f"pronunciations: `{len(pronunciations)}`"]
        if not pronunciations:
            return [
                "\n".join([*header, "No pronunciation overrides set. Example: `/voice pron source:egg target:ehg`"])
            ]

        messages: list[str] = []
        current = "\n".join([*header, "source -> spoken as:"])
        for source, target in pronunciations.items():
            line = f"`{source}` -> `{target}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join(["pronunciations (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        source_input = self.source.strip() if isinstance(self.source, str) else ""
        target = self.target.strip() if isinstance(self.target, str) else None
        source = _normalise_voice_source(source_input)

        log.info(
            f"Voice cmd pron invoked user={ctx.user.id} source={source.raw!r} source_key={source.key!r} "
            f"target={voice_tts._preview(target or '')!r}"
        )

        if not source.key and target is not None:
            await ctx.respond("source is required when target is provided")
            log.info(f"Voice cmd pron rejected missing_source user={ctx.user.id}")
            return

        if not source.key:
            pronunciations = voice_tts.user_pronunciations(ctx.user.id)
            for message in self._chunk_messages(pronunciations):
                await ctx.respond(message)
            log.info(f"Voice cmd pron list user={ctx.user.id} count={len(pronunciations)}")
            return

        if target is None:
            try:
                source_key, removed = voice_tts.remove_user_pronunciation(ctx.user.id, source.key)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(
                    f"Voice cmd pron rejected remove user={ctx.user.id} source={source.raw!r} "
                    f"source_key={source.key!r} reason={xcp}"
                )
                return

            source_display = source.display() if source.is_emoji else f"`{source_key}`"
            if removed:
                await ctx.respond(f"Removed pronunciation: {source_display}")
            else:
                await ctx.respond(f"No pronunciation override set for {source_display}.")
            log.info(f"Voice cmd pron remove user={ctx.user.id} source={source_key!r} removed={removed}")
            return

        try:
            source_key, replacement, existed = voice_tts.set_user_pronunciation(ctx.user.id, source.key, target)
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd pron rejected set user={ctx.user.id} source={source.raw!r} source_key={source.key!r} "
                f"target={voice_tts._preview(target)!r} reason={xcp}"
            )
            return

        action = "Updated" if existed else "Added"
        source_display = source.display() if source.is_emoji else f"`{source_key}`"
        await ctx.respond(f"{action} pronunciation: {source_display} -> `{replacement}`")
        log.info(
            f"Voice cmd pron set user={ctx.user.id} source={source_key!r} replacement={voice_tts._preview(replacement)!r} "
            f"updated={existed}"
        )


@group_voice.register
class CMD_VoiceSub(
    lightbulb.SlashCommand,
    name="sub",
    description="Manage your TTS text substitutions",
):
    _MAX_MESSAGE_CHARS = 1850

    source = lightbulb.string(
        "source",
        "Word to replace (leave empty to list)",
        autocomplete=ac_tts_substitution_sources,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    target = lightbulb.string(
        "target",
        "Replacement text (omit to remove source)",
        default=None,
    )

    @classmethod
    def _chunk_substitution_messages(cls, substitutions: dict[str, str]) -> list[str]:
        header = [f"substitutions: `{len(substitutions)}`"]
        if not substitutions:
            return ["\n".join([*header, "No substitutions set. Example: `/voice sub source:im target:I'm`"])]

        messages: list[str] = []
        current = "\n".join([*header, "source -> target:"])
        for source, target in substitutions.items():
            line = f"`{source}` -> `{target}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join(["substitutions (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @staticmethod
    def _build_substitution_text_file(substitutions: dict[str, str]) -> bytes:
        lines = [f"base substitutions: {len(substitutions)}", ""]
        if substitutions:
            lines.extend(f"{source} -> {target}" for source, target in substitutions.items())
        else:
            lines.append("(none)")
        text = "\n".join(lines) + "\n"
        return text.encode(config.STR_ENCODE, "replace")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        source_input = self.source.strip() if isinstance(self.source, str) else ""
        target = self.target.strip() if isinstance(self.target, str) else None
        source = _normalise_voice_source(source_input)

        log.info(
            f"Voice cmd sub invoked user={ctx.user.id} source={source.raw!r} source_key={source.key!r} "
            f"target={voice_tts._preview(target or '')!r}"
        )

        if not source.key and target is not None:
            await ctx.respond("source is required when target is provided")
            log.info(f"Voice cmd sub rejected missing_source user={ctx.user.id}")
            return

        if not source.key:
            substitutions = voice_tts.user_text_substitutions(ctx.user.id)
            for message in self._chunk_substitution_messages(substitutions):
                await ctx.respond(message)
            base_substitutions = voice_tts.base_text_substitutions()
            base_file = hikari.Bytes(
                self._build_substitution_text_file(base_substitutions),
                "voice_base_substitutions.txt",
            )
            await ctx.respond(
                f"Attached base substitutions file (`{len(base_substitutions)}` entries).",
                attachment=base_file,
            )
            log.info(f"Voice cmd sub list user={ctx.user.id} count={len(substitutions)}")
            return

        if target is None:
            try:
                source_key, removed = voice_tts.remove_user_text_substitution(ctx.user.id, source.key)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(
                    f"Voice cmd sub rejected remove user={ctx.user.id} source={source.raw!r} "
                    f"source_key={source.key!r} reason={xcp}"
                )
                return

            source_display = source.display() if source.is_emoji else f"`{source_key}`"
            if removed:
                await ctx.respond(f"Removed substitution: {source_display}")
            else:
                await ctx.respond(f"No substitution set for {source_display}.")
            log.info(f"Voice cmd sub remove user={ctx.user.id} source={source_key!r} removed={removed}")
            return

        try:
            source_key, replacement, existed = voice_tts.set_user_text_substitution(ctx.user.id, source.key, target)
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd sub rejected set user={ctx.user.id} source={source.raw!r} source_key={source.key!r} "
                f"target={voice_tts._preview(target)!r} reason={xcp}"
            )
            return

        action = "Updated" if existed else "Added"
        source_display = source.display() if source.is_emoji else f"`{source_key}`"
        await ctx.respond(f"{action} substitution: {source_display} -> `{replacement}`")
        log.info(
            f"Voice cmd sub set user={ctx.user.id} source={source_key!r} replacement={voice_tts._preview(replacement)!r} "
            f"updated={existed}"
        )


@group_voice.register
class CMD_VoiceGlobalSub(
    lightbulb.SlashCommand,
    name="globalsub",
    description="Manage shared TTS substitutions",
):
    _MAX_MESSAGE_CHARS = 1850

    category = lightbulb.string(
        "category",
        "Shared substitution category",
        choices=[lightbulb.Choice("slang", "slang"), lightbulb.Choice("typo", "typo")],
        default="slang",
    )
    source = lightbulb.string(
        "source",
        "Token to replace (leave empty to list)",
        autocomplete=ac_tts_global_substitution_sources,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    target = lightbulb.string(
        "target",
        "Replacement text (omit to remove source)",
        default=None,
    )

    @classmethod
    def _chunk_messages(cls, category: str, substitutions: dict[str, str]) -> list[str]:
        header = [f"global {category}: `{len(substitutions)}`"]
        if not substitutions:
            return ["\n".join([*header, "No shared substitutions set."])]

        messages: list[str] = []
        current = "\n".join([*header, "source -> target:"])
        for source, target in substitutions.items():
            line = f"`{source}` -> `{target}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join([f"global {category} (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.admin)
        category = self.category.strip() if isinstance(self.category, str) else "slang"
        source_input = self.source.strip() if isinstance(self.source, str) else ""
        target = self.target.strip() if isinstance(self.target, str) else None
        source = _normalise_voice_source(source_input)

        log.info(
            f"Voice cmd globalsub invoked user={ctx.user.id} category={category!r} "
            f"source={source.raw!r} source_key={source.key!r} target={voice_tts._preview(target or '')!r}"
        )

        if not source.key and target is not None:
            await ctx.respond("source is required when target is provided")
            log.info(f"Voice cmd globalsub rejected missing_source user={ctx.user.id}")
            return

        if not source.key:
            try:
                substitutions = voice_tts.global_text_substitutions(category)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(f"Voice cmd globalsub rejected list user={ctx.user.id} category={category!r} reason={xcp}")
                return

            for message in self._chunk_messages(category, substitutions):
                await ctx.respond(message)
            log.info(f"Voice cmd globalsub list user={ctx.user.id} category={category!r} count={len(substitutions)}")
            return

        if target is None:
            try:
                category_key, source_key, removed = voice_tts.remove_global_text_substitution(category, source.key)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(
                    f"Voice cmd globalsub rejected remove user={ctx.user.id} "
                    f"category={category!r} source={source.raw!r} source_key={source.key!r} reason={xcp}"
                )
                return

            source_display = source.display() if source.is_emoji else f"`{source_key}`"
            if removed:
                await ctx.respond(f"Removed global {category_key}: {source_display}")
            else:
                await ctx.respond(f"No global {category_key} set for {source_display}.")
            log.info(
                f"Voice cmd globalsub remove user={ctx.user.id} category={category_key!r} "
                f"source={source_key!r} removed={removed}"
            )
            return

        try:
            category_key, source_key, replacement, existed = voice_tts.set_global_text_substitution(
                category,
                source.key,
                target,
            )
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd globalsub rejected set user={ctx.user.id} category={category!r} "
                f"source={source.raw!r} source_key={source.key!r} target={voice_tts._preview(target)!r} reason={xcp}"
            )
            return

        action = "Updated" if existed else "Added"
        source_display = source.display() if source.is_emoji else f"`{source_key}`"
        await ctx.respond(f"{action} global {category_key}: {source_display} -> `{replacement}`")
        log.info(
            f"Voice cmd globalsub set user={ctx.user.id} category={category_key!r} "
            f"source={source_key!r} replacement={voice_tts._preview(replacement)!r} updated={existed}"
        )


@group_voice.register
class CMD_VoiceProtect(
    lightbulb.SlashCommand,
    name="protect",
    description="Manage shared TTS autocorrect protected tokens",
):
    _MAX_MESSAGE_CHARS = 1850

    source = lightbulb.string(
        "source",
        "Token to protect (leave empty to list)",
        autocomplete=ac_tts_protected_tokens,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    remove = lightbulb.boolean(
        "remove",
        "Remove this token instead of adding it",
        default=False,
    )

    @classmethod
    def _chunk_messages(cls, tokens: list[str]) -> list[str]:
        header = [f"protected tokens: `{len(tokens)}`"]
        if not tokens:
            return ["\n".join([*header, "No protected tokens set."])]

        messages: list[str] = []
        current = "\n".join([*header, "tokens:"])
        for token in tokens:
            line = f"`{token}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join(["protected tokens (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.admin)
        source = self.source.strip() if isinstance(self.source, str) else ""
        remove = bool(self.remove)

        log.info(f"Voice cmd protect invoked user={ctx.user.id} source={source!r} remove={remove}")

        if not source:
            tokens = voice_tts.global_protected_text_tokens()
            for message in self._chunk_messages(tokens):
                await ctx.respond(message)
            log.info(f"Voice cmd protect list user={ctx.user.id} count={len(tokens)}")
            return

        try:
            if remove:
                source_key, removed = voice_tts.remove_global_protected_text_token(source)
                if removed:
                    await ctx.respond(f"Removed protected token: `{source_key}`")
                else:
                    await ctx.respond(f"No protected token set for `{source_key}`.")
                log.info(f"Voice cmd protect remove user={ctx.user.id} source={source_key!r} removed={removed}")
                return

            source_key, existed = voice_tts.add_global_protected_text_token(source)
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd protect rejected user={ctx.user.id} source={source!r} remove={remove} reason={xcp}")
            return

        action = "Already protected" if existed else "Added protected token"
        await ctx.respond(f"{action}: `{source_key}`")
        log.info(f"Voice cmd protect add user={ctx.user.id} source={source_key!r} existed={existed}")


@group_voice.register
class CMD_VoiceAddModel(
    lightbulb.SlashCommand,
    name="addmodel",
    description="Add a custom Piper model from a Hugging Face URL",
):
    _SELECT_TIMEOUT_SECONDS = 90.0
    _SELECT_MAX_OPTIONS = 25

    url = lightbulb.string("url", "Hugging Face repo or .onnx file URL")

    @staticmethod
    def _component_text(value: str, limit: int = 100) -> str:
        text = value.strip() or "-"
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return text[: limit - 3].rstrip() + "..."

    async def _select_candidate(
        self,
        ctx: lightbulb.Context,
        bot: hikari.GatewayBot,
        repo_ref: HFRepoRef,
        candidates: list[str],
    ) -> str | None:
        if len(candidates) > self._SELECT_MAX_OPTIONS:
            await ctx.respond(
                "\n".join(
                    [
                        f"Found `{len(candidates)}` Piper-compatible files in `{repo_ref.repo_id}`.",
                        "Discord select menus support up to 25 options.",
                        "Use a direct file URL (`.../blob/<rev>/<path>.onnx`) to pick one explicitly.",
                    ]
                )
            )
            return None

        custom_id = f"voice-addmodel:{ctx.user.id}:{ctx.interaction.id}"
        row = hikari.impl.MessageActionRowBuilder()
        menu = row.add_text_menu(custom_id, placeholder="Choose a Piper model file", min_values=1, max_values=1)

        for idx, path in enumerate(candidates):
            label = self._component_text(Path(path).name)
            description = self._component_text(path if "/" in path else f"repo:{repo_ref.repo_id}")
            menu.add_option(label, str(idx), description=description)

        response_id = hikari.Snowflake(
            await ctx.respond(
                "\n".join(
                    [
                        f"Found `{len(candidates)}` Piper-compatible model files in `{repo_ref.repo_id}`.",
                        "Select which one to install:",
                    ]
                ),
                components=[row],
                ephemeral=True,
            )
        )

        def pred(event: hikari.InteractionCreateEvent) -> bool:
            interaction = event.interaction
            if not isinstance(interaction, hikari.ComponentInteraction):
                return False
            if interaction.custom_id != custom_id:
                return False
            if interaction.user.id != ctx.user.id:
                return False
            return bool(interaction.message and interaction.message.id == response_id)

        try:
            event = await bot.wait_for(hikari.InteractionCreateEvent, self._SELECT_TIMEOUT_SECONDS, pred)
        except asyncio.TimeoutError:
            await ctx.edit_response(
                response_id,
                "Model selection timed out. Run `/voice addmodel` again.",
                components=[],
            )
            return None

        interaction = event.interaction
        if not isinstance(interaction, hikari.ComponentInteraction) or not interaction.values:
            await ctx.edit_response(response_id, "No model selected.", components=[])
            return None

        choice = interaction.values[0]
        if not choice.isdigit():
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                "Invalid selection. Run `/voice addmodel` again.",
                components=[],
            )
            return None

        index = int(choice)
        if index < 0 or index >= len(candidates):
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_UPDATE,
                "Selection out of range. Run `/voice addmodel` again.",
                components=[],
            )
            return None

        selected_file = candidates[index]
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_UPDATE,
            f"Selected `{Path(selected_file).name}`. Downloading model...",
            components=[],
        )
        return selected_file

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        voice_tts: VoiceTTSService,
        bot: hikari.GatewayBot,
    ):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        await ctx.defer()
        log.info(f"Voice cmd addmodel invoked user={ctx.user.id} url={self.url!r}")

        try:
            repo_ref, candidates = await voice_tts.scan_piper_models_from_hf(self.url)
        except (LookupError, RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd addmodel rejected user={ctx.user.id} reason={xcp}")
            return

        if not candidates:
            await ctx.respond(
                "\n".join(
                    [
                        f"No Piper-compatible models found in `{repo_ref.repo_id}` (revision `{repo_ref.revision}`).",
                        "Expected `.onnx` files with matching Piper `.onnx.json` configs.",
                    ]
                )
            )
            log.info(
                f"Voice cmd addmodel rejected no_candidates user={ctx.user.id} repo={repo_ref.repo_id!r} "
                f"revision={repo_ref.revision!r}"
            )
            return

        selected_file = candidates[0]
        if len(candidates) > 1:
            selected_file = await self._select_candidate(ctx, bot, repo_ref, candidates)
            if not selected_file:
                log.info(
                    f"Voice cmd addmodel cancelled selection user={ctx.user.id} repo={repo_ref.repo_id!r} "
                    f"candidates={len(candidates)}"
                )
                return

        try:
            model_name, has_config = await voice_tts.add_piper_model_from_hf(repo_ref, selected_file)
        except FileExistsError as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd addmodel rejected exists user={ctx.user.id} reason={xcp}")
            return
        except (LookupError, RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd addmodel rejected user={ctx.user.id} reason={xcp}")
            return
        except requests.RequestException as xcp:
            await ctx.respond(f"Failed to download model: {xcp}")
            log.warning(f"Voice cmd addmodel network failure user={ctx.user.id}: {xcp}")
            return

        await ctx.respond(
            "\n".join(
                [
                    f"Added TTS model: `{model_name}`",
                    f"model config: `{'downloaded' if has_config else 'not found'}`",
                    f"Use `/voice set voice:{model_name}` to switch to it.",
                ]
            )
        )
        log.info(
            f"Voice cmd addmodel success user={ctx.user.id} model={model_name!r} config={has_config} "
            f"repo={repo_ref.repo_id!r} file={selected_file!r}"
        )


@group_voice.register
class CMD_VoiceDeleteModel(
    lightbulb.SlashCommand,
    name="delmodel",
    description="Delete a custom Piper model",
):
    model = lightbulb.string(
        "model",
        "Model name to delete",
        autocomplete=ac_tts_custom_models,  # pyright: ignore[reportArgumentType]
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"Voice cmd delmodel invoked user={ctx.user.id} model={self.model!r}")

        try:
            removed = await voice_tts.delete_piper_model(self.model)
        except LookupError:
            await ctx.respond(f"Unknown model `{self.model}`. Use the `model` autocomplete.")
            log.info(f"Voice cmd delmodel rejected missing user={ctx.user.id} model={self.model!r}")
            return
        except (RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd delmodel rejected user={ctx.user.id} reason={xcp}")
            return

        await ctx.respond(f"Deleted TTS model `{removed}`.")
        log.info(f"Voice cmd delmodel success user={ctx.user.id} model={removed!r}")


__all__ = [
    "CMD_VoiceAddModel",
    "CMD_VoiceAutocorrect",
    "CMD_VoiceDeleteModel",
    "CMD_VoiceGlobalSub",
    "CMD_VoiceList",
    "CMD_VoiceListen",
    "CMD_VoicePron",
    "CMD_VoiceProtect",
    "CMD_VoiceSay",
    "CMD_VoiceSet",
    "CMD_VoiceSub",
    "HFRepoRef",
    "PiperPythonVoiceRuntime",
    "SpeechContent",
    "VoiceJob",
    "VoiceRuntimeResetResult",
    "VoiceTTSService",
    "ac_tts_custom_models",
    "ac_tts_global_substitution_sources",
    "ac_tts_pronunciation_sources",
    "ac_tts_protected_tokens",
    "ac_tts_substitution_sources",
    "ac_tts_variants",
    "ac_tts_voices",
    "group_voice",
]
# AiviA APasz
