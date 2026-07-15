from __future__ import annotations

# pyright: reportUninitializedInstanceVariable=false
import contextlib
import json
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from urllib.parse import quote, unquote, urlparse

import hikari
import requests

import config
from cmd_voice_common import HUGGINGFACE_HOSTS, HFRepoRef, PiperPythonVoiceRuntime, PronunciationFormat, log

tts_log = logging.getLogger(config.LOGGER_TTS)


class VoiceTTSModelMixin:
    if TYPE_CHECKING:
        _engine_kind: str
        _engine: str | None
        _piper_python_loader: Callable[[str, str | None], PiperPythonVoiceRuntime] | None
        _piper_data_dir: str | None
        _piper_config_path: str | None
        _piper_config_cache: dict[str, tuple[int, dict[str, object] | None]]
        _piper_python_voice_cache: dict[str, PiperPythonVoiceRuntime]
        voice: str
        variant: str | None
        _VARIANT_CLEAR_VALUES: ClassVar[frozenset[str]]
        _MAX_SUBSTITUTION_KEY_CHARS: ClassVar[int]
        _MAX_SUBSTITUTION_VALUE_CHARS: ClassVar[int]
        _LOG_PREVIEW_CHARS: ClassVar[int]

    @staticmethod
    def _resolve_local_tts_engine() -> tuple[str, str | None]:
        preferred = (config.TTS_ENGINE or "auto").strip().lower()
        has_piper_python = VoiceTTSModelMixin._resolve_piper_python_loader() is not None
        if preferred in {"", "auto"}:
            if piper_path := shutil.which("piper"):
                return "piper", piper_path
            if has_piper_python:
                return "piper", "python"
            if espeak_path := VoiceTTSModelMixin._resolve_espeak_engine():
                return "espeak", espeak_path
            return "auto", None

        if preferred in {"espeak", "espeak-ng", "espeak_ng"}:
            return "espeak", VoiceTTSModelMixin._resolve_espeak_engine()

        if preferred == "piper":
            return "piper", shutil.which("piper") or ("python" if has_piper_python else None)

        log.warning(f"Unknown TTS_ENGINE={preferred!r}; falling back to auto")
        if piper_path := shutil.which("piper"):
            return "piper", piper_path
        if has_piper_python:
            return "piper", "python"
        if espeak_path := VoiceTTSModelMixin._resolve_espeak_engine():
            return "espeak", espeak_path
        return "auto", None

    @staticmethod
    def _resolve_piper_python_loader() -> Callable[[str, str | None], PiperPythonVoiceRuntime] | None:
        try:
            from piper.config import SynthesisConfig
            from piper.voice import PiperVoice
        except Exception:
            return None

        def load_voice(model_path: str, config_path: str | None) -> PiperPythonVoiceRuntime:
            loaded = cast(Any, PiperVoice).load(
                model_path,
                config_path=config_path,
                use_cuda=False,
            )
            return PiperPythonVoiceRuntime(loaded, synthesis_config_factory=cast(Callable[..., Any], SynthesisConfig))

        return load_voice

    @staticmethod
    def _resolve_espeak_engine() -> str | None:
        for executable in ("espeak-ng", "espeak"):
            if path := shutil.which(executable):
                return path
        return None

    def _engine_display(self) -> str:
        if not self._engine:
            return "none"
        if self._engine_kind == "piper":
            if self._engine == "python":
                return "Python Piper"
            if self._piper_python_loader:
                return "Python Piper"
            return "Piper"
        if self._engine_kind == "espeak":
            engine_name = Path(self._engine).name.lower()
            if engine_name == "espeak-ng":
                return "eSpeak NG"
            return "eSpeak"
        return self._engine_kind.title()

    def _initial_piper_voice(self) -> str:
        if model := config.TTS_PIPER_MODEL:
            return model

        configured_voice = (config.TTS_VOICE or "").strip()
        if configured_voice and self._piper_model_path(configured_voice):
            return configured_voice

        if discovered := self._piper_discover_models():
            return discovered[0].stem

        return configured_voice or "en-gb-x-rp"

    def _piper_available_voices(self) -> list[str]:
        voices: set[str] = set()
        for model in self._piper_discover_models():
            voices.add(model.stem)

        if model := config.TTS_PIPER_MODEL:
            voices.add(model)

        if self.voice and self._piper_model_path(self.voice):
            voices.add(self.voice)

        return sorted(voices)

    def _piper_discover_models(self) -> list[Path]:
        models: list[Path] = []
        seen: set[str] = set()
        for data_dir in self._piper_model_search_dirs():
            for model in sorted(data_dir.glob("*.onnx")):
                resolved = str(model.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                models.append(model)
        return models

    def _piper_custom_write_dir(self) -> Path:
        for path in self._piper_custom_model_dirs(include_missing=True):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            if path.exists() and path.is_dir():
                return path
        raise OSError("Unable to create a writable custom Piper model directory.")

    def _piper_custom_models(self) -> list[Path]:
        models: list[Path] = []
        seen: set[str] = set()
        for data_dir in self._piper_custom_model_dirs():
            if not data_dir.exists() or not data_dir.is_dir():
                continue
            for model in sorted(data_dir.glob("*.onnx")):
                resolved = str(model.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                models.append(model)
        return models

    def _piper_custom_model_path(self, model: str) -> Path | None:
        needle = model.strip().lower()
        if not needle:
            return None

        for path in self._piper_custom_models():
            if path.stem.lower() == needle or path.name.lower() == needle:
                return path
        return None

    def _piper_custom_model_dirs(self, include_missing: bool = False) -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()

        def add_dir(path: Path):
            if not include_missing and (not path.exists() or not path.is_dir()):
                return
            resolved = str(path.resolve())
            if resolved in seen:
                return
            seen.add(resolved)
            dirs.append(path)

        if self._piper_data_dir:
            configured = Path(self._piper_data_dir).expanduser()
            add_dir(configured / "custom")

        bot_dir = Path(__file__).resolve().parent
        add_dir(bot_dir / "voices" / "piper" / "custom")

        cwd = Path.cwd()
        add_dir(cwd / "voices" / "piper" / "custom")

        return dirs

    def _piper_model_search_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()

        def add_dir(path: Path):
            if not path.exists() or not path.is_dir():
                return
            resolved = str(path.resolve())
            if resolved in seen:
                return
            seen.add(resolved)
            dirs.append(path)

        if self._piper_data_dir:
            configured = Path(self._piper_data_dir).expanduser()
            add_dir(configured)
            add_dir(configured / "custom")

        bot_dir = Path(__file__).resolve().parent
        bot_voices_dir = bot_dir / "voices" / "piper"
        add_dir(bot_voices_dir / "custom")
        add_dir(bot_voices_dir)

        bot_voice_dir = bot_dir / "voice" / "piper"
        add_dir(bot_voice_dir)

        add_dir(bot_dir)

        cwd = Path.cwd()
        cwd_voices_dir = cwd / "voices" / "piper"
        add_dir(cwd_voices_dir / "custom")
        add_dir(cwd_voices_dir)

        cwd_voice_dir = cwd / "voice" / "piper"
        add_dir(cwd_voice_dir)

        add_dir(cwd)

        return dirs

    def _piper_available_variants(self, voice: str) -> list[str]:
        raw = self._piper_load_config(voice)
        if not raw:
            return []

        speaker_map = raw.get("speaker_id_map")
        if isinstance(speaker_map, dict) and speaker_map:
            variants = {str(name).strip() for name in speaker_map if str(name).strip()}
            for sid in speaker_map.values():
                try:
                    variants.add(str(int(sid)))
                except (TypeError, ValueError):
                    continue
            variants = sorted(variants)
            if variants:
                return variants

        num_speakers = raw.get("num_speakers")
        if isinstance(num_speakers, int) and num_speakers > 1:
            return [str(i) for i in range(num_speakers)]

        return []

    @staticmethod
    def _variant_gender_hint(value: str) -> str | None:
        tokens = {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}
        if not tokens:
            return None

        if tokens & {"female", "woman", "girl", "fem", "f"}:
            return "female"
        if tokens & {"male", "man", "boy", "masc", "m"}:
            return "male"
        if tokens & {"neutral", "nonbinary", "non-binary", "nb", "androgynous"}:
            return "neutral"
        return None

    def _piper_variant_details(self, voice: str) -> dict[str, str]:
        raw = self._piper_load_config(voice)
        if not raw:
            return {}

        speaker_map = raw.get("speaker_id_map")
        if not isinstance(speaker_map, dict) or not speaker_map:
            return {}

        name_to_id: dict[str, int] = {}
        id_to_name: dict[int, str] = {}
        id_to_gender: dict[int, str] = {}

        for raw_name, raw_id in speaker_map.items():
            name = str(raw_name).strip()
            if not name:
                continue

            try:
                speaker_id = int(raw_id)
            except (TypeError, ValueError):
                continue

            name_to_id[name] = speaker_id
            id_to_name.setdefault(speaker_id, name)

            gender = self._variant_gender_hint(name)
            if gender and speaker_id not in id_to_gender:
                id_to_gender[speaker_id] = gender

        details: dict[str, str] = {}
        for name, speaker_id in name_to_id.items():
            parts = [f"id {speaker_id}"]
            gender = self._variant_gender_hint(name)
            if gender:
                parts.append(gender)
            details[name] = "; ".join(parts)

        for speaker_id, name in id_to_name.items():
            parts = [f"name {name}"]
            gender = id_to_gender.get(speaker_id)
            if gender:
                parts.append(gender)
            details[str(speaker_id)] = "; ".join(parts)

        return details

    @staticmethod
    def _variant_choice_label(variant: str, detail: str | None) -> str:
        if not detail:
            return variant[:100]

        max_len = 100
        label = f"{variant} ({detail})"
        if len(label) <= max_len:
            return label

        available = max_len - len(variant) - 3
        if available <= 3:
            return variant[:max_len]

        clipped = detail[: available - 3].rstrip()
        return f"{variant} ({clipped}...)"

    def variant_autocomplete_choices(self, voice: str, variants: list[str], needle: str = ""):
        detail_map = self._piper_variant_details(voice) if self._engine_kind == "piper" else {}
        acb = hikari.impl.AutocompleteChoiceBuilder
        choices = []

        for variant in variants:
            detail = "disable variant" if variant == "none" else detail_map.get(variant)
            search_blob = f"{variant} {detail or ''}".lower()
            if needle and needle not in search_blob:
                continue
            label = self._variant_choice_label(variant, detail)
            choices.append(acb(label, variant))

        return choices[:25]

    def _piper_speaker_id(self, voice: str, variant: str | None) -> int | None:
        if not variant:
            return None

        value = variant.strip()
        if value.isdigit():
            return int(value)

        raw = self._piper_load_config(voice)
        if not raw:
            log.warning(f"TTS Piper speaker map unavailable for voice={voice!r}; using default speaker")
            return None

        speaker_map = raw.get("speaker_id_map")
        if not isinstance(speaker_map, dict):
            log.warning(f"TTS Piper voice has no named speakers for voice={voice!r}; using default speaker")
            return None

        match = next((sid for name, sid in speaker_map.items() if str(name).lower() == value.lower()), None)
        if match is None:
            log.warning(f"TTS Piper unknown speaker={variant!r} for voice={voice!r}; using default speaker")
            return None

        try:
            return int(match)
        except (TypeError, ValueError):
            log.warning(f"TTS Piper invalid speaker id for speaker={variant!r} voice={voice!r}; using default speaker")
            return None

    def _piper_load_config(self, voice: str) -> dict[str, object] | None:
        config_path = self._piper_config_file(voice)
        if not config_path:
            return None

        try:
            mtime_ns = config_path.stat().st_mtime_ns
            cache_key = str(config_path.resolve())
        except OSError as xcp:
            log.warning(f"TTS Piper config stat failed path={config_path!s}: {type(xcp).__name__}: {xcp}")
            return None

        cached = self._piper_config_cache.get(cache_key)
        if cached and cached[0] == mtime_ns:
            return cached[1]

        try:
            raw = json.loads(config_path.read_text(config.STR_ENCODE))
        except (OSError, ValueError) as xcp:
            log.warning(f"TTS Piper config read failed path={config_path!s}: {type(xcp).__name__}: {xcp}")
            self._piper_config_cache[cache_key] = (mtime_ns, None)
            return None

        if not isinstance(raw, dict):
            log.warning(f"TTS Piper config invalid path={config_path!s}: expected JSON object")
            self._piper_config_cache[cache_key] = (mtime_ns, None)
            return None

        self._piper_config_cache[cache_key] = (mtime_ns, raw)
        return raw

    def _piper_config_file(self, voice: str) -> Path | None:
        if self._piper_config_path:
            configured = Path(self._piper_config_path).expanduser()
            if configured.exists():
                return configured
            log.warning(f"TTS Piper config missing: {configured!s}")
            return None

        model_path = self._piper_model_path(voice)
        if not model_path:
            return None

        inferred = Path(f"{model_path}.json")
        if inferred.exists():
            return inferred

        return None

    def _piper_model_path(self, voice: str) -> Path | None:
        value = voice.strip()
        if not value:
            return None

        direct = Path(value).expanduser()
        if direct.exists() and direct.is_file():
            return direct
        if direct.suffix != ".onnx":
            with_suffix = direct.with_suffix(".onnx")
            if with_suffix.exists() and with_suffix.is_file():
                return with_suffix

        filename = direct.name if direct.suffix == ".onnx" else f"{direct.name}.onnx"
        for data_dir in self._piper_model_search_dirs():
            candidate = data_dir / filename
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _piper_python_voice(self, voice: str) -> PiperPythonVoiceRuntime | None:
        if not self._piper_python_loader:
            return None

        model_path = self._piper_model_path(voice)
        if not model_path:
            return None

        config_path = self._piper_config_file(voice)
        resolved_model = str(model_path.resolve())
        resolved_config = str(config_path.resolve()) if config_path else ""
        cache_key = f"{resolved_model}::{resolved_config}"
        cached = self._piper_python_voice_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            loaded = self._piper_python_loader(
                resolved_model,
                str(config_path) if config_path else None,
            )
        except Exception as xcp:
            log.warning(f"TTS Piper python voice load failed path={model_path!s}: {type(xcp).__name__}: {xcp}")
            return None

        self._piper_python_voice_cache[cache_key] = loaded
        return loaded

    @staticmethod
    def _hf_parse_repo_url(url: str) -> HFRepoRef:
        value = url.strip()
        if not value:
            raise ValueError("url must not be empty")

        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only HTTP/HTTPS Hugging Face URLs are supported.")
        if parsed.netloc.lower() not in HUGGINGFACE_HOSTS:
            raise ValueError("Only huggingface.co model links are supported.")

        parts = [segment for segment in parsed.path.split("/") if segment]
        if len(parts) < 2:
            raise ValueError("Expected a URL like https://huggingface.co/<owner>/<repo>")

        repo_id = f"{parts[0]}/{parts[1]}"
        revision = "main"
        onnx_file: str | None = None

        if len(parts) >= 3 and parts[2] in {"blob", "resolve", "tree"}:
            if len(parts) < 4:
                raise ValueError("Invalid Hugging Face URL.")
            revision = unquote(parts[3])
            if parts[2] in {"blob", "resolve"}:
                if len(parts) < 5:
                    raise ValueError("Model file URL must include a file path.")
                onnx_file = unquote("/".join(parts[4:]))

        if onnx_file and not onnx_file.lower().endswith(".onnx"):
            raise ValueError("Model file URL must point to a `.onnx` file.")

        return HFRepoRef(repo_id=repo_id, revision=revision, onnx_file=onnx_file)

    @staticmethod
    def _hf_repo_files(repo_id: str, revision: str) -> list[str]:
        api_url = f"https://huggingface.co/api/models/{repo_id}"
        try:
            with requests.get(api_url, params={"revision": revision}, timeout=30) as response:
                if response.status_code == 404:
                    raise LookupError(f"Hugging Face repository `{repo_id}` not found.")

                try:
                    response.raise_for_status()
                except requests.HTTPError as xcp:
                    raise RuntimeError(f"Hugging Face API error: {response.status_code}") from xcp

                try:
                    payload = response.json()
                except ValueError as xcp:
                    raise RuntimeError("Hugging Face API returned invalid JSON.") from xcp
        except requests.RequestException as xcp:
            raise RuntimeError(f"Failed to query Hugging Face API: {xcp}") from xcp

        siblings = payload.get("siblings")
        if not isinstance(siblings, list):
            return []

        files: list[str] = []
        for entry in siblings:
            if not isinstance(entry, dict):
                continue
            filename = entry.get("rfilename")
            if isinstance(filename, str) and filename.strip():
                files.append(filename.strip())
        return files

    @classmethod
    def _hf_find_piper_candidates(cls, repo_id: str, revision: str, files: list[str]) -> list[str]:
        file_map: dict[str, str] = {}
        for value in files:
            clean = value.strip()
            if not clean:
                continue
            file_map.setdefault(clean.lower(), clean)

        onnx_files = sorted({path for key, path in file_map.items() if key.endswith(".onnx")}, key=str.lower)
        candidates: list[str] = []

        for onnx_file in onnx_files:
            config_file = file_map.get(f"{onnx_file}.json".lower())
            if not config_file:
                continue

            raw = cls._hf_load_json_file(repo_id, revision, config_file)
            if not raw:
                continue

            if cls._hf_is_piper_model_config(raw):
                candidates.append(onnx_file)

        tts_log.info(
            f"TTS HF candidate scan repo={repo_id!r} revision={revision!r} "
            f"files={len(files)} onnx_files={len(onnx_files)} candidates={len(candidates)}"
        )
        return candidates

    @classmethod
    def _hf_is_piper_file_candidate(cls, repo_id: str, revision: str, onnx_file: str) -> bool:
        clean = onnx_file.strip()
        if not clean or not clean.lower().endswith(".onnx"):
            tts_log.info(
                f"TTS HF direct candidate rejected repo={repo_id!r} revision={revision!r} "
                f"file={onnx_file!r} reason=invalid_extension"
            )
            return False

        raw = cls._hf_load_json_file(repo_id, revision, f"{clean}.json")
        if raw and cls._hf_is_piper_model_config(raw):
            tts_log.info(
                f"TTS HF direct candidate accepted repo={repo_id!r} revision={revision!r} "
                f"file={clean!r} source=config_json"
            )
            return True

        if repo_id == "rhasspy/piper-voices":
            in_index = cls._hf_repo_index_lists_piper_file(repo_id, revision, clean)
            tts_log.info(
                f"TTS HF direct candidate fallback repo={repo_id!r} revision={revision!r} "
                f"file={clean!r} source=voices_json accepted={in_index}"
            )
            return in_index

        tts_log.info(
            f"TTS HF direct candidate rejected repo={repo_id!r} revision={revision!r} "
            f"file={clean!r} reason=missing_or_invalid_config"
        )
        return False

    @classmethod
    def _hf_repo_index_lists_piper_file(cls, repo_id: str, revision: str, onnx_file: str) -> bool:
        raw = cls._hf_load_json_file(repo_id, revision, "voices.json")
        if not isinstance(raw, dict):
            tts_log.info(f"TTS HF voices index unavailable repo={repo_id!r} revision={revision!r} file={onnx_file!r}")
            return False

        config_file = f"{onnx_file}.json"
        matched = False
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            files = entry.get("files")
            if not isinstance(files, dict):
                continue
            if onnx_file in files and config_file in files:
                matched = True
                break

        tts_log.info(
            f"TTS HF voices index lookup repo={repo_id!r} revision={revision!r} file={onnx_file!r} matched={matched}"
        )
        return matched

    @classmethod
    def _hf_load_json_file(cls, repo_id: str, revision: str, path: str) -> dict[str, object] | None:
        url = cls._hf_resolve_download_url(repo_id, revision, path)
        try:
            with requests.get(url, timeout=30) as response:
                if response.status_code == 404:
                    return None

                try:
                    response.raise_for_status()
                except requests.HTTPError as xcp:
                    raise RuntimeError(
                        f"Hugging Face file request failed for `{path}`: {response.status_code}"
                    ) from xcp

                try:
                    payload = response.json()
                except ValueError:
                    return None
        except requests.RequestException as xcp:
            raise RuntimeError(f"Failed to read `{path}` from Hugging Face: {xcp}") from xcp
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _hf_is_piper_model_config(raw: dict[str, object]) -> bool:
        audio = raw.get("audio")
        inference = raw.get("inference")
        phoneme_type = raw.get("phoneme_type")
        phoneme_id_map = raw.get("phoneme_id_map")
        sample_rate = audio.get("sample_rate") if isinstance(audio, dict) else None

        return bool(
            isinstance(audio, dict)
            and isinstance(sample_rate, (int, float))
            and isinstance(inference, dict)
            and isinstance(phoneme_type, str)
            and phoneme_type.strip()
            and isinstance(phoneme_id_map, dict)
            and phoneme_id_map
        )

    @staticmethod
    def _hf_resolve_download_url(repo_id: str, revision: str, path: str) -> str:
        quoted_revision = quote(revision, safe="/")
        quoted_path = quote(path, safe="/")
        return f"https://huggingface.co/{repo_id}/resolve/{quoted_revision}/{quoted_path}"

    @staticmethod
    def _download_file(url: str, target: Path, optional: bool) -> bool:
        partial = Path(f"{target}.part")
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                if response.status_code == 404 and optional:
                    return False
                if response.status_code == 404:
                    raise LookupError(f"File not found: {url}")
                response.raise_for_status()
                with partial.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            stream.write(chunk)
            partial.replace(target)
            return True
        except Exception:
            with contextlib.suppress(OSError):
                partial.unlink()
            raise

    @staticmethod
    def _voice_spec(voice: str, variant: str | None) -> str:
        if not variant:
            return voice
        return f"{voice}+{variant}"

    @classmethod
    def _normalise_variant(cls, variant: str | None, allow_empty: bool = True) -> str | None:
        if variant is None:
            return None

        value = variant.strip()
        if value.startswith("+"):
            value = value[1:].strip()

        if not value:
            if allow_empty:
                return None
            raise ValueError("variant must not be empty")

        if value.lower() in cls._VARIANT_CLEAR_VALUES:
            return None

        return value

    @classmethod
    def _normalise_substitution_key(cls, source: str, *, case_sensitive: bool = False) -> str:
        key = source.strip()
        if not key:
            raise ValueError("source must not be empty")
        if not case_sensitive:
            key = key.lower()
        if len(key) > cls._MAX_SUBSTITUTION_KEY_CHARS:
            raise ValueError(f"source is too long (max {cls._MAX_SUBSTITUTION_KEY_CHARS} chars)")
        if re.fullmatch(r"(?:https?://|www\.)\S+", key, re.IGNORECASE):
            return key
        pattern = r"[A-Za-z0-9][A-Za-z0-9'_-]*" if case_sensitive else r"[a-z0-9][a-z0-9'_-]*"
        if not re.fullmatch(pattern, key):
            raise ValueError(
                "source may only include letters, numbers, apostrophes, underscores, hyphens, or a full URL"
            )
        return key

    @classmethod
    def _normalise_substitution_value(cls, target: str) -> str:
        value = target.strip()
        if not value:
            raise ValueError("target must not be empty")
        if len(value) > cls._MAX_SUBSTITUTION_VALUE_CHARS:
            raise ValueError(f"target is too long (max {cls._MAX_SUBSTITUTION_VALUE_CHARS} chars)")
        return value

    @staticmethod
    def _normalise_pronunciation_format(value: PronunciationFormat | str) -> PronunciationFormat:
        if isinstance(value, PronunciationFormat):
            return value

        raw = value.strip().lower()
        try:
            return PronunciationFormat(raw)
        except ValueError as xcp:
            raise ValueError("pronunciation format must be `text` or `ipa`") from xcp

    def voice_supports_ipa_pronunciations(self, voice: str) -> bool:
        if self._engine_kind != "piper":
            return False

        raw = self._piper_load_config(voice)
        phoneme_type = raw.get("phoneme_type") if isinstance(raw, dict) else None
        return isinstance(phoneme_type, str) and phoneme_type.strip().lower() == "espeak"

    def _preview(self, text: str) -> str:
        if len(text) <= self._LOG_PREVIEW_CHARS:
            return text
        return text[: self._LOG_PREVIEW_CHARS].rstrip() + "..."

    @staticmethod
    def _playback_timeout_seconds(text: str) -> float:
        words = max(1, len(text.split()))
        # At ~165 wpm this leaves generous headroom for connect/encode jitter.
        return min(120.0, max(10.0, words * 0.7 + 8.0))


# AiviA APasz
