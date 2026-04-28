from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

import config
from cmd_voice_common import TextCorrectionCatalog, UserVoiceSettings, VoiceLinkRules
from cmd_voice_core import VoiceTTSCoreMixin
from cmd_voice_model import VoiceTTSModelMixin
from cmd_voice import _normalise_voice_source
from cmd_voice_runtime import VoiceTTSRuntimeMixin
from modmux.providers.curseforge import CurseforgeCreds
from modmux.providers.modio import ModioCreds
from modmux.providers.modrinth import ModrinthCreds
from modmux.providers.nexusmods import NexusCreds
from modmux.providers.wube import WubeCreds
from modmux import SteamCreds
from pydantic import SecretStr


class _CorrectionRuntime(VoiceTTSRuntimeMixin):
    _FUZZY_AUTOCORRECT_MIN_LEN = 4
    _MAX_SPOKEN_CHARS = 550

    def __init__(
        self,
        *,
        catalog: TextCorrectionCatalog,
        user_settings: dict[int, UserVoiceSettings] | None = None,
    ) -> None:
        self._text_corrections = catalog
        self._user_settings = user_settings or {}
        self._voice_link_rules = VoiceLinkRules()

    def _refresh_voice_link_rules_if_needed(self) -> None:
        return None

    async def _mod_link_name(self, url: str) -> str | None:
        return None

class _CorrectionCore(VoiceTTSCoreMixin, VoiceTTSModelMixin):
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 40
    _MAX_SUBSTITUTION_VALUE_CHARS = 120

    def __init__(self, path: Path) -> None:
        self._corrections_path = path
        self._voice_link_rules_path = path.with_name("voice_link_rules.json")
        self._text_corrections = TextCorrectionCatalog()
        self._voice_link_rules = VoiceLinkRules()
        self._voice_link_rules_mtime_ns = None
        self._mod_link_name_cache = {}
        self._modmux = None


class _CorrectionLinkRuntime(VoiceTTSCoreMixin, VoiceTTSRuntimeMixin, VoiceTTSModelMixin):
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 40
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


