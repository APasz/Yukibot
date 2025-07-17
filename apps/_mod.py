from abc import abstractmethod
import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path

import aiofiles

import config
from _file import File_Utils
from apps._config import App_Config, Mod_Config

log = logging.getLogger(__name__)


class Mod:
    def __init__(self, cfg: Mod_Config, nice_name: str | None = None):
        self.cfg = cfg
        "Mod_Config"
        self.name = cfg.name
        "Name of mod"
        self.friendly = nice_name or cfg.name
        # Path(cfg.name).stem.strip().replace("_", " ").replace("-", " ").title()
        "Hopefully more user friendly name"
        self.directory = cfg.directory
        "Directory of app's mods folder"
        self.path = cfg.directory.joinpath(cfg.name)
        "Path to mod in app's mods folder"

    def is_coremod(self, silent: bool = False) -> bool:
        if not self.cfg.coremod:
            return False
        if silent:
            return True
        raise RuntimeError("Coremod")

    async def _handle_drop(self, src: Path, atomic: bool = True):
        self.path = await asyncio.to_thread(File_Utils.move, src, self.path, atomic)
        log.info(f"Copied mod; {self.name}: {self.path}")

    async def _handle_extr(self, src: Path, atomic: bool = True):
        self.path = await asyncio.to_thread(File_Utils.extract, src, self.path.parent, atomic)
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
        self.cfg.enabled = True
        return await asyncio.to_thread(File_Utils.move, self.path.with_suffix(".disabled"), self.path)

    async def enable(self, override_coremod: bool = False) -> bool:
        return bool(await self._enable_file(override_coremod))

    async def _disable_file(self, override_coremod: bool = False) -> Path:
        if not override_coremod:
            self.is_coremod()
        self.cfg.enabled = False
        return await asyncio.to_thread(File_Utils.move, self.path, self.path.with_suffix(".disabled"))

    async def disable(self, override_coremod: bool = False) -> bool:
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
            raise KeyError("App's mod_dir not set")
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
            raise KeyError("App's mod_dir not set")
        self.folder = app_cfg.mods_dir.resolve()
        if not self.folder.exists():
            log.debug(f"{app_cfg.name} mods folder missing")
            raise FileNotFoundError(f"{app_cfg.name} mods folder missing")

        if not mod_cls or not issubclass(mod_cls, Mod):
            raise ValueError(f"mod_cls not appropriate type: {type(mod_cls)}")
        else:
            self.mod_cls = mod_cls

        if not modcf_cls or not issubclass(modcf_cls, Mod_Config):
            raise ValueError(f"modcf_cls not appropriate type: {type(modcf_cls)}")
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
            self.db_path.write_text("{}", config.STR_ENCODE)

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
                    data: dict = json.loads(line.strip())
                    if not data:
                        log.warning(f"Bad Input index{index + 1}: {line}")
                        continue
                    cfg = self.modcf_cls(**data)
                except Exception:
                    log.exception("ModCF Load")
                    continue
                pointer = cfg.directory / cfg.name
                if pointer.exists():
                    self.index[cfg.name] = self.mod_cls(cfg)
                elif cfg.name in self.index:
                    del self.index[cfg.name]

            def permitate(trans: str, base: str, /):
                if trans:
                    self._lookup[trans] = base
                    self._lookup[trans.lower()] = base
                    self._lookup[trans.upper()] = base
                    self._lookup[trans.title()] = base
                    self._lookup[trans.capitalize()] = base
                    self._lookup[trans.casefold()] = base
                    self._lookup[trans.swapcase()] = base

            for name, mod in self.index.items():
                permitate(mod.name, name)
                permitate(mod.friendly, name)

        for file in self.folder.iterdir():
            if file.name not in self.index:
                self.index[file.name] = self.mod_cls(self.modcf_cls(name=file.name, directory=self.folder))

        await self.save_mods()

    async def save_mods(self):
        lines = [m.cfg.model_dump_json() for m in self.index.values()]
        async with aiofiles.open(self.db_path, mode="w") as f:
            await f.write("\n".join(lines))

    async def reload_mods(self):
        self.index.clear()
        await self.load_mods()

    async def add(self, src: Path):
        if not src or not isinstance(src, Path):
            raise ValueError(f"src must be Path not: {type(src)}")
        mod = self.mod_cls(self.modcf_cls(name=src.name, directory=self.folder))
        await mod.install(src)
        self.index[mod.name] = mod

    async def remove(self, mod_name: str | Mod):
        mod = self.get(mod_name)
        await mod.uninstall()
        del self.index[mod.name]

    def get(self, name: str | Mod) -> Mod:
        if isinstance(name, Mod):
            return name
        if mod_name := self._lookup.get(name):
            return self.index[mod_name]
        raise ModuleNotFoundError(f"No such Mod: {name}")

    __getitem__ = get

    def list_mods(self) -> list[Mod]:
        return sorted(self.index.values(), key=lambda m: m.cfg.added)

    def list_mods_json(self) -> list[Mod] | Mapping[str, str]:
        return {name: mod.cfg.model_dump_json(indent=4) for name, mod in self.index.items()}

    def list_names(self, state: bool | None = None) -> list[str]:
        match state:
            case None:
                return sorted([m.name for m in self.index.values()], key=str.lower)
            case True:
                return sorted([m.name for m in self.index.values() if m.cfg.enabled], key=str.lower)
            case False:
                return sorted([m.name for m in self.index.values() if not m.cfg.enabled], key=str.lower)


# AiviA APasz
