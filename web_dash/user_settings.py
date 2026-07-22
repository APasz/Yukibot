from __future__ import annotations

import re
import threading
from _thread import RLock
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from re import Pattern

from pydantic import BaseModel, ConfigDict, field_validator

import config

_SETTINGS_SCHEMA_VERSION = 1
_HEX_COLOR_PATTERN: Pattern[str] = re.compile(r"#[0-9a-fA-F]{6}$")


class ModWebColorScheme(StrEnum):
    CURRENT = "current"
    DARK = "dark"
    LIGHT = "light"


class ModWebAppearanceSettings(BaseModel):
    """Stored appearance choices for the mod-web UI."""

    color_scheme: ModWebColorScheme = ModWebColorScheme.CURRENT
    primary_color_hex: str | None = None
    positive_color_hex: str | None = None
    warning_color_hex: str | None = None
    negative_color_hex: str | None = None
    info_color_hex: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator(
        "primary_color_hex",
        "positive_color_hex",
        "warning_color_hex",
        "negative_color_hex",
        "info_color_hex",
    )
    @classmethod
    def _validate_color_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _HEX_COLOR_PATTERN.fullmatch(normalized):
            raise ValueError("Appearance colours must be six-digit #RRGGBB values.")
        return normalized.upper()


class ModWebChatSettings(BaseModel):
    """Stored web-chat choices. They are not applied until the chat UI supports them."""

    use_24_hour_time: bool = True

    model_config = ConfigDict(extra="forbid", frozen=True)


class ModWebUserSettings(BaseModel):
    """Versioned preferences owned by one Discord user across mod-web nodes."""

    appearance: ModWebAppearanceSettings = ModWebAppearanceSettings()
    web_chat: ModWebChatSettings = ModWebChatSettings()

    model_config = ConfigDict(extra="forbid", frozen=True)


class ModWebUserSettingsStore:
    """Persistent, authority-aware storage for per-user mod-web preferences."""

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path or config.USER_SETTINGS
        self._lock: RLock = threading.RLock()
        self._loaded = False
        self._settings_by_user_id: dict[int, ModWebUserSettings] = {}

    @staticmethod
    def _validate_user_id(user_id: int) -> int:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer.")
        return user_id

    def get(self, *, user_id: int) -> ModWebUserSettings:
        validated_user_id: int = self._validate_user_id(user_id)
        with self._lock:
            self._ensure_loaded()
            return self._settings_by_user_id.get(validated_user_id, ModWebUserSettings())

    def set(self, *, user_id: int, settings: ModWebUserSettings) -> bool:
        validated_user_id: int = self._validate_user_id(user_id)
        if not isinstance(settings, ModWebUserSettings):
            raise TypeError("settings must be a ModWebUserSettings instance.")
        with self._lock:
            self._ensure_loaded()
            if self._settings_by_user_id.get(validated_user_id) == settings:
                return False
            if config.DATA_AUTHORITY_MODE is config.DataAuthorityMode.REMOTE:
                saved_payload = config.mutate_remote_user_settings(
                    user_id=validated_user_id,
                    settings=settings.model_dump(mode="json"),
                )
                self._settings_by_user_id = self._parse_payload(saved_payload)
                return True

            next_settings: dict[int, ModWebUserSettings] = dict[int, ModWebUserSettings](self._settings_by_user_id)
            next_settings[validated_user_id] = settings
            self._save(next_settings)
            self._settings_by_user_id = next_settings
            return True

    def refresh(self) -> None:
        with self._lock:
            self._settings_by_user_id = self._load()
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._settings_by_user_id = self._load()
            self._loaded = True

    def _load(self) -> dict[int, ModWebUserSettings]:
        if config.DATA_AUTHORITY_MODE is config.DataAuthorityMode.LOCAL and not self._path.exists():
            return {}
        payload: dict[str, object] = config.load_authority_json(config.AuthorityResource.USER_SETTINGS, self._path)
        if not payload:
            return {}
        return self._parse_payload(payload)

    def _save(self, settings_by_user_id: Mapping[int, ModWebUserSettings]) -> None:
        payload: dict[str, object] = self._serialize_payload(settings_by_user_id)
        saved_payload: dict[str, object] = config.save_authority_json(
            config.AuthorityResource.USER_SETTINGS,
            self._path,
            payload,
        )
        parsed: dict[int, ModWebUserSettings] = self._parse_payload(saved_payload)
        if parsed != dict(settings_by_user_id):
            raise RuntimeError("Saved user settings did not match the requested values.")

    @staticmethod
    def _serialize_payload(settings_by_user_id: Mapping[int, ModWebUserSettings]) -> dict[str, object]:
        return {
            "version": _SETTINGS_SCHEMA_VERSION,
            "users": {
                str(user_id): settings.model_dump(mode="json")
                for user_id, settings in sorted(settings_by_user_id.items())
            },
        }

    @classmethod
    def _parse_payload(cls, payload: Mapping[str, object]) -> dict[int, ModWebUserSettings]:
        version: object | None = payload.get("version")
        users_payload: object | None = payload.get("users")
        if version is None and users_payload is None:
            return {}
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("User settings payload version must be an integer.")
        if version != _SETTINGS_SCHEMA_VERSION:
            raise ValueError(f"Unsupported user settings payload version: {version}.")
        if not isinstance(users_payload, Mapping):
            raise ValueError("User settings payload users must be an object.")

        parsed: dict[int, ModWebUserSettings] = {}
        for raw_user_id, raw_settings in users_payload.items():
            if not isinstance(raw_user_id, str) or not raw_user_id.isdecimal():
                raise ValueError("User settings payload user ids must be positive decimal strings.")
            user_id = cls._validate_user_id(int(raw_user_id))
            if not isinstance(raw_settings, Mapping):
                raise ValueError(f"User settings for {user_id} must be an object.")
            parsed[user_id] = ModWebUserSettings.model_validate(raw_settings)
        return parsed


__all__ = (
    "ModWebAppearanceSettings",
    "ModWebChatSettings",
    "ModWebColorScheme",
    "ModWebUserSettings",
    "ModWebUserSettingsStore",
)
