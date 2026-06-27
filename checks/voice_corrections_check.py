from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import hikari
from modmux import SteamCreds
from modmux.providers.curseforge import CurseforgeCreds
from modmux.providers.modio import ModioCreds
from modmux.providers.modrinth import ModrinthCreds
from modmux.providers.nexusmods import NexusCreds
from modmux.providers.wube import WubeCreds
from pydantic import SecretStr

import config
from cmd_voice import (
    VoiceAdminActionKind,
    VoiceAdminChannelsView,
    VoiceAdminEditorService,
    VoiceAdminLinksView,
    VoiceAdminPronunciationView,
    VoiceAdminSection,
    VoiceAdminSelectionState,
    VoiceAdminState,
    VoiceAdminSubstitutionCategory,
    VoiceSettingsActionKind,
    VoiceSettingsEditorService,
    VoiceSettingsSection,
    VoiceSettingsState,
    _builtin_voice_names,
    _normalise_voice_source,
    _voice_admin_state_from_action,
    _voice_admin_state_value,
    _voice_settings_state_from_action,
    _voice_settings_state_value,
)
from cmd_voice_common import (
    MAX_TTS_VOICES,
    HFRepoRef,
    PronunciationFormat,
    PronunciationOverride,
    TextCorrectionCatalog,
    TextSubstitutionRule,
    UserVoiceSettings,
    VoiceLinkRule,
    VoiceLinkRules,
)
from cmd_voice_core import VoiceTTSCoreMixin
from cmd_voice_model import VoiceTTSModelMixin
from cmd_voice_runtime import VoiceTTSRuntimeMixin
from cmd_voice_service import VoiceTTSService


class _CorrectionRuntime(VoiceTTSRuntimeMixin):
    _FUZZY_AUTOCORRECT_MIN_LEN = 4
    _MAX_SPOKEN_CHARS = 550
    _MAX_SUBSTITUTION_KEY_CHARS = 256

    def __init__(
        self,
        *,
        catalog: TextCorrectionCatalog,
        user_settings: dict[int, UserVoiceSettings] | None = None,
    ) -> None:
        self._text_corrections = catalog
        self._user_settings = user_settings or {}
        self._voice_link_rules = VoiceLinkRules()
        self.voice = "default-voice"
        self.variant = None

    def _refresh_voice_link_rules_if_needed(self) -> None:
        return None

    async def _mod_link_name(self, url: str) -> str | None:
        return None

    def user_voice_variant(self, user_id: int) -> tuple[str, str | None]:
        settings = self._user_settings.get(user_id)
        if settings and settings.voice:
            return settings.voice, settings.variant
        return self.voice, self.variant

    def global_pronunciations(self, voice: str | None = None) -> dict[str, PronunciationOverride]:
        selected_voice = self.voice if voice is None else voice
        entries = self._text_corrections.pronunciations.get(selected_voice, {})
        return dict(sorted(entries.items()))

    def user_pronunciation_overrides(
        self,
        user_id: int,
        voice: str | None = None,
    ) -> dict[str, PronunciationOverride]:
        settings = self._user_settings.get(user_id)
        if not settings or not settings.pronunciations:
            return {}
        selected_voice = self.voice if voice is None else voice
        entries = settings.pronunciations.get(selected_voice, {})
        return dict(sorted(entries.items()))

    def user_pronunciations(self, user_id: int, voice: str | None = None) -> dict[str, PronunciationOverride]:
        selected_voice = self.voice if voice is None else voice
        merged = self.global_pronunciations(selected_voice)
        merged.update(self.user_pronunciation_overrides(user_id, selected_voice))
        return dict(sorted(merged.items()))

    def voice_supports_ipa_pronunciations(self, voice: str) -> bool:
        return voice == self.voice

class _CorrectionCore(VoiceTTSCoreMixin, VoiceTTSModelMixin):
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 256
    _MAX_SUBSTITUTION_VALUE_CHARS = 120

    def __init__(self, path: Path) -> None:
        self._bot_configuration_path = path.with_name("configuration.json")
        self._corrections_path = path
        self._voice_link_rules_path = path.with_name("voice_link_rules.json")
        self._voice_target_name_cache = {}
        self._voice_targets = {}
        self._voice_target_choices_dirty = False
        self._enabled = False
        self._text_corrections = TextCorrectionCatalog()
        self._voice_link_rules = VoiceLinkRules()
        self._voice_link_rules_mtime_ns = None
        self._mod_link_name_cache = {}
        self._modmux = None


class _CorrectionLinkRuntime(VoiceTTSService):
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 256
    _MAX_SUBSTITUTION_VALUE_CHARS = 120
    _FUZZY_AUTOCORRECT_MIN_LEN = 4
    _MAX_SPOKEN_CHARS = 550

    def __init__(self, path: Path) -> None:
        self._corrections_path = path
        self._voice_link_rules_path = path.with_name("voice_link_rules.json")
        self._text_corrections = TextCorrectionCatalog()
        self._user_settings = {}
        self._voice_link_rules = VoiceLinkRules()
        self._voice_link_rules_mtime_ns = None
        self._mod_link_name_cache = {}
        self._modmux = None
        self.voice = "default-voice"
        self.variant = None


class _CorrectionAsyncRuntime(_CorrectionRuntime):
    def __init__(self, *, catalog: TextCorrectionCatalog, mod_names: dict[str, str]) -> None:
        super().__init__(catalog=catalog)
        self._mod_names = mod_names

    async def _mod_link_name(self, url: str) -> str | None:
        return self._mod_names.get(url)


class _HFScanRuntime(VoiceTTSCoreMixin, VoiceTTSModelMixin):
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 256
    _MAX_SUBSTITUTION_VALUE_CHARS = 120
    repo_files_called = False
    candidate_result = False

    def __init__(self, *, is_candidate: bool) -> None:
        self._engine_kind = "piper"
        type(self).candidate_result = is_candidate
        type(self).repo_files_called = False

    @staticmethod
    def _hf_parse_repo_url(url: str):
        return VoiceTTSModelMixin._hf_parse_repo_url(url)

    @staticmethod
    def _hf_repo_files(repo_id: str, revision: str) -> list[str]:
        _HFScanRuntime.repo_files_called = True
        raise AssertionError("Direct model URL should not enumerate repository files.")

    @classmethod
    def _hf_is_piper_file_candidate(cls, repo_id: str, revision: str, onnx_file: str) -> bool:
        return cls.candidate_result


async def _immediate_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


def _substitution_rule(source: str, target: str, *, case_sensitive: bool = False) -> TextSubstitutionRule:
    return TextSubstitutionRule(source=source, target=target, case_sensitive=case_sensitive)


def _substitution_rules(*entries: tuple[str, str, bool]) -> dict[str, TextSubstitutionRule]:
    return {
        source: _substitution_rule(source, target, case_sensitive=case_sensitive)
        for source, target, case_sensitive in entries
    }


class _VoiceAdminRenderStub(VoiceTTSService):
    def __init__(
        self,
        *,
        target: config.VoiceTargetConfig | None = None,
        substitutions: dict[str, dict[str, TextSubstitutionRule]] | None = None,
        link_rules: tuple[VoiceLinkRule, ...] = (),
    ) -> None:
        self.voice = "default-voice"
        self.variant = None
        self._target = target
        self._substitutions = substitutions or {}
        self._link_rules = link_rules

    def _engine_display(self) -> str:
        return "stub"

    def available_custom_voices(self) -> list[str]:
        return []

    async def available_voices(self, force_refresh: bool = False) -> list[str]:
        return [self.voice]

    def global_mention_overrides(self) -> dict[int, str]:
        return {}

    def global_text_substitutions(self, category: str | None = None) -> dict[str, TextSubstitutionRule]:
        if category is None:
            return {}
        return self._substitutions.get(category, {})

    def all_global_pronunciations(self) -> dict[str, dict[str, PronunciationOverride]]:
        return {}

    def global_protected_text_tokens(self) -> list[str]:
        return []

    def voice_link_host_labels(self) -> dict[str, str]:
        return {}

    def voice_link_rules(self) -> tuple[VoiceLinkRule, ...]:
        return self._link_rules

    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None:
        return self._target


