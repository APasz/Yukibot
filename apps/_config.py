from __future__ import annotations
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from _resolator import Resolutator
import config

log = logging.getLogger(__name__)


class App_Config(BaseModel):
    name: str
    friendly_name: str | None = None
    directory: Path
    apps_dir: Path
    mods_dir: Path | None = None
    settings_pointer: Path | None = None
    server_log_file: Path | None = None
    address: str = config.public_ip()
    scope: str
    chat_channel: str | None = None
    chat_ignore_symbol: str = config.CHAT_IGNORE
    enabled: bool = True
    cmd_start: list[str] = Field(default_factory=list)
    provider_alt_text: str | None = None

    @property
    def enabled_txt(self) -> str:
        return "Enabled" if self.enabled else "Disabled"

    model_config = ConfigDict(arbitrary_types_allowed=True, str_strip_whitespace=True)

    @field_validator("directory", "mods_dir", "settings_pointer", "server_log_file", mode="before")
    def resolve_dir(cls, raw: str | Path | None, info):
        if not raw or isinstance(raw, Path):
            return raw

        resolved = Resolutator.path_tokens(raw, {"WD": info.data.get("directory", "")})
        return Path(resolved)


class Mod_Config(BaseModel):
    name: str
    directory: Path
    added: datetime = Field(default_factory=datetime.now)
    enabled: bool = True
    version: str | None = None
    origin: str = "manual"
    coremod: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True, str_strip_whitespace=True)


# AiviA APasz
