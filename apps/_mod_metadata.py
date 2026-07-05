from __future__ import annotations

import enum
import json
import logging
import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import config
from apps._config import ModClassificationOverride, ModPageLink, normalise_optional_friendly_name

log = logging.getLogger(__name__)

_SHARED_MOD_METADATA_SCHEMA_VERSION = 1


class ModIdentityKind(enum.StrEnum):
    PROVIDER = "provider"
    NATIVE = "native"
    FILENAME = "filename"


class ModIdentity(BaseModel):
    kind: ModIdentityKind
    namespace: str = Field(min_length=1)
    value: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("namespace", "value", mode="before")
    @classmethod
    def validate_text(cls, raw: object) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Mod identity fields must be non-empty strings.")
        return raw.strip()

    @property
    def key(self) -> tuple[ModIdentityKind, str, str]:
        value = self.value.casefold() if self.kind is not ModIdentityKind.PROVIDER else self.value
        return (self.kind, self.namespace.casefold(), value)


class SharedModMetadata(BaseModel):
    identities: tuple[ModIdentity, ...]
    friendly_name: str | None = None
    classification_override: ModClassificationOverride | None = None
    mod_pages: tuple[ModPageLink, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("friendly_name", mode="before")
    @classmethod
    def validate_friendly_name(cls, raw: object) -> str | None:
        return normalise_optional_friendly_name(raw)

    @model_validator(mode="after")
    def validate_identities(self) -> SharedModMetadata:
        if not self.identities:
            raise ValueError("Shared mod metadata requires at least one identity.")
        keys = tuple(identity.key for identity in self.identities)
        if len(keys) != len(set(keys)):
            raise ValueError("Shared mod metadata identities must be unique.")
        return self

    @property
    def is_empty(self) -> bool:
        return self.friendly_name is None and self.classification_override is None and not self.mod_pages


class SharedModMetadataDocument(BaseModel):
    scope: str = Field(min_length=1)
    mods: tuple[SharedModMetadata, ...] = ()
    schema_version: int = _SHARED_MOD_METADATA_SCHEMA_VERSION

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @model_validator(mode="after")
    def validate_document(self) -> SharedModMetadataDocument:
        if self.schema_version != _SHARED_MOD_METADATA_SCHEMA_VERSION:
            raise ValueError(f"Unsupported shared mod metadata schema version: {self.schema_version}")
        seen: set[tuple[ModIdentityKind, str, str]] = set()
        for mod in self.mods:
            overlap = seen.intersection(identity.key for identity in mod.identities)
            if overlap:
                raise ValueError("Shared mod metadata entries contain overlapping identities.")
            seen.update(identity.key for identity in mod.identities)
        return self


class ScopeModMetadataStore:
    _instances: dict[Path, ScopeModMetadataStore] = {}
    _instances_lock = threading.Lock()

    def __new__(cls, *, scope: str, path: Path) -> ScopeModMetadataStore:
        resolved_path = path.resolve(strict=False)
        with cls._instances_lock:
            existing = cls._instances.get(resolved_path)
            if existing is not None:
                if existing.scope != scope:
                    raise ValueError(
                        f"Shared mod metadata path {resolved_path} is already assigned to scope {existing.scope!r}."
                    )
                return existing
            instance = super().__new__(cls)
            cls._instances[resolved_path] = instance
            return instance

    def __init__(self, *, scope: str, path: Path) -> None:
        if getattr(self, "_initialised", False):
            return
        self._initialised = True
        self.scope = scope
        self.path = path.resolve(strict=False)
        self._lock = threading.RLock()
        self._mods: list[SharedModMetadata] = []
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        document = SharedModMetadataDocument.model_validate_json(self.path.read_text(config.STR_ENCODE))
        if document.scope != self.scope:
            raise ValueError(
                f"Shared mod metadata scope mismatch at {self.path}: {document.scope!r} != {self.scope!r}."
            )
        self._mods = list(document.mods)

    @staticmethod
    def _identity_keys(identities: tuple[ModIdentity, ...]) -> frozenset[tuple[ModIdentityKind, str, str]]:
        if not identities:
            raise ValueError("At least one mod identity is required.")
        return frozenset(identity.key for identity in identities)

    def resolve(self, identities: tuple[ModIdentity, ...]) -> SharedModMetadata | None:
        keys = self._identity_keys(identities)
        with self._lock:
            matches = [
                mod
                for mod in self._mods
                if keys.intersection(identity.key for identity in mod.identities)
            ]
        if len(matches) > 1:
            raise ValueError("Mod identities resolve to multiple shared metadata entries.")
        return matches[0] if matches else None

    @staticmethod
    def _merged_identities(
        current: tuple[ModIdentity, ...],
        incoming: tuple[ModIdentity, ...],
    ) -> tuple[ModIdentity, ...]:
        identities: list[ModIdentity] = list(current)
        known_keys = {identity.key for identity in identities}
        for identity in incoming:
            if identity.key in known_keys:
                continue
            identities.append(identity)
            known_keys.add(identity.key)
        return tuple(identities)

    @staticmethod
    def _merged_pages(
        current: tuple[ModPageLink, ...],
        incoming: tuple[ModPageLink, ...],
    ) -> tuple[ModPageLink, ...]:
        pages = list(current)
        known_urls = {page.url for page in pages}
        for page in incoming:
            if page.url in known_urls:
                continue
            pages.append(page)
            known_urls.add(page.url)
        return tuple(pages)

    def migrate(
        self,
        *,
        identities: tuple[ModIdentity, ...],
        friendly_name: str | None,
        classification_override: ModClassificationOverride | None,
        mod_pages: tuple[ModPageLink, ...],
    ) -> SharedModMetadata | None:
        with self._lock:
            current = self.resolve(identities)
            if current is None:
                candidate = SharedModMetadata(
                    identities=identities,
                    friendly_name=friendly_name,
                    classification_override=classification_override,
                    mod_pages=mod_pages,
                )
                if candidate.is_empty:
                    return None
                self._mods.append(candidate)
                self._dirty = True
                return candidate

            merged = current.model_copy(
                update={
                    "identities": self._merged_identities(current.identities, identities),
                    "friendly_name": current.friendly_name or friendly_name,
                    "classification_override": current.classification_override or classification_override,
                    "mod_pages": self._merged_pages(current.mod_pages, mod_pages),
                }
            )
            if current.friendly_name is not None and friendly_name is not None and friendly_name != current.friendly_name:
                log.warning("Ignoring conflicting legacy shared friendly name for %s", identities[0].value)
            if (
                current.classification_override is not None
                and classification_override is not None
                and classification_override != current.classification_override
            ):
                log.warning("Ignoring conflicting legacy shared classification for %s", identities[0].value)
            if merged != current:
                self._mods[self._mods.index(current)] = merged
                self._dirty = True
            return merged

    def set(
        self,
        *,
        identities: tuple[ModIdentity, ...],
        friendly_name: str | None,
        classification_override: ModClassificationOverride | None,
        mod_pages: tuple[ModPageLink, ...],
    ) -> SharedModMetadata | None:
        with self._lock:
            current = self.resolve(identities)
            merged_identities = identities if current is None else self._merged_identities(current.identities, identities)
            updated = SharedModMetadata(
                identities=merged_identities,
                friendly_name=friendly_name,
                classification_override=classification_override,
                mod_pages=mod_pages,
            )
            if current is not None:
                self._mods.remove(current)
            if not updated.is_empty:
                self._mods.append(updated)
                result: SharedModMetadata | None = updated
            else:
                result = None
            if current != result:
                self._dirty = True
            return result

    def save_if_dirty(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            document = SharedModMetadataDocument(scope=self.scope, mods=tuple(self._mods))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(f".{self.path.name}.tmp")
            temp_path.write_text(
                json.dumps(document.model_dump(mode="json"), indent=2) + "\n",
                config.STR_ENCODE,
            )
            temp_path.replace(self.path)
            self._dirty = False


__all__ = (
    "ModIdentity",
    "ModIdentityKind",
    "ScopeModMetadataStore",
    "SharedModMetadata",
)