class _VoiceNoticeRuntime(VoiceTTSService):
    def __init__(
        self,
        *,
        targets: dict[hikari.Snowflake, config.VoiceTargetConfig],
        connections: list[SimpleNamespace],
    ) -> None:
        self.bot = SimpleNamespace(rest=SimpleNamespace(create_message=AsyncMock()))
        self._targets = targets
        self._connections = connections

    def active_voice_connections(self) -> list[SimpleNamespace]:
        return list(self._connections)

    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None:
        return self._targets.get(hikari.Snowflake(guild_id))


class VoiceCorrectionTests(unittest.TestCase):
    def test_command_source_normalises_unicode_emoji(self) -> None:
        source = _normalise_voice_source("😭")

        self.assertEqual(source.key, "loudly_crying_face")
        self.assertTrue(source.is_emoji)
        self.assertEqual(source.display(), "`😭` (`loudly_crying_face`)")

    def test_command_source_normalises_custom_emoji(self) -> None:
        source = _normalise_voice_source("<:sob:123456>")

        self.assertEqual(source.key, "sob")
        self.assertTrue(source.is_emoji)
        self.assertEqual(source.display(), "`<:sob:123456>` (`sob`)")

    def test_builtin_voice_names_excludes_custom_models_case_insensitively(self) -> None:
        voices = ["en-us", "custom-voice", "En-GB", "CUSTOM-VOICE-2"]
        custom_models = ["Custom-Voice", "custom-voice-2"]

        self.assertEqual(_builtin_voice_names(voices, custom_models), ["en-us", "En-GB"])

    def test_exact_slang_expands_before_fuzzy_autocorrect(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang=_substitution_rules(("idk", "I don't know", False)),
                typos=_substitution_rules(("definately", "definitely", False)),
                fuzzy_targets=("definitely",),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("idk definately").render(), "I don't know definitely")

    def test_pronunciation_override_precedes_substitution(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={
                123: UserVoiceSettings(
                    pronunciations={
                        "default-voice": {
                            "egg": PronunciationOverride(
                                format=PronunciationFormat.TEXT,
                                value="ehg",
                            )
                        }
                    },
                    substitutions=_substitution_rules(("egg", "egg replacement", False)),
                )
            },
        )

        self.assertEqual(runtime._normalise_for_speech("egg", user_id=123).render(), "ehg")

    def test_exact_global_correction_can_match_full_punctuation_token(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang=_substitution_rules(("w/", "with", False)),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("w/").render(), "with")

    def test_notify_connected_tts_channels_targets_primary_channel_for_each_connected_guild(self) -> None:
        runtime = _VoiceNoticeRuntime(
            targets={
                hikari.Snowflake(1): config.VoiceTargetConfig(
                    guild_id=hikari.Snowflake(1),
                    voice_channel=hikari.Snowflake(11),
                    primary_tts_channel=hikari.Snowflake(101),
                ),
                hikari.Snowflake(2): config.VoiceTargetConfig(
                    guild_id=hikari.Snowflake(2),
                    voice_channel=hikari.Snowflake(22),
                    primary_tts_channel=hikari.Snowflake(202),
                ),
            },
            connections=[
                SimpleNamespace(guild_id=hikari.Snowflake(1), channel_id=hikari.Snowflake(11)),
                SimpleNamespace(guild_id=hikari.Snowflake(2), channel_id=hikari.Snowflake(22)),
                SimpleNamespace(guild_id=hikari.Snowflake(1), channel_id=hikari.Snowflake(11)),
            ],
        )

        sent_count = asyncio.run(runtime.notify_connected_tts_channels("Scheduled maintenance: restart in 1m."))
        create_message = runtime.bot.rest.create_message

        self.assertEqual(sent_count, 2)
        self.assertEqual(create_message.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in create_message.await_args_list],
            [hikari.Snowflake(101), hikari.Snowflake(202)],
        )
        self.assertTrue(
            all(
                call.kwargs["flags"] == hikari.MessageFlag.SUPPRESS_NOTIFICATIONS
                for call in create_message.await_args_list
            )
        )

    def test_user_substitution_applies_to_emoji_name(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={123: UserVoiceSettings(substitutions=_substitution_rules(("sob", "crying", False)))},
        )

        self.assertEqual(runtime._normalise_for_speech(":sob:", user_id=123).render(), "crying")

    def test_case_sensitive_user_substitution_only_matches_exact_case(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={
                123: UserVoiceSettings(
                    substitutions=_substitution_rules(("LOL", "laughing out loud", True)),
                )
            },
        )

        self.assertEqual(runtime._normalise_for_speech("LOL lol", user_id=123).render(), "laughing out loud lol")

    def test_case_sensitive_global_substitution_precedes_case_insensitive_match(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang=_substitution_rules(("hello", "hi", False), ("Hello", "greetings", True)),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("Hello hello HELLO").render(), "greetings hi HI")

    def test_user_pronunciation_applies_to_emoji_name(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={
                123: UserVoiceSettings(
                    pronunciations={
                        "default-voice": {
                            "sob": PronunciationOverride(
                                format=PronunciationFormat.TEXT,
                                value="sobbing",
                            )
                        }
                    }
                )
            },
        )

        self.assertEqual(runtime._normalise_for_speech(":sob:", user_id=123).render(), "sobbing")

    def test_global_pronunciation_forms_basis_for_user_override(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                pronunciations={
                    "default-voice": {
                        "egg": PronunciationOverride(
                            format=PronunciationFormat.TEXT,
                            value="ehg",
                        )
                    }
                }
            ),
            user_settings={
                123: UserVoiceSettings(
                    pronunciations={
                        "default-voice": {
                            "egg": PronunciationOverride(
                                format=PronunciationFormat.TEXT,
                                value="ayg",
                            )
                        }
                    }
                )
            },
        )

        self.assertEqual(runtime._normalise_for_speech("egg", user_id=999).render(), "ehg")
        self.assertEqual(runtime._normalise_for_speech("egg", user_id=123).render(), "ayg")

    def test_global_substitution_applies_to_emoji_name(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang=_substitution_rules(("thumbs_up", "nice", False)),
            )
        )

        self.assertEqual(runtime._normalise_for_speech(":thumbs_up:").render(), "nice")

    def test_emoji_substitution_preserves_repeat_count(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang=_substitution_rules(("skull", "dead", False)),
            )
        )

        self.assertEqual(runtime._normalise_for_speech(":skull: :skull:").render(), "dead x2")

    def test_user_mention_override_replaces_resolved_name(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={123: UserVoiceSettings(mention_overrides={456: "bee"})},
        )
        fake_bot = SimpleNamespace(
            cache=SimpleNamespace(
                get_member=lambda guild_id, user_id: None,
                get_user=lambda user_id: None,
            )
        )
        setattr(runtime, "bot", fake_bot)
        event = cast(
            hikari.GuildMessageCreateEvent,
            SimpleNamespace(
            author_id=hikari.Snowflake(123),
            guild_id=hikari.Snowflake(1),
            message=SimpleNamespace(
                get_member_mentions=lambda: {
                    hikari.Snowflake(456): SimpleNamespace(display_name="Bee Display", username="BeeUser")
                },
                user_mentions=hikari.UNDEFINED,
                channel_mentions=hikari.UNDEFINED,
            ),
            ),
        )

        self.assertEqual(runtime._replace_mentions_with_names("<@456>", event).strip(), "bee")

    def test_global_mention_override_only_applies_when_display_name_matches_username(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(mention_overrides={456: "bee"}),
        )
        fake_bot = SimpleNamespace(
            cache=SimpleNamespace(
                get_member=lambda guild_id, user_id: None,
                get_user=lambda user_id: None,
            )
        )
        setattr(runtime, "bot", fake_bot)
        event = cast(
            hikari.GuildMessageCreateEvent,
            SimpleNamespace(
            author_id=hikari.Snowflake(123),
            guild_id=hikari.Snowflake(1),
            message=SimpleNamespace(
                get_member_mentions=lambda: {
                    hikari.Snowflake(456): SimpleNamespace(display_name="BeeUser", username="BeeUser")
                },
                user_mentions=hikari.UNDEFINED,
                channel_mentions=hikari.UNDEFINED,
            ),
            ),
        )

        self.assertEqual(runtime._replace_mentions_with_names("<@456>", event).strip(), "bee")

    def test_discord_timestamp_becomes_time_code(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("<t:1777333938:S>").render(), "time code")

    def test_discord_markdown_labels_are_spoken(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        spoken = runtime._normalise_for_speech("~~gone~~ __here__ **loud** *soft* ||secret|| `ping`").render()

        self.assertEqual(
            spoken,
            "strikethrough gone underline here bold loud italic soft spoiler secret code ping",
        )

    def test_nested_discord_markdown_preserves_multiple_labels(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("__**alert**__").render(), "underline bold alert")

    def test_discord_headings_are_spoken(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        spoken = runtime._normalise_for_speech("# top\n## middle\n### low\n-# note").render()

        self.assertEqual(spoken, "heading top heading 2 middle heading 3 low subtext note")

    def test_generic_link_becomes_domain(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("https://example.com/path?q=1").render(), "link example")

    def test_generic_non_com_link_keeps_tld(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("https://example.net/path?q=1").render(), "link example.net")

    def test_shortened_youtube_link_uses_youtube_name(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("https://youtu.be/abc123").render(), "link youtube")

    def test_gif_hosts_use_gif_label(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionLinkRuntime(Path(tmp) / "voice_corrections.json")
            runtime._ensure_voice_link_rules_file()
            runtime._refresh_voice_link_rules_if_needed()

            self.assertEqual(runtime._normalise_for_speech("https://tenor.com/view/test").render(), "gif")
            self.assertEqual(runtime._normalise_for_speech("https://www.giphy.com/gifs/test").render(), "gif")
            self.assertEqual(runtime._normalise_for_speech("https://klipy.com/clip/test").render(), "gif")

    def test_user_url_substitution_overrides_generic_link_speech(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={
                123: UserVoiceSettings(
                    substitutions=_substitution_rules(
                        ("https://tenor.com/view/funny-cat-reaction-gif-123456", "funny cat gif", False)
                    )
                )
            },
        )

        self.assertEqual(
            runtime._normalise_for_speech("https://tenor.com/view/funny-cat-reaction-gif-123456", user_id=123).render(),
            "funny cat gif",
        )

    def test_compact_minutes_are_expanded(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("5m").render(), "5 minutes")
        self.assertEqual(runtime._normalise_for_speech("1m, brb").render(), "1 minute, brb")

    def test_other_compact_units_are_expanded(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("5s 2h 3d 5km").render(), "5 seconds 2 hours 3 days 5 kilometers")
        self.assertEqual(runtime._normalise_for_speech("(1km)").render(), "(1 kilometer)")
        self.assertEqual(runtime._normalise_for_speech("1.5h").render(), "1.5 hours")

    def test_ordinals_are_not_misread_as_compact_units(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("1st 2nd 3rd 4th").render(), "1st 2nd 3rd 4th")

    def test_slash_ratio_is_spoken_as_out_of(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("5/10").render(), "5 out of 10")
        self.assertEqual(runtime._normalise_for_speech("(7/10)").render(), "(7 out of 10)")
        self.assertEqual(runtime._normalise_for_speech("5.5/10").render(), "5.5 out of 10")

    def test_currency_symbols_are_expanded(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(
            runtime._normalise_for_speech("$1 and 2£ plus 3 € and €4.5").render(),
            "1 dollar and 2 pounds plus 3 euros and 4.5 euros",
        )

    def test_common_with_shorthand_is_expanded(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("w/ fries, w/o sauce").render(), "with fries, without sauce")

    def test_attachment_only_message_uses_attachment_placeholder(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._message_speech_input("", attachment_count=1), "attachment")
        self.assertEqual(runtime._message_speech_input("hello", attachment_count=1), "hello")
        self.assertEqual(runtime._message_speech_input("", attachment_count=0, sticker_count=1), "sticker")
        self.assertEqual(runtime._message_speech_input("", attachment_count=1, sticker_count=1), "attachment, sticker")
        self.assertEqual(
            runtime._message_speech_input(
                "",
                attachment_count=0,
                sticker_count=2,
                sticker_names=(":party_parrot:",),
            ),
            ":party_parrot:, sticker",
        )
        self.assertEqual(runtime._message_speech_input("", attachment_count=0), "")
        self.assertEqual(runtime._message_speech_input("hello", attachment_count=0, is_reply=True), "is reply... hello")
        self.assertEqual(
            runtime._message_speech_input("hello", attachment_count=0, is_forward=True),
            "is forwarded... hello",
        )
        self.assertEqual(
            runtime._message_speech_input(
                "check this",
                attachment_count=0,
                is_forward=True,
                forwarded_content="forwarded body, attachment, sticker",
            ),
            "is forwarded... check this... forwarded body, attachment, sticker",
        )

    def test_sticker_names_are_normalised_into_emoji_style_speech_fragments(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(
            runtime._sticker_speech_fragments(
                (
                    SimpleNamespace(name="Party Parrot!!!"),
                    SimpleNamespace(name="blob-dance"),
                    SimpleNamespace(name="   "),
                    SimpleNamespace(),
                )
            ),
            (":party_parrot:", ":blob-dance:"),
        )
        self.assertEqual(runtime._normalise_for_speech(":party_parrot:").render(), "party parrot")

    def test_forwarded_snapshot_content_includes_content_and_extras(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())
        setattr(
            runtime,
            "bot",
            SimpleNamespace(
                cache=SimpleNamespace(
                    get_member=lambda guild_id, user_id: None,
                    get_user=lambda user_id: None,
                    get_guild_channel=lambda channel_id: None,
                )
            ),
        )
        message = cast(
            hikari.Message,
            SimpleNamespace(
                message_snapshots=(
                    SimpleNamespace(
                        content="hello there",
                        attachments=(object(),),
                        stickers=(
                            SimpleNamespace(name="Party Parrot"),
                            SimpleNamespace(name="blob-dance"),
                        ),
                        user_mentions=hikari.UNDEFINED,
                    ),
                )
            ),
        )

        self.assertEqual(
            runtime._forwarded_snapshot_speech_input(message, hikari.Snowflake(123), hikari.Snowflake(1)),
            "hello there, attachment, :party_parrot:, :blob-dance:",
        )

    def test_forwarded_snapshot_mentions_use_overrides(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(mention_overrides={456: "bee global"}),
            user_settings={123: UserVoiceSettings(mention_overrides={456: "bee user"})},
        )
        setattr(
            runtime,
            "bot",
            SimpleNamespace(
                cache=SimpleNamespace(
                    get_member=lambda guild_id, user_id: None,
                    get_user=lambda user_id: None,
                    get_guild_channel=lambda channel_id: None,
                )
            ),
        )
        message = cast(
            hikari.Message,
            SimpleNamespace(
                message_snapshots=(
                    SimpleNamespace(
                        content="hi <@456>",
                        attachments=(),
                        stickers=(),
                        user_mentions={
                            hikari.Snowflake(456): SimpleNamespace(display_name="BeeUser", username="BeeUser")
                        },
                    ),
                )
            ),
        )

        self.assertEqual(
            runtime._forwarded_snapshot_speech_input(message, hikari.Snowflake(123), hikari.Snowflake(1)),
            "hi  bee user",
        )

    def test_voice_settings_state_round_trips(self) -> None:
        state = VoiceSettingsState(section=VoiceSettingsSection.MENTIONS, page=3)
        action = SimpleNamespace(page=state.page, value=_voice_settings_state_value(state))

        self.assertEqual(_voice_settings_state_from_action(action), state)

    def test_voice_admin_state_round_trips(self) -> None:
        state = VoiceAdminState(
            section=VoiceAdminSection.MENTIONS,
            page=2,
            substitution_category=VoiceAdminSubstitutionCategory.TYPO,
            links_view=VoiceAdminLinksView.RULES,
            pronunciation_view=VoiceAdminPronunciationView.CREATE,
        )
        action = SimpleNamespace(page=state.page, value=_voice_admin_state_value(state))

        self.assertEqual(_voice_admin_state_from_action(action), state)

    def test_voice_settings_mention_custom_ids_fit_discord_limit(self) -> None:
        service = VoiceSettingsEditorService()
        user_id = hikari.Snowflake(123456789012345678)
        state = VoiceSettingsState(section=VoiceSettingsSection.MENTIONS, page=0)
        editor_ctx = service._editor.context(
            scope_id=user_id,
            user_id=user_id,
            locale=hikari.Locale.EN_GB,
        )

        select_action = service._build_state_action(VoiceSettingsActionKind.SELECT_MENTION_TARGET, state)
        edit_action = service._action_codec.build(
            VoiceSettingsActionKind.EDIT_MENTION_OVERRIDE,
            page=state.page,
            value=str(int(user_id)),
        )

        self.assertLessEqual(len(editor_ctx.custom_id(select_action)), 100)
        self.assertLessEqual(
            len(service._mention_modal.build_id(edit_action, scope_id=user_id, user_id=user_id)),
            100,
        )

    def test_voice_admin_mention_custom_ids_fit_discord_limit(self) -> None:
        service = VoiceAdminEditorService()
        user_id = hikari.Snowflake(123456789012345678)
        state = VoiceAdminState(
            section=VoiceAdminSection.MENTIONS,
            page=0,
            substitution_category=VoiceAdminSubstitutionCategory.SLANG,
            links_view=VoiceAdminLinksView.HOSTS,
            pronunciation_view=VoiceAdminPronunciationView.LIST,
        )
        editor_ctx = service._editor.context(
            scope_id=user_id,
            user_id=user_id,
            locale=hikari.Locale.EN_GB,
        )

        select_action = service._build_state_action(VoiceAdminActionKind.SELECT_MENTION_TARGET, state)
        edit_action = service._action_codec.build(
            VoiceAdminActionKind.EDIT_MENTION_OVERRIDE,
            page=state.page,
            value=f"{_voice_admin_state_value(state)}~{int(user_id)}",
        )

        self.assertLessEqual(len(editor_ctx.custom_id(select_action)), 100)
        self.assertLessEqual(
            len(service._mention_modal.build_id(edit_action, scope_id=user_id, user_id=user_id)),
            100,
        )

    def test_voice_settings_substitution_modal_ids_ignore_long_selection_values(self) -> None:
        service = VoiceSettingsEditorService()
        user_id = hikari.Snowflake(123456789012345678)
        state = VoiceSettingsState(section=VoiceSettingsSection.SUBSTITUTIONS, page=0)
        editor_ctx = service._editor.context(scope_id=user_id, user_id=user_id, locale=hikari.Locale.EN_GB)

        select_action = service._action_codec.build(
            VoiceSettingsActionKind.SELECT_SUBSTITUTION,
            page=state.page,
            value=state.section.value,
        )
        edit_action = service._action_codec.build(
            VoiceSettingsActionKind.EDIT_SUBSTITUTION,
            page=state.page,
            value=str(int(user_id)),
        )

        self.assertLessEqual(len(editor_ctx.custom_id(select_action)), 100)
        self.assertLessEqual(
            len(service._substitution_modal.build_id(edit_action, scope_id=user_id, user_id=user_id)),
            100,
        )

    def test_voice_admin_pronunciation_modal_ids_ignore_long_selection_values(self) -> None:
        service = VoiceAdminEditorService()
        user_id = hikari.Snowflake(123456789012345678)
        state = VoiceAdminState(
            section=VoiceAdminSection.PRONUNCIATIONS,
            page=0,
            substitution_category=VoiceAdminSubstitutionCategory.SLANG,
            links_view=VoiceAdminLinksView.HOSTS,
            pronunciation_view=VoiceAdminPronunciationView.LIST,
        )
        editor_ctx = service._editor.context(scope_id=user_id, user_id=user_id, locale=hikari.Locale.EN_GB)

        select_action = service._action_codec.build(
            VoiceAdminActionKind.SELECT_PRONUNCIATION,
            page=state.page,
            value=_voice_admin_state_value(state),
        )
        edit_action = service._action_codec.build(
            VoiceAdminActionKind.EDIT_PRONUNCIATION,
            page=state.page,
            value=f"{_voice_admin_state_value(state)}~{int(user_id)}",
        )

        self.assertLessEqual(len(editor_ctx.custom_id(select_action)), 100)
        self.assertLessEqual(
            len(service._pronunciation_modal.build_id(edit_action, scope_id=user_id, user_id=user_id)),
            100,
        )

    def test_voice_admin_link_host_modal_ids_ignore_long_selection_values(self) -> None:
        service = VoiceAdminEditorService()
        user_id = hikari.Snowflake(123456789012345678)
        state = VoiceAdminState(
            section=VoiceAdminSection.LINKS,
            page=0,
            substitution_category=VoiceAdminSubstitutionCategory.SLANG,
            links_view=VoiceAdminLinksView.HOSTS,
            pronunciation_view=VoiceAdminPronunciationView.LIST,
        )
        editor_ctx = service._editor.context(scope_id=user_id, user_id=user_id, locale=hikari.Locale.EN_GB)

        select_action = service._action_codec.build(
            VoiceAdminActionKind.SELECT_LINK_HOST,
            page=state.page,
            value=_voice_admin_state_value(state),
        )
        edit_action = service._action_codec.build(
            VoiceAdminActionKind.EDIT_LINK_HOST,
            page=state.page,
            value=f"{_voice_admin_state_value(state)}~{int(user_id)}",
        )

        self.assertLessEqual(len(editor_ctx.custom_id(select_action)), 100)
        self.assertLessEqual(
            len(service._link_host_modal.build_id(edit_action, scope_id=user_id, user_id=user_id)),
            100,
        )

    def test_voice_admin_link_rule_modal_ids_fit_discord_limit(self) -> None:
        service = VoiceAdminEditorService()
        user_id = hikari.Snowflake(123456789012345678)
        state = VoiceAdminState(
            section=VoiceAdminSection.LINKS,
            page=0,
            substitution_category=VoiceAdminSubstitutionCategory.SLANG,
            links_view=VoiceAdminLinksView.RULES,
            pronunciation_view=VoiceAdminPronunciationView.LIST,
        )
        editor_ctx = service._editor.context(scope_id=user_id, user_id=user_id, locale=hikari.Locale.EN_GB)

        select_action = service._action_codec.build(
            VoiceAdminActionKind.SELECT_LINK_RULE,
            page=state.page,
            value=_voice_admin_state_value(state),
        )
        edit_action = service._action_codec.build(
            VoiceAdminActionKind.EDIT_LINK_RULE,
            page=state.page,
            value=f"{_voice_admin_state_value(state)}~{int(user_id)}",
        )
        add_simple_action = service._action_codec.build(
            VoiceAdminActionKind.ADD_SIMPLE_LINK_RULE,
            page=state.page,
            value=f"{_voice_admin_state_value(state)}~{int(user_id)}",
        )
        add_complex_action = service._action_codec.build(
            VoiceAdminActionKind.ADD_COMPLEX_LINK_RULE,
            page=state.page,
            value=f"{_voice_admin_state_value(state)}~{int(user_id)}",
        )

        self.assertLessEqual(len(editor_ctx.custom_id(select_action)), 100)
        self.assertLessEqual(len(editor_ctx.custom_id(add_simple_action)), 100)
        self.assertLessEqual(len(editor_ctx.custom_id(add_complex_action)), 100)
        self.assertLessEqual(
            len(service._link_rule_modal.build_id(edit_action, scope_id=user_id, user_id=user_id)),
            100,
        )
        self.assertLessEqual(
            len(service._link_rule_modal.build_id(add_simple_action, scope_id=user_id, user_id=user_id)),
            100,
        )
        self.assertLessEqual(
            len(service._link_rule_modal.build_id(add_complex_action, scope_id=user_id, user_id=user_id)),
            100,
        )

    def test_voice_admin_channels_subpage_renders_with_back_action(self) -> None:
        service = VoiceAdminEditorService()
        state = VoiceAdminState(section=VoiceAdminSection.CHANNELS, page=0)
        voice_tts = _VoiceAdminRenderStub(
            target=config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(123),
                voice_channel=hikari.Snowflake(456),
                primary_tts_channel=hikari.Snowflake(789),
                secondary_tts_channel=hikari.Snowflake(790),
                relay_tts_enabled=True,
            )
        )

        embed, components = asyncio.run(
            service._render_editor(
                actor_user_id=hikari.Snowflake(123456789012345678),
                locale=hikari.Locale.EN_GB,
                guild_id=hikari.Snowflake(123),
                voice_tts=voice_tts,
                state=state,
            )
        )

        self.assertEqual(embed.title, "Voice Admin")
        self.assertGreaterEqual(len(components), 1)
        self.assertLessEqual(len(components), 5)
        self.assertTrue(all(len(getattr(row, "components", ())) <= 5 for row in components))
        self.assertEqual(embed.fields[0].name, "Current Guild Channels")
        self.assertIn("primary tts channel: <#789> (listening)", embed.fields[0].value)
        self.assertIn("secondary tts channel: <#790>", embed.fields[0].value)
        self.assertIn("relay tts: enabled", embed.fields[0].value)

    def test_voice_admin_channel_config_subpage_renders_within_discord_component_limits(self) -> None:
        service = VoiceAdminEditorService()
        state = VoiceAdminState(
            section=VoiceAdminSection.CHANNELS,
            page=0,
            channels_view=VoiceAdminChannelsView.CONFIG,
        )
        voice_tts = _VoiceAdminRenderStub(
            target=config.VoiceTargetConfig(
                guild_id=hikari.Snowflake(123),
                voice_channel=hikari.Snowflake(456),
                primary_tts_channel=hikari.Snowflake(789),
                secondary_tts_channel=hikari.Snowflake(790),
                relay_tts_enabled=True,
            )
        )

        embed, components = asyncio.run(
            service._render_editor(
                actor_user_id=hikari.Snowflake(123456789012345678),
                locale=hikari.Locale.EN_GB,
                guild_id=hikari.Snowflake(123),
                voice_tts=voice_tts,
                state=state,
            )
        )

        self.assertEqual(embed.title, "Voice Admin")
        self.assertLessEqual(len(components), 5)
        self.assertTrue(all(len(getattr(row, "components", ())) <= 5 for row in components))
        self.assertEqual(embed.fields[1].name, "Channel Config")

    def test_voice_admin_paginated_substitutions_render_without_footer_overflow(self) -> None:
        service = VoiceAdminEditorService()
        session_message_id = hikari.Snowflake(987654321012345678)
        state = VoiceAdminState(
            section=VoiceAdminSection.SUBSTITUTIONS,
            page=0,
            substitution_category=VoiceAdminSubstitutionCategory.SLANG,
        )
        substitutions = {
            f"term-{index:02d}": _substitution_rule(f"term-{index:02d}", f"value-{index:02d}") for index in range(26)
        }
        voice_tts = _VoiceAdminRenderStub(substitutions={VoiceAdminSubstitutionCategory.SLANG.value: substitutions})
        service._selection_state[session_message_id] = VoiceAdminSelectionState(substitution_source="term-00")

        embed, components = asyncio.run(
            service._render_editor(
                actor_user_id=hikari.Snowflake(123456789012345678),
                locale=hikari.Locale.EN_GB,
                guild_id=hikari.Snowflake(123),
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
            )
        )

        self.assertEqual(embed.title, "Voice Admin")
        self.assertGreaterEqual(len(components), 1)

    def test_voice_admin_link_rules_render_selected_rule_field(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            _, rule = runtime.add_voice_link_rule(
                "",
                "",
                "link {host} {title_norm}",
                mode="simple",
                example_url="https://example.com/games/test",
            )

            service = VoiceAdminEditorService()
            session_message_id = hikari.Snowflake(987654321012345678)
            state = VoiceAdminState(
                section=VoiceAdminSection.LINKS,
                page=0,
                links_view=VoiceAdminLinksView.RULES,
            )
            voice_tts = _VoiceAdminRenderStub(link_rules=(rule,))
            service._selection_state[session_message_id] = VoiceAdminSelectionState(link_rule_index=1)

            embed, components = asyncio.run(
                service._render_editor(
                    actor_user_id=hikari.Snowflake(123456789012345678),
                    locale=hikari.Locale.EN_GB,
                    guild_id=hikari.Snowflake(123),
                    voice_tts=voice_tts,
                    state=state,
                    session_message_id=session_message_id,
                )
            )

        field_names = [field.name for field in embed.fields]
        self.assertIn("Selected Rule", field_names)
        self.assertGreaterEqual(len(components), 1)

    def test_steam_store_link_uses_store_title(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionLinkRuntime(Path(tmp) / "voice_corrections.json")
            runtime._ensure_voice_link_rules_file()
            runtime._refresh_voice_link_rules_if_needed()

            spoken = runtime._normalise_for_speech(
                "https://store.steampowered.com/app/3493540/Transport_Fever_2/"
            ).render()

            self.assertEqual(spoken, "link steam store Transport Fever 2")

    def test_async_mod_link_name_replaces_generic_link_text(self) -> None:
        runtime = _CorrectionAsyncRuntime(
            catalog=TextCorrectionCatalog(),
            mod_names={"https://modrinth.com/mod/sodium": "Sodium"},
        )

        spoken = asyncio.run(runtime._normalise_for_speech_async("install https://modrinth.com/mod/sodium now")).render()

        self.assertEqual(spoken, "install mod Sodium now")

    def test_hot_reloaded_host_link_label_is_applied_without_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionLinkRuntime(Path(tmp) / "voice_corrections.json")
            runtime._voice_link_rules_path.write_text(
                json.dumps({"hosts": {"example.com": "link example site"}, "rules": []}),
                encoding="utf-8",
            )
            runtime._refresh_voice_link_rules_if_needed()

            self.assertEqual(runtime._describe_link("example.com", "/path"), "link example site")

            previous_mtime = runtime._voice_link_rules_path.stat().st_mtime_ns
            time.sleep(0.001)
            runtime._voice_link_rules_path.write_text(
                json.dumps({"hosts": {"example.com": "link changed label"}, "rules": []}),
                encoding="utf-8",
            )
            os.utime(runtime._voice_link_rules_path, ns=(previous_mtime + 1, previous_mtime + 1))
            runtime._refresh_voice_link_rules_if_needed()

            self.assertEqual(runtime._describe_link("example.com", "/path"), "link changed label")

    def test_link_host_label_edits_are_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            host, label, existed = runtime.set_voice_link_host_label("Example.com", "link example site")
            payload = json.loads(runtime._voice_link_rules_path.read_text())

        self.assertEqual(host, "example.com")
        self.assertEqual(label, "link example site")
        self.assertFalse(existed)
        self.assertEqual(payload["hosts"]["example.com"], "link example site")
        self.assertEqual(payload["hosts"]["tenor.com"], "gif")
        self.assertEqual(payload["rules"][0]["host"], "store.steampowered.com")
        self.assertEqual(payload["rules"][0]["template"], "link steam store {title_norm}")

    def test_link_rule_edits_are_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            index, rule = runtime.add_voice_link_rule(
                "example.com",
                r"^/games/(?P<title>[^/?#]+)",
                "link example games {title_norm}",
            )
            payload = json.loads(runtime._voice_link_rules_path.read_text())

        self.assertEqual(index, 2)
        self.assertEqual(rule.host, "example.com")
        self.assertEqual(rule.path_regex, r"^/games/(?P<title>[^/?#]+)")
        self.assertEqual(rule.template, "link example games {title_norm}")
        self.assertEqual(
            payload["rules"][1],
            {
                "host": "example.com",
                "mode": "regex",
                "path_regex": r"^/games/(?P<title>[^/?#]+)",
                "template": "link example games {title_norm}",
            },
        )

    def test_simple_link_rule_edits_are_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            index, rule = runtime.add_voice_link_rule(
                "",
                "",
                "link {host} {title_norm}",
                mode="simple",
                example_url="https://example.com/games/Transport_Fever_2",
            )
            payload = json.loads(runtime._voice_link_rules_path.read_text())

        self.assertEqual(index, 2)
        self.assertEqual(rule.host, "example.com")
        self.assertEqual(rule.path_shape, "/games/{title}")
        self.assertEqual(rule.template, "link {host} {title_norm}")
        self.assertEqual(
            payload["rules"][1],
            {
                "example_url": "https://example.com/games/Transport_Fever_2",
                "host": "example.com",
                "mode": "simple",
                "path_shape": "/games/{title}",
                "template": "link {host} {title_norm}",
            },
        )

    def test_simple_link_rule_speaks_derived_example_url(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionLinkRuntime(Path(tmp) / "voice_corrections.json")
            runtime.add_voice_link_rule(
                "",
                "",
                "link {host} {title_norm}",
                mode="simple",
                example_url="https://example.com/games/Transport_Fever_2",
            )

            spoken = runtime._describe_link("example.com", "/games/Transport_Fever_2")

        self.assertEqual(spoken, "link example Transport Fever 2")

    def test_simple_link_rule_payload_loads(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionLinkRuntime(Path(tmp) / "voice_corrections.json")
            runtime._voice_link_rules_path.write_text(
                json.dumps(
                    {
                        "hosts": {},
                        "rules": [
                            {
                                "host": "example.com",
                                "mode": "simple",
                                "path_shape": "/games/{title}",
                                "template": "link {host} {title_norm}",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            runtime._refresh_voice_link_rules_if_needed()

            spoken = runtime._describe_link("example.com", "/games/Transport_Fever_2")

        self.assertEqual(spoken, "link example Transport Fever 2")

    def test_link_rule_template_norm_alias_matches_words_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionLinkRuntime(Path(tmp) / "voice_corrections.json")
            runtime._ensure_voice_link_rules_file()

            runtime.add_voice_link_rule(
                "example.com",
                r"^/games/(?P<title>[^/?#]+)",
                "link example games {title_norm}",
            )
            spoken_norm = runtime._describe_link("example.com", "/games/Transport_Fever_2")

            runtime.add_voice_link_rule(
                "example.net",
                r"^/games/(?P<title>[^/?#]+)",
                "link example games {title_words}",
            )
            spoken_words = runtime._describe_link("example.net", "/games/Transport_Fever_2")

        self.assertEqual(spoken_norm, "link example games Transport Fever 2")
        self.assertEqual(spoken_words, "link example games Transport Fever 2")

    def test_link_rule_template_validation_rejects_unknown_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            with self.assertRaisesRegex(ValueError, "template field"):
                runtime.add_voice_link_rule(
                    "example.com",
                    r"^/games/(?P<title>[^/?#]+)",
                    "link example games {unknown}",
                )

    def test_voice_target_edits_are_persisted_to_bot_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._bot_configuration_path.write_text(json.dumps({"keep": "me"}), encoding="utf-8")

            target = runtime.set_voice_target_config(
                123,
                voice_channel=456,
                primary_tts_channel=789,
                secondary_tts_channel=790,
                relay_tts_enabled=True,
            )
            payload = json.loads(runtime._bot_configuration_path.read_text(encoding="utf-8"))

        self.assertEqual(target.guild_id, 123)
        self.assertEqual(target.voice_channel, 456)
        self.assertEqual(target.primary_tts_channel, 789)
        self.assertEqual(target.secondary_tts_channel, 790)
        self.assertTrue(target.primary_tts_listen_enabled)
        self.assertTrue(target.secondary_tts_listen_enabled)
        self.assertTrue(target.relay_tts_enabled)
        self.assertEqual(payload["keep"], "me")
        self.assertEqual(
            payload["voice_targets"],
            {
                "123": {
                    "voice_channel": 456,
                    "primary_tts_channel": 789,
                    "primary_tts_listen_enabled": True,
                    "secondary_tts_channel": 790,
                    "secondary_tts_listen_enabled": True,
                    "relay_tts_enabled": True,
                }
            },
        )

    def test_voice_target_channel_edits_preserve_relay_tts_setting(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            runtime.set_voice_target_config(
                123,
                voice_channel=456,
                primary_tts_channel=789,
                secondary_tts_channel=790,
                relay_tts_enabled=True,
            )
            updated = runtime.set_voice_target_config(
                123,
                voice_channel=654,
                primary_tts_channel=987,
                secondary_tts_channel=988,
            )

        self.assertEqual(updated.voice_channel, 654)
        self.assertEqual(updated.primary_tts_channel, 987)
        self.assertEqual(updated.secondary_tts_channel, 988)
        self.assertTrue(updated.primary_tts_listen_enabled)
        self.assertTrue(updated.secondary_tts_listen_enabled)
        self.assertTrue(updated.relay_tts_enabled)

    def test_voice_target_listen_toggles_are_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime.set_voice_target_config(
                123,
                voice_channel=456,
                primary_tts_channel=789,
                secondary_tts_channel=790,
            )

            updated = runtime.set_voice_target_tts_listen_enabled(
                123,
                config.VoiceTargetTtsChannelRole.PRIMARY,
                False,
            )
            updated = runtime.set_voice_target_tts_listen_enabled(
                123,
                config.VoiceTargetTtsChannelRole.SECONDARY,
                False,
            )
            payload = json.loads(runtime._bot_configuration_path.read_text(encoding="utf-8"))

        self.assertFalse(updated.primary_tts_listen_enabled)
        self.assertFalse(updated.secondary_tts_listen_enabled)
        self.assertEqual(
            payload["voice_targets"]["123"],
            {
                "voice_channel": 456,
                "primary_tts_channel": 789,
                "primary_tts_listen_enabled": False,
                "secondary_tts_channel": 790,
                "secondary_tts_listen_enabled": False,
                "relay_tts_enabled": False,
            },
        )

    def test_voice_target_config_can_be_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime.set_voice_target_config(
                123,
                voice_channel=456,
                primary_tts_channel=789,
                secondary_tts_channel=790,
                relay_tts_enabled=True,
            )

            removed = runtime.remove_voice_target_config(123)
            payload = json.loads(runtime._bot_configuration_path.read_text(encoding="utf-8"))

        self.assertTrue(removed)
        self.assertIsNone(runtime.voice_target(123))
        self.assertEqual(payload["voice_targets"], {})

    def test_voice_targets_are_loaded_from_legacy_bot_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._bot_configuration_path.write_text(
                json.dumps(
                    {
                        "voice_targets": {
                            "123": {
                                "voice_channel": 456,
                                "tts_channel": 789,
                                "relay_tts_enabled": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = runtime._load_voice_targets()

        self.assertEqual(
            loaded,
            {
                hikari.Snowflake(123): config.VoiceTargetConfig(
                    guild_id=hikari.Snowflake(123),
                    voice_channel=hikari.Snowflake(456),
                    primary_tts_channel=hikari.Snowflake(789),
                    primary_tts_listen_enabled=True,
                    relay_tts_enabled=True,
                )
            },
        )

    def test_voice_target_config_rejects_duplicate_primary_and_secondary_tts_channels(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            with self.assertRaisesRegex(ValueError, "Secondary TTS channel must differ"):
                runtime.set_voice_target_config(
                    123,
                    voice_channel=456,
                    primary_tts_channel=789,
                    secondary_tts_channel=789,
                )

    def test_modmux_creds_are_loaded_from_env_with_secretstr(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            values = {
                "MODRINTH_API_KEY": "modrinth-token",
                "CURSEFORGE_API_KEY": "curseforge-token",
                "NEXUSMODS_API_KEY": "nexus-token",
                "WUBE_API_KEY": "wube-token",
                "MODIO_API_KEY": "modio-token",
                "MODIO_USER_ID": "modio-user",
                "STEAM_WEB_API_KEY": "steam-token",
            }
            original_env_opt = config.env_opt
            config.env_opt = lambda name: values.get(name)
            try:
                creds = runtime._modmux_creds_from_env()
            finally:
                config.env_opt = original_env_opt

        self.assertEqual([type(item) for item in creds], [
            ModrinthCreds,
            CurseforgeCreds,
            NexusCreds,
            WubeCreds,
            ModioCreds,
            SteamCreds,
        ])
        modrinth = cast(ModrinthCreds, creds[0])
        curseforge = cast(CurseforgeCreds, creds[1])
        nexus = cast(NexusCreds, creds[2])
        wube = cast(WubeCreds, creds[3])
        modio = cast(ModioCreds, creds[4])
        steam = cast(SteamCreds, creds[5])
        self.assertTrue(
            all(
                isinstance(item.api_key, SecretStr)
                for item in (modrinth, curseforge, nexus, wube, modio, steam)
            )
        )
        self.assertIsInstance(modio.user_id, SecretStr)

    def test_modmux_creds_skip_incomplete_modio_env(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            values = {
                "MODIO_API_KEY": "modio-token",
                "MODIO_USER_ID": "",
            }
            original_env_opt = config.env_opt
            config.env_opt = lambda name: values.get(name)
            try:
                creds = runtime._modmux_creds_from_env()
            finally:
                config.env_opt = original_env_opt

        self.assertEqual(creds, [])

    def test_fuzzy_autocorrect_uses_single_clear_candidate(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                typos=_substitution_rules(("definately", "definitely", False)),
                fuzzy_targets=("definitely", "tomorrow"),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("definitly tomorow").render(), "definitely tomorrow")

    def test_fuzzy_autocorrect_is_disabled_per_user(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                typos=_substitution_rules(("definately", "definitely", False)),
                fuzzy_targets=("definitely",),
            ),
            user_settings={123: UserVoiceSettings(autocorrect=False)},
        )

        self.assertEqual(runtime._normalise_for_speech("definitly", user_id=123).render(), "definitly")

    def test_mixed_case_and_digits_are_not_fuzzy_corrected(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                typos=_substitution_rules(("definately", "definitely", False)),
                fuzzy_targets=("definitely", "minecraft"),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("Definitly mc1").render(), "Definitly mc1")

    def test_protected_terms_are_not_fuzzy_corrected(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                protected=frozenset({"piper"}),
                fuzzy_targets=("paper",),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("piper").render(), "piper")

    def test_ambiguous_fuzzy_candidate_is_left_unchanged(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                fuzzy_targets=("care", "cart"),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("cars").render(), "cars")

    def test_global_correction_edits_are_persisted_to_sectioned_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice_corrections.json"
            runtime = _CorrectionCore(path)

            category, source, target, existed = runtime.set_global_text_substitution("slang", "brb", "be right back")
            protected, protected_existed = runtime.add_global_protected_text_token("factorio")

            payload = json.loads(path.read_text())

        self.assertEqual(category, "slang")
        self.assertEqual(source, "brb")
        self.assertEqual(target, _substitution_rule("brb", "be right back"))
        self.assertFalse(existed)
        self.assertEqual(protected, "factorio")
        self.assertFalse(protected_existed)
        self.assertEqual(payload["slang"], {"brb": "be right back"})
        self.assertEqual(payload["typos"], {})
        self.assertEqual(payload["pronunciations"], {})
        self.assertEqual(payload["mention_overrides"], {})
        self.assertEqual(payload["protected"], ["factorio"])

    def test_case_sensitive_global_correction_is_persisted_with_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice_corrections.json"
            runtime = _CorrectionCore(path)

            category, source, target, existed = runtime.set_global_text_substitution(
                "slang",
                "LOL",
                "laughing out loud",
                case_sensitive=True,
            )
            payload = json.loads(path.read_text())

        self.assertEqual(category, "slang")
        self.assertEqual(source, "LOL")
        self.assertEqual(target, _substitution_rule("LOL", "laughing out loud", case_sensitive=True))
        self.assertFalse(existed)
        self.assertEqual(
            payload["slang"]["LOL"],
            {
                "value": "laughing out loud",
                "case_sensitive": True,
            },
        )

    def test_global_mention_override_is_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice_corrections.json"
            runtime = _CorrectionCore(path)

            target_user_id, spoken_name, existed = runtime.set_global_mention_override(456, "bee")
            payload = json.loads(path.read_text())

        self.assertEqual(target_user_id, 456)
        self.assertEqual(spoken_name, "bee")
        self.assertFalse(existed)
        self.assertEqual(payload["mention_overrides"], {"456": "bee"})

    def test_global_pronunciation_edits_are_persisted_to_sectioned_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice_corrections.json"
            runtime = _CorrectionCore(path)

            voice, source, target, existed = runtime.set_global_pronunciation(
                "default-voice",
                "egg",
                "ehg",
                PronunciationFormat.TEXT,
            )
            payload = json.loads(path.read_text())

        self.assertEqual(voice, "default-voice")
        self.assertEqual(source, "egg")
        self.assertEqual(target, PronunciationOverride(format=PronunciationFormat.TEXT, value="ehg"))
        self.assertFalse(existed)
        self.assertEqual(
            payload["pronunciations"],
            {
                "default-voice": {
                    "egg": {
                        "format": "text",
                        "value": "ehg",
                    }
                }
            },
        )

    def test_user_pronunciation_edits_are_persisted_to_user_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._users_path = Path(tmp) / "voice_users.json"
            runtime._user_settings = {}

            source, target, existed = runtime.set_user_pronunciation(
                123,
                "default-voice",
                "egg",
                "ehg",
                PronunciationFormat.TEXT,
            )
            payload = json.loads(runtime._users_path.read_text())

        self.assertEqual(source, "egg")
        self.assertEqual(target, PronunciationOverride(format=PronunciationFormat.TEXT, value="ehg"))
        self.assertFalse(existed)
        self.assertEqual(
            payload["users"]["123"]["pronunciations"],
            {
                "default-voice": {
                    "egg": {
                        "format": "text",
                        "value": "ehg",
                    }
                }
            },
        )

    def test_user_substitution_and_mention_override_are_persisted_to_user_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._users_path = Path(tmp) / "voice_users.json"
            runtime._user_settings = {}

            source, target, existed = runtime.set_user_text_substitution(
                123,
                "LOL",
                "laughing out loud",
                case_sensitive=True,
            )
            target_user_id, spoken_name, mention_existed = runtime.set_user_mention_override(123, 456, "bee")
            payload = json.loads(runtime._users_path.read_text())

        self.assertEqual(source, "LOL")
        self.assertEqual(target, _substitution_rule("LOL", "laughing out loud", case_sensitive=True))
        self.assertFalse(existed)
        self.assertEqual(target_user_id, 456)
        self.assertEqual(spoken_name, "bee")
        self.assertFalse(mention_existed)
        self.assertEqual(
            payload["users"]["123"]["substitutions"]["LOL"],
            {
                "value": "laughing out loud",
                "case_sensitive": True,
            },
        )
        self.assertEqual(payload["users"]["123"]["mention_overrides"], {"456": "bee"})

    def test_user_url_substitution_is_persisted_to_user_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._users_path = Path(tmp) / "voice_users.json"
            runtime._user_settings = {}

            source, target, existed = runtime.set_user_text_substitution(
                123,
                "https://tenor.com/view/funny-cat-reaction-gif-123456",
                "funny cat gif",
            )
            payload = json.loads(runtime._users_path.read_text())

        self.assertEqual(source, "https://tenor.com/view/funny-cat-reaction-gif-123456")
        self.assertEqual(target, _substitution_rule("https://tenor.com/view/funny-cat-reaction-gif-123456", "funny cat gif"))
        self.assertFalse(existed)
        self.assertEqual(
            payload["users"]["123"]["substitutions"]["https://tenor.com/view/funny-cat-reaction-gif-123456"],
            "funny cat gif",
        )

    def test_available_voices_are_limited_to_supported_max(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._engine = "python"
            runtime._engine_kind = "piper"
            runtime.voice = f"voice-{MAX_TTS_VOICES + 4:02d}"
            runtime._available_voices = []
            runtime._available_variants = []
            runtime._piper_config_cache = {}
            runtime._piper_python_voice_cache = {}
            runtime._piper_available_voices = lambda: [  # type: ignore[method-assign]
                f"voice-{index:02d}" for index in range(MAX_TTS_VOICES + 5)
            ]

            voices = asyncio.run(runtime.available_voices(force_refresh=True))

        self.assertEqual(len(voices), MAX_TTS_VOICES)
        self.assertIn(f"voice-{MAX_TTS_VOICES + 4:02d}", voices)
        self.assertNotIn(f"voice-{MAX_TTS_VOICES - 1:02d}", voices)

    def test_add_piper_model_rejects_when_voice_limit_is_reached(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._engine_kind = "piper"
            runtime._available_voices = []
            runtime._available_variants = []
            runtime._piper_config_cache = {}
            runtime._piper_python_voice_cache = {}
            runtime._piper_custom_write_dir = lambda: Path(tmp)  # type: ignore[method-assign]
            runtime._piper_available_voices = lambda: [  # type: ignore[method-assign]
                f"voice-{index:02d}" for index in range(MAX_TTS_VOICES)
            ]

            with self.assertRaisesRegex(ValueError, "at most 25 total voices are supported"):
                asyncio.run(
                    runtime.add_piper_model_from_hf(
                        HFRepoRef(repo_id="example/repo", revision="main", onnx_file=None),
                        "voice-new.onnx",
                    )
                )

    def test_direct_hf_model_url_is_validated_without_scanning_repo(self) -> None:
        runtime = _HFScanRuntime(is_candidate=True)

        with patch("cmd_voice_core.asyncio.to_thread", new=_immediate_to_thread):
            repo_ref, candidates = asyncio.run(
                runtime.scan_piper_models_from_hf(
                    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fi/fi_FI/harri/low/fi_FI-harri-low.onnx"
                )
            )

        self.assertEqual(repo_ref.repo_id, "rhasspy/piper-voices")
        self.assertEqual(repo_ref.revision, "v1.0.0")
        self.assertEqual(
            candidates,
            ["fi/fi_FI/harri/low/fi_FI-harri-low.onnx"],
        )
        self.assertFalse(runtime.repo_files_called)

    def test_direct_hf_model_url_rejects_invalid_piper_config(self) -> None:
        runtime = _HFScanRuntime(is_candidate=False)

        with patch("cmd_voice_core.asyncio.to_thread", new=_immediate_to_thread):
            with self.assertRaisesRegex(LookupError, "not Piper-compatible"):
                asyncio.run(
                    runtime.scan_piper_models_from_hf(
                        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fi/fi_FI/harri/low/fi_FI-harri-low.onnx"
                    )
                )

        self.assertFalse(runtime.repo_files_called)

    def test_hf_candidate_falls_back_to_official_voices_index(self) -> None:
        def fake_load_json_file(repo_id: str, revision: str, path: str) -> dict[str, object] | None:
            if path == "fi/fi_FI/harri/low/fi_FI-harri-low.onnx.json":
                return None
            if path == "voices.json":
                return {
                    "fi_FI-harri-low": {
                        "files": {
                            "fi/fi_FI/harri/low/fi_FI-harri-low.onnx": {},
                            "fi/fi_FI/harri/low/fi_FI-harri-low.onnx.json": {},
                        }
                    }
                }
            raise AssertionError(f"Unexpected path: {path}")

        with patch.object(VoiceTTSModelMixin, "_hf_load_json_file", side_effect=fake_load_json_file):
            self.assertTrue(
                VoiceTTSModelMixin._hf_is_piper_file_candidate(
                    "rhasspy/piper-voices",
                    "v1.0.0",
                    "fi/fi_FI/harri/low/fi_FI-harri-low.onnx",
                )
            )

    def test_hf_candidate_does_not_use_index_fallback_for_other_repos(self) -> None:
        def fake_load_json_file(repo_id: str, revision: str, path: str) -> dict[str, object] | None:
            if path == "fi/fi_FI/harri/low/fi_FI-harri-low.onnx.json":
                return None
            if path == "voices.json":
                raise AssertionError("Non-official repos should not query voices.json fallback.")
            raise AssertionError(f"Unexpected path: {path}")

        with patch.object(VoiceTTSModelMixin, "_hf_load_json_file", side_effect=fake_load_json_file):
            self.assertFalse(
                VoiceTTSModelMixin._hf_is_piper_file_candidate(
                    "someone-else/piper-voices",
                    "main",
                    "fi/fi_FI/harri/low/fi_FI-harri-low.onnx",
                )
            )


if __name__ == "__main__":
    unittest.main()