class _CorrectionAsyncRuntime(_CorrectionRuntime):
    def __init__(self, *, catalog: TextCorrectionCatalog, mod_names: dict[str, str]) -> None:
        super().__init__(catalog=catalog)
        self._mod_names = mod_names

    async def _mod_link_name(self, url: str) -> str | None:
        return self._mod_names.get(url)


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

    def test_exact_slang_expands_before_fuzzy_autocorrect(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang={"idk": "I don't know"},
                typos={"definately": "definitely"},
                fuzzy_targets=("definitely",),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("idk definately").render(), "I don't know definitely")

    def test_pronunciation_override_precedes_substitution(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={
                123: UserVoiceSettings(
                    pronunciations={"egg": "ehg"},
                    substitutions={"egg": "egg replacement"},
                )
            },
        )

        self.assertEqual(runtime._normalise_for_speech("egg", user_id=123).render(), "ehg")

    def test_exact_global_correction_can_match_full_punctuation_token(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang={"w/": "with"},
            )
        )

        self.assertEqual(runtime._normalise_for_speech("w/").render(), "with")

    def test_user_substitution_applies_to_emoji_name(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={123: UserVoiceSettings(substitutions={"sob": "crying"})},
        )

        self.assertEqual(runtime._normalise_for_speech(":sob:", user_id=123).render(), "crying")

    def test_user_pronunciation_applies_to_emoji_name(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(),
            user_settings={123: UserVoiceSettings(pronunciations={"sob": "sobbing"})},
        )

        self.assertEqual(runtime._normalise_for_speech(":sob:", user_id=123).render(), "sobbing")

    def test_global_substitution_applies_to_emoji_name(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang={"thumbs_up": "nice"},
            )
        )

        self.assertEqual(runtime._normalise_for_speech(":thumbs_up:").render(), "nice")

    def test_emoji_substitution_preserves_repeat_count(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                slang={"skull": "dead"},
            )
        )

        self.assertEqual(runtime._normalise_for_speech(":skull: :skull:").render(), "dead x2")

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

        self.assertEqual(runtime._normalise_for_speech("https://example.com/path?q=1").render(), "link example.com")

    def test_gif_hosts_use_gif_label(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionLinkRuntime(Path(tmp) / "voice_corrections.json")
            runtime._ensure_voice_link_rules_file()
            runtime._refresh_voice_link_rules_if_needed()

            self.assertEqual(runtime._normalise_for_speech("https://tenor.com/view/test").render(), "gif")
            self.assertEqual(runtime._normalise_for_speech("https://www.giphy.com/gifs/test").render(), "gif")
            self.assertEqual(runtime._normalise_for_speech("https://klipy.com/clip/test").render(), "gif")

    def test_compact_minutes_are_expanded(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("5m").render(), "5 minutes")
        self.assertEqual(runtime._normalise_for_speech("1m, brb").render(), "1 minute, brb")

    def test_other_compact_units_are_expanded(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("5s 2h 3d 5km").render(), "5 seconds 2 hours 3 days 5 kilometers")
        self.assertEqual(runtime._normalise_for_speech("(1km)").render(), "(1 kilometer)")

    def test_slash_ratio_is_spoken_as_out_of(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("5/10").render(), "5 out of 10")
        self.assertEqual(runtime._normalise_for_speech("(7/10)").render(), "(7 out of 10)")

    def test_common_with_shorthand_is_expanded(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._normalise_for_speech("w/ fries, w/o sauce").render(), "with fries, without sauce")

    def test_attachment_only_message_uses_attachment_placeholder(self) -> None:
        runtime = _CorrectionRuntime(catalog=TextCorrectionCatalog())

        self.assertEqual(runtime._message_speech_input("", attachment_count=1), "attachment")
        self.assertEqual(runtime._message_speech_input("hello", attachment_count=1), "hello")
        self.assertEqual(runtime._message_speech_input("", attachment_count=0), "")

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

    def test_link_rule_edits_are_persisted(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            index, rule = runtime.add_voice_link_rule(
                "example.com",
                r"^/games/(?P<title>[^/?#]+)",
                "link example games {title_words}",
            )
            payload = json.loads(runtime._voice_link_rules_path.read_text())

        self.assertEqual(index, 2)
        self.assertEqual(rule.host, "example.com")
        self.assertEqual(rule.path_regex, r"^/games/(?P<title>[^/?#]+)")
        self.assertEqual(rule.template, "link example games {title_words}")
        self.assertEqual(payload["rules"][1], {
            "host": "example.com",
            "path_regex": r"^/games/(?P<title>[^/?#]+)",
            "template": "link example games {title_words}",
        })

    def test_link_rule_template_validation_rejects_unknown_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")

            with self.assertRaisesRegex(ValueError, "template field"):
                runtime.add_voice_link_rule(
                    "example.com",
                    r"^/games/(?P<title>[^/?#]+)",
                    "link example games {unknown}",
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
        self.assertTrue(all(isinstance(item.api_key, SecretStr) for item in creds if hasattr(item, "api_key")))
        self.assertIsInstance(creds[4].user_id, SecretStr)

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
                typos={"definately": "definitely"},
                fuzzy_targets=("definitely", "tomorrow"),
            )
        )

        self.assertEqual(runtime._normalise_for_speech("definitly tomorow").render(), "definitely tomorrow")

    def test_fuzzy_autocorrect_is_disabled_per_user(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                typos={"definately": "definitely"},
                fuzzy_targets=("definitely",),
            ),
            user_settings={123: UserVoiceSettings(autocorrect=False)},
        )

        self.assertEqual(runtime._normalise_for_speech("definitly", user_id=123).render(), "definitly")

    def test_mixed_case_and_digits_are_not_fuzzy_corrected(self) -> None:
        runtime = _CorrectionRuntime(
            catalog=TextCorrectionCatalog(
                typos={"definately": "definitely"},
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
        self.assertEqual(target, "be right back")
        self.assertFalse(existed)
        self.assertEqual(protected, "factorio")
        self.assertFalse(protected_existed)
        self.assertEqual(payload["slang"], {"brb": "be right back"})
        self.assertEqual(payload["typos"], {})
        self.assertEqual(payload["protected"], ["factorio"])

    def test_user_pronunciation_edits_are_persisted_to_user_settings(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = _CorrectionCore(Path(tmp) / "voice_corrections.json")
            runtime._users_path = Path(tmp) / "voice_users.json"
            runtime._user_settings = {}

            source, target, existed = runtime.set_user_pronunciation(123, "egg", "ehg")
            payload = json.loads(runtime._users_path.read_text())

        self.assertEqual(source, "egg")
        self.assertEqual(target, "ehg")
        self.assertFalse(existed)
        self.assertEqual(payload["users"]["123"]["pronunciations"], {"egg": "ehg"})


if __name__ == "__main__":
    unittest.main()
