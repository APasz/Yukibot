from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cmd_voice_common import TextCorrectionCatalog, UserVoiceSettings
from cmd_voice_core import VoiceTTSCoreMixin
from cmd_voice_model import VoiceTTSModelMixin
from cmd_voice import _normalise_voice_source
from cmd_voice_runtime import VoiceTTSRuntimeMixin


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

class _CorrectionCore(VoiceTTSCoreMixin, VoiceTTSModelMixin):
    _MAX_SUBSTITUTIONS_PER_USER = 100
    _MAX_SUBSTITUTION_KEY_CHARS = 40
    _MAX_SUBSTITUTION_VALUE_CHARS = 120

    def __init__(self, path: Path) -> None:
        self._corrections_path = path
        self._text_corrections = TextCorrectionCatalog()


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
