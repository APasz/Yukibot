import asyncio
import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles

import config
from _file import File_Utils
from apps._config import (
    App_Config,
    ClientPackPolicy,
    Mod_Config,
    ModClassificationOverride,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModType,
)

log = logging.getLogger(__name__)
_MOD_SEPARATOR_RE = re.compile(r"[_\-\s]+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _split_camel_words(raw: str) -> tuple[str, ...]:
    return tuple(part for part in _CAMEL_BOUNDARY_RE.split(raw) if part)


def humanise_mod_identifier(raw: str, *, split_single_camel: bool) -> str:
    pieces: list[str] = []
    for chunk in _MOD_SEPARATOR_RE.split(raw.strip()):
        if not chunk:
            continue
        camel_parts = _split_camel_words(chunk)
        if len(camel_parts) > 1 and (split_single_camel or len(camel_parts) > 2):
            pieces.extend(camel_parts)
            continue
        pieces.append(chunk)

    rendered: list[str] = []
    for piece in pieces:
        if piece.isupper():
            rendered.append(piece)
        elif piece.islower():
            rendered.append(piece.title())
        else:
            rendered.append(piece)
    return " ".join(rendered)


class Mod(ABC):
    def __init__(self, cfg: Mod_Config, nice_name: str | None = None):
        self.cfg = cfg
        self._explicit_friendly = nice_name is not None
        "Mod_Config"
        self.name = cfg.name
        "Name of mod"
        self.friendly = cfg.metadata_overrides.friendly_name or nice_name or cfg.name
        # Path(cfg.name).stem.strip().replace("_", " ").replace("-", " ").title()
        "Hopefully more user friendly name"
        self.directory = cfg.directory
        "Directory of app's mods folder"
        if cfg.classification_override is None:
            if cfg.mod_type is ModType.REGULAR:
                cfg.mod_type = self.default_mod_type()
            if cfg.download_block_reason is None:
                cfg.download_block_reason = self.default_download_block_reason()

    @property
    def enabled_path(self) -> Path:
        return self.directory / self.name

    @property
    def disabled_path(self) -> Path:
        return self.enabled_path.with_suffix(".disabled")

    @property
    def client_path(self) -> Path:
        if self.cfg.client_path is not None:
            return self.cfg.client_path
        return self.enabled_path.with_suffix(".client")

    @property
    def path(self) -> Path:
        return self.enabled_path if self.cfg.enabled else self.disabled_path

    @classmethod
    def iter_candidates(cls, folder: Path) -> tuple[Path, ...]:
        return tuple(sorted(folder.iterdir(), key=lambda pointer: pointer.name.casefold()))

    @classmethod
    def config_from_candidate(
        cls,
        candidate: Path,
        modcf_cls: type[Mod_Config],
        *,
        folder: Path,
    ) -> Mod_Config | None:
        if candidate.name.endswith(".disabled"):
            return modcf_cls(name=candidate.with_suffix("").name, directory=folder, enabled=False)
        return modcf_cls(name=candidate.name, directory=folder)

    @property
    def downloadable(self) -> bool:
        return self.download_block_reason is None

    @property
    def mod_type(self) -> ModType:
        if self.cfg.classification_override is not None:
            return self.cfg.classification_override.mod_type
        return self.cfg.mod_type

    @property
    def download_block_reason(self) -> ModDownloadBlockReason | None:
        if self.cfg.classification_override is not None:
            return self.cfg.classification_override.download_block_reason
        return self.cfg.download_block_reason

    @property
    def version(self) -> str | None:
        return self.cfg.metadata_overrides.version or self.cfg.version

    @property
    def origin(self) -> str:
        return self.cfg.metadata_overrides.origin or self.cfg.origin

    @property
    def added(self) -> datetime:
        return self.cfg.metadata_overrides.added or self.cfg.added

    @property
    def is_coremod_type(self) -> bool:
        return self.mod_type is ModType.COREMOD

    @property
    def is_builtin(self) -> bool:
        return self.mod_type is ModType.BUILTIN

    @property
    def is_server_only(self) -> bool:
        return self.mod_type is ModType.SERVER

    @property
    def is_client(self) -> bool:
        return self.mod_type is ModType.CLIENT

    @property
    def is_protected(self) -> bool:
        return self.mod_type in (ModType.COREMOD, ModType.BUILTIN)

    @property
    def counts_as_coremod(self) -> bool:
        return self.mod_type in (ModType.COREMOD, ModType.BUILTIN)

    @property
    def download_block_label(self) -> str | None:
        if self.download_block_reason is None:
            return None
        return self.download_block_reason.label

    def default_mod_type(self) -> ModType:
        return ModType.REGULAR

    def default_download_block_reason(self) -> ModDownloadBlockReason | None:
        if self.is_builtin:
            return ModDownloadBlockReason.BUILTIN
        if self.is_server_only:
            return ModDownloadBlockReason.SERVER_ONLY
        return None

    def exists(self) -> bool:
        return self.enabled_path.exists() or self.disabled_path.exists()

    def sync_enabled_state(self) -> None:
        enabled_exists = self.enabled_path.exists()
        disabled_exists = self.disabled_path.exists()
        if enabled_exists and not disabled_exists:
            self.cfg.enabled = True
        elif disabled_exists and not enabled_exists:
            self.cfg.enabled = False

    def detect_version(self) -> str | None:
        return None

    def detect_friendly(self) -> str | None:
        return None

    def sync_metadata(self) -> None:
        self.sync_enabled_state()
        detected_version = self.detect_version()
        if detected_version is not None or self.cfg.version is None:
            self.cfg.version = detected_version
        if self.cfg.metadata_overrides.friendly_name is not None:
            self.friendly = self.cfg.metadata_overrides.friendly_name
        elif not self._explicit_friendly:
            detected_friendly = self.detect_friendly()
            if detected_friendly is not None and detected_friendly.strip():
                self.friendly = detected_friendly.strip()

    def is_coremod(self, silent: bool = False) -> bool:
        if not self.is_protected:
            return False
        if silent:
            return True
        raise RuntimeError("Coremod")

    async def _handle_drop(self, src: Path, atomic: bool = True):
        await asyncio.to_thread(File_Utils.move, src, self.path, atomic)
        log.info(f"Copied mod; {self.name}: {self.path}")

    async def _handle_extr(self, src: Path, atomic: bool = True):
        await asyncio.to_thread(File_Utils.extract, src, self.path.parent, atomic)
        log.info(f"Extracted mod; {self.name}: {self.path}")

    @abstractmethod
    async def install(self, src: Path, atomic: bool = True):
        raise NotImplementedError

    async def uninstall(self, override_coremod: bool = False) -> bool:
        if not override_coremod:
            self.is_coremod()
        return await asyncio.to_thread(File_Utils.remove, self.path)

    async def _enable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        target = self.enabled_path
        await asyncio.to_thread(File_Utils.move, self.disabled_path, target)
        self.cfg.enabled = True
        return target

    async def enable(self, override_coremod: bool = False) -> bool:
        self.sync_enabled_state()
        if self.cfg.enabled:
            return True
        return bool(await self._enable_file(override_coremod))

    async def _disable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        target = self.disabled_path
        await asyncio.to_thread(File_Utils.move, self.enabled_path, target)
        self.cfg.enabled = False
        return target

    async def disable(self, override_coremod: bool = False) -> bool:
        self.sync_enabled_state()
        if not self.cfg.enabled:
            return True
        return bool(await self._disable_file(override_coremod))

    async def toggle(self, state: bool, override_coremod: bool = False) -> bool:
        if state:
            return await self.enable(override_coremod)
        else:
            return await self.disable(override_coremod)

    def __repr__(self):
        return f"<Mod: {self.name} @ {self.path} | {self.cfg.enabled}>"

    def __hash__(self) -> int:
        return hash(self.path)


class Mod_Manager:
    _instances: dict[Path, "Mod_Manager"] = {}

    def __new__(cls, app_cfg: App_Config, *args, **kwargs):
        if not app_cfg.mods_dir:
            raise KeyError("App mod_dir not set")
        key = app_cfg.mods_dir.resolve()
        log.debug(f"Mod_Manager.__new__: {'reusing' if key in cls._instances else 'creating'} instance for {key}")
        if key in cls._instances:
            return cls._instances[key]
        instance = super().__new__(cls)
        cls._instances[key] = instance
        return instance

    def __init__(
        self,
        app_cfg: App_Config,
        mod_cls: type[Mod] = Mod,
        modcf_cls: type[Mod_Config] = Mod_Config,
        db_path: Path | None = None,
    ):
        if getattr(self, "_initialised", False):
            return
        self._initialised = True
        if not app_cfg.mods_dir:
            raise KeyError("App mod_dir not set")
        self.folder = app_cfg.mods_dir.resolve()
        if not self.folder.exists():
            log.debug(f"{app_cfg.name} mods folder missing")
            raise FileNotFoundError(f"{app_cfg.name} mods folder missing")

        if not mod_cls or not issubclass(mod_cls, Mod):
            raise ValueError(f"mod_cls not appropriate type: {type(mod_cls)}")  # pyright: ignore[reportUnreachable]
        else:
            self.mod_cls = mod_cls

        if not modcf_cls or not issubclass(modcf_cls, Mod_Config):
            raise ValueError(f"modcf_cls not appropriate type: {type(modcf_cls)}")  # pyright: ignore[reportUnreachable]
        else:
            self.modcf_cls = modcf_cls

        self.app_name = app_cfg.name or "~UNKNOWN~"
        self.index: dict[str, Mod] = {}
        self._lookup: dict[str, str] = {}

        if db_path:
            self.db_path = db_path
        else:
            slug = self._make_slug(app_cfg.apps_dir, db_path)
            self.db_path = app_cfg.apps_dir / f"moddb;{slug}.jsonl"

        if not self.db_path.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path.write_text("", config.STR_ENCODE)

    def _permit_lookup(self, trans: str, base: str, /) -> None:
        if trans:
            self._lookup[trans] = base
            self._lookup[trans.lower()] = base
            self._lookup[trans.upper()] = base
            self._lookup[trans.title()] = base
            self._lookup[trans.capitalize()] = base
            self._lookup[trans.casefold()] = base
            self._lookup[trans.swapcase()] = base

    def _rebuild_lookup(self) -> None:
        self._lookup.clear()
        for name, mod in self.index.items():
            self._permit_lookup(mod.name, name)
            self._permit_lookup(mod.friendly, name)

    def _make_slug(self, apps_dir: Path, db_path: Path | None = None):
        resolved = self.folder
        if db_path:
            db_path = db_path.resolve()
        if resolved.is_relative_to(apps_dir):
            resolved = resolved.relative_to(apps_dir)
        elif resolved.is_relative_to(Path.home()):
            resolved = resolved.relative_to(Path.home())
        elif db_path and resolved.is_relative_to(db_path):
            resolved = resolved.relative_to(db_path)
        log.debug(f"Mod_Manager: final.{resolved=}")
        if resolved.name.lower() in ("mod", "mods"):
            slug = resolved.parent.name.lower()
        else:
            slug = "_".join(p.lower() for p in resolved.parts[-2:])
        if not slug or len(slug) > 64:
            slug += "_" + hashlib.sha1(str(resolved).encode()).hexdigest()[:6]
        return slug

    def __contains__(self, mod_name: str) -> bool:
        return mod_name in self.index

    async def load_mods(self):
        log.info(f"Loading mod DB for {self.app_name} from {self.db_path}")

        if self.db_path.exists():
            async with aiofiles.open(self.db_path, mode="r") as f:
                content = await f.readlines()
            for index, line in enumerate(content):
                try:
                    data: dict[str, Any] = json.loads(line.strip())
                    if not data:
                        log.warning(f"Bad Input index{index + 1}: {line}")
                        continue
                    cfg = self.modcf_cls(**data)
                except Exception:
                    log.exception("ModCF Load")
                    continue
                mod = self.mod_cls(cfg)
                mod.sync_metadata()
                if mod.exists():
                    self.index[cfg.name] = mod
                elif cfg.name in self.index:
                    del self.index[cfg.name]

        known_files = set(self.index)
        known_candidate_names = {mod.path.name for mod in self.index.values()}
        for candidate in self.mod_cls.iter_candidates(self.folder):
            if candidate.name in known_candidate_names:
                continue
            cfg = self.mod_cls.config_from_candidate(candidate, self.modcf_cls, folder=self.folder)
            if cfg is None or cfg.name in known_files:
                continue
            mod = self.mod_cls(cfg)
            mod.sync_metadata()
            if not mod.exists():
                continue
            self.index[mod.name] = mod
            known_files.add(mod.name)
            known_candidate_names.add(candidate.name)

        self._rebuild_lookup()
        await self.save_mods()

    async def save_mods(self):
        self.validate_client_pack_configuration()
        lines = [m.cfg.model_dump_json() for m in self.index.values()]
        async with aiofiles.open(self.db_path, mode="w") as f:
            await f.write("\n".join(lines))

    def validate_client_pack_configuration(self, mods: Iterable[Mod] | None = None) -> None:
        choice_groups: dict[str, list[Mod]] = {}
        for mod in self.index.values() if mods is None else mods:
            client_pack = mod.cfg.client_pack
            if client_pack.policy is ClientPackPolicy.REQUIRED:
                continue
            if not mod.downloadable:
                raise ValueError(f"Client-pack mod {mod.name!r} must be downloadable")
            if client_pack.policy is ClientPackPolicy.ALTERNATIVE:
                assert client_pack.choice_group is not None
                choice_groups.setdefault(client_pack.choice_group, []).append(mod)

        for group_name, choices in choice_groups.items():
            if len(choices) < 2:
                raise ValueError(f"Client-pack choice group {group_name!r} requires at least two mods")
            default_count: int = sum(1 for mod in choices if mod.cfg.client_pack.default_choice)
            if default_count != 1:
                raise ValueError(
                    f"Client-pack choice group {group_name!r} requires exactly one default; found {default_count}"
                )

    async def reload_mods(self):
        self.index.clear()
        self._lookup.clear()
        await self.load_mods()

    async def add(self, src: Path, *, atomic: bool = True) -> Mod:
        if not src or not isinstance(src, Path):
            raise ValueError(f"src must be Path not: {type(src)}")  # pyright: ignore[reportUnreachable]
        mod = self.mod_cls(self.modcf_cls(name=src.name, directory=self.folder))
        await mod.install(src, atomic)
        mod.sync_metadata()
        self.index[mod.name] = mod
        self._rebuild_lookup()
        await self.save_mods()
        return mod

    async def remove(self, mod_name: str | Mod, *, override_coremod: bool = False) -> Mod:
        mod = self.get(mod_name)
        self.validate_client_pack_configuration(entry for entry in self.index.values() if entry is not mod)
        await mod.uninstall(override_coremod)
        del self.index[mod.name]
        self._rebuild_lookup()
        await self.save_mods()
        return mod

    async def set_enabled(self, mod_name: str | Mod, state: bool, *, override_coremod: bool = False) -> Mod:
        mod = self.get(mod_name)
        mod.sync_metadata()
        if mod.cfg.enabled == state:
            return mod
        await mod.toggle(state, override_coremod)
        mod.sync_metadata()
        await self.save_mods()
        return mod

    async def toggle(self, mod_name: str | Mod, *, override_coremod: bool = False) -> Mod:
        mod = self.get(mod_name)
        mod.sync_metadata()
        await mod.toggle(not mod.cfg.enabled, override_coremod)
        mod.sync_metadata()
        await self.save_mods()
        return mod

    async def set_coremod(self, mod_name: str | Mod, state: bool) -> Mod:
        mod = self.get(mod_name)
        if not state and not mod.is_coremod_type:
            return mod
        next_mod_type = ModType.COREMOD if state else ModType.REGULAR
        if mod.cfg.classification_override is not None:
            mod.cfg.classification_override = mod.cfg.classification_override.model_copy(
                update={"mod_type": next_mod_type}
            )
        elif state or mod.is_coremod_type:
            mod.cfg.mod_type = next_mod_type
        await self.save_mods()
        return mod

    async def set_download_block_reason(
        self,
        mod_name: str | Mod,
        reason: ModDownloadBlockReason | None,
    ) -> Mod:
        mod = self.get(mod_name)
        if reason is not None and mod.cfg.client_pack.policy is not ClientPackPolicy.REQUIRED:
            raise ValueError(f"Client-pack mod {mod.name!r} cannot be blocked from downloads")
        if mod.cfg.classification_override is not None:
            mod.cfg.classification_override = mod.cfg.classification_override.model_copy(
                update={"download_block_reason": reason}
            )
        else:
            mod.cfg.download_block_reason = reason
        await self.save_mods()
        return mod

    async def update_properties(
        self,
        mod_name: str | Mod,
        *,
        mod_type: ModType,
        download_block_reason: ModDownloadBlockReason | None,
        metadata_overrides: ModMetadataOverrides,
    ) -> Mod:
        mod = self.get(mod_name)
        if mod_type is ModType.BUILTIN:
            raise ValueError("Mods cannot be converted to built-in mods")
        if download_block_reason is ModDownloadBlockReason.BUILTIN:
            raise ValueError("The built-in download block reason is reserved for detected built-in mods")
        previous_classification_override = mod.cfg.classification_override
        previous_metadata_overrides = mod.cfg.metadata_overrides
        previous_friendly = mod.friendly
        try:
            mod.cfg.classification_override = ModClassificationOverride(
                mod_type=mod_type,
                download_block_reason=download_block_reason,
            )
            mod.cfg.metadata_overrides = metadata_overrides
            mod.sync_metadata()
            self._rebuild_lookup()
            await self.save_mods()
        except Exception:
            mod.cfg.classification_override = previous_classification_override
            mod.cfg.metadata_overrides = previous_metadata_overrides
            mod.friendly = previous_friendly
            self._rebuild_lookup()
            raise
        return mod

    def get(self, name: str | Mod) -> Mod:
        if isinstance(name, Mod):
            return name
        if mod_name := self._lookup.get(name):
            return self.index[mod_name]
        raise ModuleNotFoundError(f"No such Mod: {name}")

    __getitem__ = get

    def list_mods(self, state: bool | None = None) -> list[Mod]:
        mods = sorted(self.index.values(), key=lambda m: m.cfg.added)
        match state:
            case None:
                return mods
            case True:
                return [mod for mod in mods if mod.cfg.enabled]
            case False:
                return [mod for mod in mods if not mod.cfg.enabled]

    def list_mods_json(self) -> list[Mod] | Mapping[str, str]:
        return {name: mod.cfg.model_dump_json(indent=4) for name, mod in self.index.items()}

    def list_names(self, state: bool | None = None) -> list[str]:
        return sorted((mod.name for mod in self.list_mods(state)), key=str.lower)


# AiviA APasz
