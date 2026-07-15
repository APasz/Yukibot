import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import aiofiles
from modmux import parse_url
from modmux.models import Provider

import config
from _async_utils import run_blocking
from _file import File_Utils
from apps._config import (
    App_Config,
    BulkLauncherMetadataEntry,
    BulkLauncherMetadataStatus,
    ClientPackConfig,
    ClientPackPolicy,
    Mod_Config,
    ModClassificationOverride,
    ModDownloadBlockReason,
    ModMetadataOverrides,
    ModPageLink,
    ModPlacement,
    ModPlatformMetadata,
    ModType,
    is_client_pack_candidate,
)
from apps._mod_metadata import ModIdentity, ModIdentityKind, ScopeModMetadataStore, SharedModMetadata

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
        if cfg.classification_override is not None:
            if cfg.classification_override.download_block_reason is ModDownloadBlockReason.SERVER_ONLY:
                cfg.classification_override = cfg.classification_override.model_copy(
                    update={"download_block_reason": None}
                )
        elif cfg.download_block_reason is ModDownloadBlockReason.SERVER_ONLY:
            cfg.download_block_reason = None
        cfg.client_pack.apply_default_inclusion(self.mod_type)

    @property
    def enabled_path(self) -> Path:
        return self.directory / self.name

    @staticmethod
    def _append_marker(pointer: Path, marker: str) -> Path:
        return pointer.with_name(f"{pointer.name}.{marker}")

    @property
    def disabled_path(self) -> Path:
        return self._append_marker(self.enabled_path, "disabled")

    @property
    def legacy_disabled_path(self) -> Path:
        return self.enabled_path.with_suffix(".disabled")

    @property
    def client_path(self) -> Path:
        if self.cfg.client_path is not None:
            return self.cfg.client_path
        return self._append_marker(self.enabled_path, "client")

    @property
    def storage_path(self) -> Path:
        return self.placement_path

    @property
    def placement_path(self) -> Path:
        return self.path_for_placement(self.cfg.placement)

    def path_for_placement(self, placement: ModPlacement) -> Path:
        match placement:
            case ModPlacement.SERVER_ENABLED:
                return self.enabled_path
            case ModPlacement.SERVER_DISABLED:
                return self.disabled_path
            case ModPlacement.CLIENT_ONLY:
                return self.client_path

    @property
    def path(self) -> Path:
        return self.storage_path

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
            return modcf_cls(
                name=candidate.name.removesuffix(".disabled"),
                directory=folder,
                placement=ModPlacement.SERVER_DISABLED,
            )
        if candidate.name.endswith(".client"):
            return modcf_cls(
                name=candidate.name.removesuffix(".client"),
                directory=folder,
                placement=ModPlacement.CLIENT_ONLY,
            )
        return modcf_cls(name=candidate.name, directory=folder)

    @property
    def downloadable(self) -> bool:
        return self.download_block_reason in {None, ModDownloadBlockReason.SERVER_ONLY}

    @property
    def server_loadable(self) -> bool:
        return self.cfg.placement.server_loadable

    @property
    def client_pack_eligible(self) -> bool:
        return self.client_pack_candidate and self.downloadable

    @property
    def client_pack_candidate(self) -> bool:
        placement_allows_client_pack = is_client_pack_candidate(self.cfg.placement, self.mod_type.side)
        return placement_allows_client_pack and self.cfg.client_pack.included_in_client

    @property
    def logical_archive_name(self) -> str:
        return self.name

    def download_archive_path_rewrites(self) -> tuple[tuple[PurePosixPath, PurePosixPath], ...]:
        return ()

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
    def description(self) -> str | None:
        return self.cfg.platforms.description or self.detect_description()

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
        if self.downloadable:
            return None
        assert self.download_block_reason is not None
        return self.download_block_reason.label

    def default_mod_type(self) -> ModType:
        return ModType.REGULAR

    def default_download_block_reason(self) -> ModDownloadBlockReason | None:
        if self.is_builtin:
            return ModDownloadBlockReason.BUILTIN
        return None

    def exists(self) -> bool:
        return self.storage_path.exists()

    def _migrate_legacy_disabled_file(self) -> None:
        legacy_path = self.legacy_disabled_path
        if (
            self.cfg.placement is not ModPlacement.SERVER_DISABLED
            or legacy_path == self.disabled_path
            or not legacy_path.exists()
        ):
            return
        conflicting_paths = tuple(
            pointer
            for pointer in (self.enabled_path, self.disabled_path, self.client_path)
            if pointer.exists()
        )
        if conflicting_paths:
            raise RuntimeError(
                f"Legacy disabled mod conflicts with canonical placement: {legacy_path}; "
                f"existing: {', '.join(str(pointer) for pointer in conflicting_paths)}"
            )
        File_Utils.move(legacy_path, self.disabled_path, overwrite=False)
        log.info("Migrated legacy disabled mod path: %s -> %s", legacy_path, self.disabled_path)

    def sync_enabled_state(self) -> None:
        self._migrate_legacy_disabled_file()
        existing_placements = tuple(
            placement
            for placement, pointer in (
                (ModPlacement.SERVER_ENABLED, self.enabled_path),
                (ModPlacement.SERVER_DISABLED, self.disabled_path),
                (ModPlacement.CLIENT_ONLY, self.client_path),
            )
            if pointer.exists()
        )
        if len(existing_placements) > 1:
            raise RuntimeError(f"Mod has files in multiple placements: {self.name}")
        if existing_placements:
            self.cfg.set_placement(existing_placements[0])
        else:
            self.cfg.set_placement(self.cfg.placement)

    def sync_classification_placement(self) -> None:
        desired_placement = (
            ModPlacement.CLIENT_ONLY
            if self.mod_type is ModType.CLIENT
            else (
                ModPlacement.SERVER_ENABLED
                if self.cfg.placement is ModPlacement.CLIENT_ONLY
                else self.cfg.placement
            )
        )
        if desired_placement is self.cfg.placement:
            return

        source_path = self.placement_path
        if not source_path.exists():
            return
        self.move_to_placement(desired_placement)

    def move_to_placement(self, placement: ModPlacement) -> None:
        if placement is self.cfg.placement:
            return
        source_path = self.placement_path
        if not source_path.exists():
            raise FileNotFoundError(f"Cannot move missing mod representation: {source_path}")
        target_path = self.path_for_placement(placement)
        if target_path.exists():
            raise FileExistsError(
                f"Cannot move mod to {placement.label}: target already exists: {target_path}"
            )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        File_Utils.move(source_path, target_path, overwrite=False)
        self.cfg.set_placement(placement)
        log.info(
            "Moved mod for %s classification: %s -> %s",
            self.mod_type.label,
            source_path,
            target_path,
        )

    def detect_version(self) -> str | None:
        return None

    def detect_friendly(self) -> str | None:
        return None

    def detect_description(self) -> str | None:
        return None

    def native_metadata_id(self) -> str | None:
        return None

    def metadata_fallback_id(self) -> str:
        stem = Path(self.name).stem
        version = self.version
        if version is not None and stem.casefold().endswith(version.casefold()):
            stem = stem[: -len(version)].rstrip("-_. ")
        return stem.casefold()

    def metadata_identities(self, *, scope: str) -> tuple[ModIdentity, ...]:
        identities: list[ModIdentity] = []
        if self.cfg.platforms.modrinth is not None:
            identities.append(
                ModIdentity(
                    kind=ModIdentityKind.PROVIDER,
                    namespace=Provider.MODRINTH.value,
                    value=self.cfg.platforms.modrinth.project_id,
                )
            )
        if self.cfg.platforms.curseforge is not None:
            identities.append(
                ModIdentity(
                    kind=ModIdentityKind.PROVIDER,
                    namespace=Provider.CURSEFORGE.value,
                    value=str(self.cfg.platforms.curseforge.project_id),
                )
            )
        for page in self.cfg.mod_pages:
            provider_id = parse_url(page.url)
            if provider_id is None:
                continue
            identities.append(
                ModIdentity(
                    kind=ModIdentityKind.PROVIDER,
                    namespace=provider_id.provider.value,
                    value=provider_id.id,
                )
            )
        if native_id := self.native_metadata_id():
            identities.append(
                ModIdentity(
                    kind=ModIdentityKind.NATIVE,
                    namespace=scope,
                    value=native_id,
                )
            )
        identities.append(
            ModIdentity(
                kind=ModIdentityKind.FILENAME,
                namespace=scope,
                value=self.metadata_fallback_id(),
            )
        )
        unique: list[ModIdentity] = []
        known_keys: set[tuple[ModIdentityKind, str, str]] = set()
        for identity in identities:
            if identity.key in known_keys:
                continue
            unique.append(identity)
            known_keys.add(identity.key)
        return tuple(unique)

    def sync_metadata(self) -> None:
        self.sync_enabled_state()
        self.sync_classification_placement()
        if self.cfg.placement is not ModPlacement.CLIENT_ONLY:
            self.cfg.set_placement(
                ModPlacement.SERVER_ENABLED if self.cfg.enabled else ModPlacement.SERVER_DISABLED
            )
        detected_version = self.detect_version()
        if detected_version is not None or self.cfg.version is None:
            self.cfg.version = detected_version
        if self.cfg.metadata_overrides.friendly_name is not None:
            self.friendly = self.cfg.metadata_overrides.friendly_name
        elif not self._explicit_friendly:
            detected_friendly = self.detect_friendly()
            if detected_friendly is not None and detected_friendly.strip():
                self.friendly = detected_friendly.strip()

    @classmethod
    async def sync_external_metadata_batch(cls, mods: Iterable["Mod"]) -> None:
        """Enrich locally detected metadata from app-specific external providers."""

    def is_coremod(self, silent: bool = False) -> bool:
        if not self.is_protected:
            return False
        if silent:
            return True
        raise RuntimeError("Coremod")

    async def _handle_drop(self, src: Path, atomic: bool = True):
        await run_blocking(File_Utils.move, src, self.path, atomic)
        log.info(f"Copied mod; {self.name}: {self.path}")

    async def _handle_extr(self, src: Path, atomic: bool = True):
        await run_blocking(File_Utils.extract, src, self.path.parent, atomic)
        log.info(f"Extracted mod; {self.name}: {self.path}")

    @abstractmethod
    async def install(self, src: Path, atomic: bool = True):
        raise NotImplementedError

    async def uninstall(self, override_coremod: bool = False) -> bool:
        if not override_coremod:
            self.is_coremod()
        return await run_blocking(File_Utils.remove, self.path)

    async def _enable_file(self, override_coremod: bool = False) -> Path:
        if self.cfg.placement is ModPlacement.CLIENT_ONLY:
            raise ValueError(f"Client-only mod cannot be enabled on the server: {self.name}")
        if not override_coremod:
            self.is_coremod()
        target = self.enabled_path
        await run_blocking(File_Utils.move, self.disabled_path, target)
        self.cfg.set_placement(ModPlacement.SERVER_ENABLED)
        return target

    async def enable(self, override_coremod: bool = False) -> bool:
        self.sync_enabled_state()
        if self.cfg.placement is ModPlacement.CLIENT_ONLY:
            raise ValueError(f"Client-only mod cannot be enabled on the server: {self.name}")
        if self.cfg.enabled:
            return True
        return bool(await self._enable_file(override_coremod))

    async def _disable_file(self, override_coremod: bool = False) -> Path:
        if self.cfg.placement is ModPlacement.CLIENT_ONLY:
            raise ValueError(f"Client-only mod cannot be disabled on the server: {self.name}")
        if not override_coremod:
            self.is_coremod()
        target = self.disabled_path
        await run_blocking(File_Utils.move, self.enabled_path, target)
        self.cfg.set_placement(ModPlacement.SERVER_DISABLED)
        return target

    async def disable(self, override_coremod: bool = False) -> bool:
        self.sync_enabled_state()
        if self.cfg.placement is ModPlacement.CLIENT_ONLY:
            raise ValueError(f"Client-only mod cannot be disabled on the server: {self.name}")
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
    _instances: dict[tuple[Path, Path | None], "Mod_Manager"] = {}

    def __new__(cls, app_cfg: App_Config, *args, **kwargs):
        if not app_cfg.mods_dir:
            raise KeyError("App mod_dir not set")
        key = (
            app_cfg.mods_dir.resolve(),
            app_cfg.client_mods_dir.resolve() if app_cfg.client_mods_dir is not None else None,
        )
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
        self.client_folder = (
            app_cfg.client_mods_dir.resolve() if app_cfg.client_mods_dir is not None else None
        )
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
        self.scope = (app_cfg.scope or self.app_name).strip().casefold()
        self.index: dict[str, Mod] = {}
        self._lookup: dict[str, str] = {}

        scope_slug = re.sub(r"[^a-z0-9_.-]+", "-", self.scope).strip("-.") or "unknown"
        self.shared_metadata = ScopeModMetadataStore(
            scope=self.scope,
            path=app_cfg.apps_dir / f"mod-metadata;{scope_slug}.json",
        )

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

    @property
    def has_separate_client_folder(self) -> bool:
        return self.client_folder is not None and self.client_folder != self.folder

    def _server_candidate_config(self, candidate: Path) -> Mod_Config | None:
        if candidate.name.endswith(".client"):
            return self.modcf_cls(
                name=candidate.name.removesuffix(".client"),
                directory=self.folder,
                placement=ModPlacement.CLIENT_ONLY,
                mod_type=ModType.CLIENT,
            )
        return self.mod_cls.config_from_candidate(candidate, self.modcf_cls, folder=self.folder)

    def _client_candidate_config(self, candidate: Path) -> Mod_Config:
        if candidate.name.endswith(".disabled"):
            raise ValueError(f"Client mod cannot use the disabled marker: {candidate}")
        name = candidate.name.removesuffix(".client")
        if not name:
            raise ValueError(f"Client mod name is empty: {candidate}")
        return self.modcf_cls(
            name=name,
            directory=self.folder,
            client_path=candidate,
            placement=ModPlacement.CLIENT_ONLY,
            mod_type=ModType.CLIENT,
        )

    def _discovery_candidates(self) -> tuple[tuple[Path, Mod_Config], ...]:
        server_candidates = set(self.mod_cls.iter_candidates(self.folder))
        server_candidates.update(pointer for pointer in self.folder.iterdir() if pointer.name.endswith(".client"))

        discovered: list[tuple[Path, Mod_Config]] = []
        for candidate in sorted(server_candidates, key=lambda pointer: pointer.name.casefold()):
            cfg = self._server_candidate_config(candidate)
            if cfg is not None:
                discovered.append((candidate, cfg))

        if self.has_separate_client_folder and self.client_folder is not None and self.client_folder.exists():
            for candidate in sorted(self.client_folder.iterdir(), key=lambda pointer: pointer.name.casefold()):
                discovered.append((candidate, self._client_candidate_config(candidate)))
        return tuple(discovered)

    @staticmethod
    def _validate_discovery_conflicts(candidates: Iterable[tuple[Path, Mod_Config]]) -> None:
        representations: dict[str, list[tuple[str, Path]]] = {}
        for candidate, cfg in candidates:
            representations.setdefault(cfg.name.casefold(), []).append((cfg.name, candidate))
        for entries in representations.values():
            if len(entries) < 2:
                continue
            name = entries[0][0]
            paths = ", ".join(str(pointer) for _entry_name, pointer in entries)
            raise RuntimeError(f"Mod has conflicting physical representations: {name}: {paths}")

    @staticmethod
    def _mod_exists(mod: Mod) -> bool:
        if mod.cfg.placement is ModPlacement.CLIENT_ONLY:
            return mod.storage_path.exists()
        return mod.exists()

    @staticmethod
    def _legacy_classification_override(mod: Mod) -> ModClassificationOverride | None:
        if mod.cfg.classification_override is not None:
            return mod.cfg.classification_override
        default_mod_type = mod.default_mod_type()
        if mod.cfg.mod_type is ModType.COREMOD and default_mod_type is not ModType.COREMOD:
            return ModClassificationOverride(
                mod_type=ModType.COREMOD,
                download_block_reason=mod.cfg.download_block_reason,
            )
        if mod.cfg.download_block_reason in {
            ModDownloadBlockReason.ARTIFACT,
            ModDownloadBlockReason.OTHER,
        }:
            return ModClassificationOverride(
                mod_type=mod.cfg.mod_type,
                download_block_reason=mod.cfg.download_block_reason,
            )
        return None

    def _apply_shared_metadata(self, mod: Mod, shared: SharedModMetadata | None) -> None:
        mod.cfg.metadata_overrides = mod.cfg.metadata_overrides.model_copy(
            update={"friendly_name": None if shared is None else shared.friendly_name}
        )
        mod.cfg.classification_override = None if shared is None else shared.classification_override
        mod.cfg.mod_pages = () if shared is None else shared.mod_pages
        mod.sync_metadata()
        mod.cfg.client_pack.apply_default_inclusion(mod.mod_type)

    def _migrate_shared_metadata(self, mods: Iterable[Mod]) -> None:
        for mod in mods:
            identities = mod.metadata_identities(scope=self.scope)
            shared = self.shared_metadata.migrate(
                identities=identities,
                friendly_name=mod.cfg.metadata_overrides.friendly_name,
                classification_override=self._legacy_classification_override(mod),
                mod_pages=mod.cfg.mod_pages,
            )
            self._apply_shared_metadata(mod, shared)
        self.shared_metadata.save_if_dirty()

    def _set_shared_metadata(self, mod: Mod) -> None:
        shared = self.shared_metadata.set(
            identities=mod.metadata_identities(scope=self.scope),
            friendly_name=mod.cfg.metadata_overrides.friendly_name,
            classification_override=mod.cfg.classification_override,
            mod_pages=mod.cfg.mod_pages,
        )
        self.shared_metadata.save_if_dirty()
        self._apply_shared_metadata(mod, shared)

    def _restore_shared_metadata(
        self,
        *,
        identities: tuple[ModIdentity, ...],
        shared: SharedModMetadata | None,
    ) -> None:
        self.shared_metadata.set(
            identities=identities,
            friendly_name=None if shared is None else shared.friendly_name,
            classification_override=None if shared is None else shared.classification_override,
            mod_pages=() if shared is None else shared.mod_pages,
        )
        self.shared_metadata.save_if_dirty()

    @staticmethod
    def _instance_config(mod: Mod) -> Mod_Config:
        instance_config = mod.cfg.model_copy(deep=True)
        instance_config.metadata_overrides = instance_config.metadata_overrides.model_copy(
            update={"friendly_name": None}
        )
        instance_config.classification_override = None
        instance_config.mod_pages = ()
        base_mod_type = ModType.CLIENT if mod.cfg.placement is ModPlacement.CLIENT_ONLY else mod.default_mod_type()
        instance_config.mod_type = base_mod_type
        instance_config.download_block_reason = (
            ModDownloadBlockReason.BUILTIN if base_mod_type is ModType.BUILTIN else None
        )
        return instance_config

    async def load_mods(self):
        log.info(f"Loading mod DB for {self.app_name} from {self.db_path}")
        discovery_candidates = self._discovery_candidates()
        self._validate_discovery_conflicts(discovery_candidates)

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
                if self._mod_exists(mod):
                    self.index[cfg.name] = mod
                elif cfg.name in self.index:
                    del self.index[cfg.name]

        known_files = set(self.index)
        known_candidate_names = {mod.storage_path.name for mod in self.index.values()}
        for candidate, cfg in discovery_candidates:
            if candidate.name in known_candidate_names:
                continue
            if cfg.name in known_files:
                continue
            mod = self.mod_cls(cfg)
            mod.sync_metadata()
            if not self._mod_exists(mod):
                continue
            self.index[mod.name] = mod
            known_files.add(mod.name)
            known_candidate_names.add(candidate.name)

        self._migrate_shared_metadata(self.index.values())
        await self.mod_cls.sync_external_metadata_batch(tuple(self.index.values()))
        self._migrate_shared_metadata(self.index.values())
        self._rebuild_lookup()
        await self.save_mods()

    async def save_mods(self):
        self.validate_client_pack_configuration()
        self.shared_metadata.save_if_dirty()
        lines = [self._instance_config(mod).model_dump_json() for mod in self.index.values()]
        async with aiofiles.open(self.db_path, mode="w") as f:
            await f.write("\n".join(lines))

    def validate_client_pack_configuration(self, mods: Iterable[Mod] | None = None) -> None:
        choice_groups: dict[str, list[Mod]] = {}
        for mod in self.index.values() if mods is None else mods:
            if not mod.client_pack_candidate:
                continue
            client_pack = mod.cfg.client_pack
            if client_pack.policy is ClientPackPolicy.REQUIRED:
                continue
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

    async def add(
        self,
        src: Path,
        *,
        atomic: bool = True,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
    ) -> Mod:
        if not src or not isinstance(src, Path):
            raise ValueError(f"src must be Path not: {type(src)}")  # pyright: ignore[reportUnreachable]
        if placement is ModPlacement.SERVER_DISABLED:
            raise ValueError("New mod uploads cannot start server-disabled")
        if src.name.endswith((".disabled", ".client")):
            raise ValueError(f"Uploaded mod name uses a reserved placement marker: {src.name}")

        if placement is ModPlacement.CLIENT_ONLY:
            client_path = (
                self.client_folder / src.name
                if self.has_separate_client_folder and self.client_folder is not None
                else Mod._append_marker(self.folder / src.name, "client")
            )
            cfg = self.modcf_cls(
                name=src.name,
                directory=self.folder,
                client_path=client_path,
                placement=placement,
                mod_type=ModType.CLIENT,
            )
        else:
            cfg = self.modcf_cls(name=src.name, directory=self.folder, placement=placement)

        mod = self.mod_cls(cfg)
        conflicting_paths = tuple(
            pointer
            for pointer in {mod.enabled_path, mod.disabled_path, mod.client_path, mod.legacy_disabled_path}
            if pointer.exists()
        )
        if conflicting_paths:
            raise FileExistsError(
                f"Mod already has a physical representation: {mod.name}: "
                f"{', '.join(str(pointer) for pointer in sorted(conflicting_paths))}"
            )
        if placement is ModPlacement.CLIENT_ONLY:
            mod.storage_path.parent.mkdir(parents=True, exist_ok=True)
            await run_blocking(File_Utils.move, src, mod.storage_path, atomic)
        else:
            await mod.install(src, atomic)
        mod.sync_metadata()
        self._migrate_shared_metadata((mod,))
        await self.mod_cls.sync_external_metadata_batch((mod,))
        self._migrate_shared_metadata((mod,))
        self.index[mod.name] = mod
        self._rebuild_lookup()
        await self.save_mods()
        return mod

    async def remove(self, mod_name: str | Mod, *, override_coremod: bool = False) -> Mod:
        mod = self.get(mod_name)
        self.validate_client_pack_configuration(entry for entry in self.index.values() if entry is not mod)
        if mod.cfg.placement is ModPlacement.CLIENT_ONLY:
            await run_blocking(File_Utils.remove, mod.storage_path)
        else:
            await mod.uninstall(override_coremod)
        del self.index[mod.name]
        self._rebuild_lookup()
        await self.save_mods()
        return mod

    async def set_enabled(self, mod_name: str | Mod, state: bool, *, override_coremod: bool = False) -> Mod:
        mod = self.get(mod_name)
        mod.sync_metadata()
        if mod.cfg.placement is ModPlacement.CLIENT_ONLY:
            raise ValueError(f"Client-only mod has no server enabled state: {mod.name}")
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
        current_reason = mod.download_block_reason
        mod.cfg.classification_override = ModClassificationOverride(
            mod_type=next_mod_type,
            download_block_reason=current_reason,
        )
        self._set_shared_metadata(mod)
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
        mod.cfg.classification_override = ModClassificationOverride(
            mod_type=mod.mod_type,
            download_block_reason=reason,
        )
        self._set_shared_metadata(mod)
        await self.save_mods()
        return mod

    async def update_properties(
        self,
        mod_name: str | Mod,
        *,
        mod_type: ModType,
        download_block_reason: ModDownloadBlockReason | None,
        metadata_overrides: ModMetadataOverrides,
        mod_pages: tuple[ModPageLink, ...] | None = None,
        client_pack: ClientPackConfig | None = None,
        platforms: ModPlatformMetadata | None = None,
    ) -> Mod:
        mod = self.get(mod_name)
        if mod_type is ModType.BUILTIN:
            raise ValueError("Mods cannot be converted to built-in mods")
        if download_block_reason is ModDownloadBlockReason.BUILTIN:
            raise ValueError("The built-in download block reason is reserved for detected built-in mods")
        previous_classification_override = mod.cfg.classification_override
        previous_mod_pages = mod.cfg.mod_pages
        previous_metadata_overrides = mod.cfg.metadata_overrides
        previous_client_pack = mod.cfg.client_pack
        previous_platforms = mod.cfg.platforms
        previous_placement = mod.cfg.placement
        previous_friendly = mod.friendly
        previous_shared_identities = mod.metadata_identities(scope=self.scope)
        previous_shared = self.shared_metadata.resolve(previous_shared_identities)
        try:
            mod.cfg.classification_override = ModClassificationOverride(
                mod_type=mod_type,
                download_block_reason=download_block_reason,
            )
            if mod_pages is not None:
                mod.cfg.mod_pages = mod_pages
            mod.cfg.metadata_overrides = metadata_overrides
            if client_pack is not None:
                mod.cfg.client_pack = client_pack
            if platforms is not None:
                mod.cfg.platforms = platforms
            mod.sync_metadata()
            await self.mod_cls.sync_external_metadata_batch((mod,))
            self._set_shared_metadata(mod)
            self._rebuild_lookup()
            await self.save_mods()
        except Exception:
            self._restore_shared_metadata(
                identities=previous_shared_identities,
                shared=previous_shared,
            )
            mod.cfg.classification_override = previous_classification_override
            mod.cfg.mod_pages = previous_mod_pages
            mod.cfg.metadata_overrides = previous_metadata_overrides
            mod.cfg.client_pack = previous_client_pack
            mod.cfg.platforms = previous_platforms
            mod.friendly = previous_friendly
            if mod.cfg.placement is not previous_placement:
                mod.move_to_placement(previous_placement)
            self._rebuild_lookup()
            raise
        return mod

    async def update_notes(self, mod_name: str | Mod, *, notes: str | None) -> Mod:
        mod = self.get(mod_name)
        previous_notes = mod.cfg.notes
        try:
            mod.cfg.notes = notes
            await self.save_mods()
        except Exception:
            mod.cfg.notes = previous_notes
            raise
        return mod

    async def apply_discovered_launcher_metadata(
        self,
        mod_name: str | Mod,
        discovery: BulkLauncherMetadataEntry,
        *,
        apply_suggested_mod_type: bool = False,
    ) -> Mod:
        mod = self.get(mod_name)
        if discovery.mod_name != mod.name:
            raise ValueError("Bulk launcher metadata entry does not match the target mod")
        if discovery.status is not BulkLauncherMetadataStatus.EXACT:
            raise ValueError("Only exact launcher metadata matches can be applied")
        if mod.is_builtin:
            raise ValueError("Built-in mod metadata cannot be changed")
        suggested_mod_type = discovery.suggested_mod_type
        if apply_suggested_mod_type and (
            suggested_mod_type is None or suggested_mod_type is ModType.REGULAR
        ):
            raise ValueError("Only non-Regular launcher metadata type suggestions can be applied")

        previous_classification_override = mod.cfg.classification_override
        previous_mod_pages = mod.cfg.mod_pages
        previous_platforms = mod.cfg.platforms
        previous_placement = mod.cfg.placement
        previous_friendly = mod.friendly
        previous_shared_identities = mod.metadata_identities(scope=self.scope)
        previous_shared = self.shared_metadata.resolve(previous_shared_identities)
        known_page_urls = {page.url for page in previous_mod_pages}
        merged_mod_pages = (
            *previous_mod_pages,
            *(page for page in discovery.mod_pages if page.url not in known_page_urls),
        )
        merged_platforms = ModPlatformMetadata(
            modrinth=previous_platforms.modrinth or discovery.platforms.modrinth,
            curseforge=previous_platforms.curseforge or discovery.platforms.curseforge,
        )
        try:
            if apply_suggested_mod_type:
                assert suggested_mod_type is not None
                mod.cfg.classification_override = ModClassificationOverride(
                    mod_type=suggested_mod_type,
                    download_block_reason=mod.download_block_reason,
                )
            mod.cfg.mod_pages = merged_mod_pages
            mod.cfg.platforms = merged_platforms
            mod.sync_metadata()
            self._set_shared_metadata(mod)
            self._rebuild_lookup()
            await self.save_mods()
        except Exception:
            self._restore_shared_metadata(
                identities=previous_shared_identities,
                shared=previous_shared,
            )
            mod.cfg.classification_override = previous_classification_override
            mod.cfg.mod_pages = previous_mod_pages
            mod.cfg.platforms = previous_platforms
            mod.friendly = previous_friendly
            if mod.cfg.placement is not previous_placement:
                mod.move_to_placement(previous_placement)
            self._rebuild_lookup()
            raise
        return mod

    async def update_client_pack_configs(
        self,
        updates: Mapping[str, ClientPackConfig],
    ) -> tuple[Mod, ...]:
        resolved_updates: tuple[tuple[Mod, ClientPackConfig], ...] = tuple(
            (self.get(mod_name), client_pack) for mod_name, client_pack in updates.items()
        )
        if any(mod.is_builtin for mod, _client_pack in resolved_updates):
            raise ValueError("Built-in client-pack configuration cannot be changed")

        previous_configs: tuple[tuple[Mod, ClientPackConfig], ...] = tuple(
            (mod, mod.cfg.client_pack) for mod, _client_pack in resolved_updates
        )
        try:
            for mod, client_pack in resolved_updates:
                mod.cfg.client_pack = client_pack
            await self.save_mods()
        except Exception:
            for mod, client_pack in previous_configs:
                mod.cfg.client_pack = client_pack
            raise
        return tuple(mod for mod, _client_pack in resolved_updates)

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
                return [mod for mod in mods if mod.cfg.placement is ModPlacement.SERVER_ENABLED]
            case False:
                return [mod for mod in mods if mod.cfg.placement is ModPlacement.SERVER_DISABLED]

    def list_mods_json(self) -> list[Mod] | Mapping[str, str]:
        return {name: mod.cfg.model_dump_json(indent=4) for name, mod in self.index.items()}

    def list_names(self, state: bool | None = None) -> list[str]:
        return sorted((mod.name for mod in self.list_mods(state)), key=str.lower)


# AiviA APasz
