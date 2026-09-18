"""Read-only Steam Workshop inventory for Garry's Mod.

This source intentionally reports configured Workshop content without trying to
subscribe, download, alter collections, or otherwise manage Steam state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import time
from typing import Final, Protocol

import aiohttp

from apps._config import ClientPackConfig, ModPageLink, ModPlacement, ModType
from apps._mod_catalog import (
    ModInventoryEntry,
    ModReference,
    ModSourceKind,
    ModSourceRefreshError,
    ModSourceRefreshPolicy,
    ModSourceStatus,
)

_STEAM_COLLECTION_DETAILS_URL: Final[str] = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
)
_STEAM_PUBLISHED_FILE_DETAILS_URL: Final[str] = (
    "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)
_STEAM_WORKSHOP_BATCH_SIZE: Final[int] = 100
_STEAM_WORKSHOP_REQUEST_TIMEOUT_SECONDS: Final[float] = 12.0
_STEAM_WORKSHOP_CONNECT_TIMEOUT_SECONDS: Final[float] = 4.0
_STEAM_WORKSHOP_READ_TIMEOUT_SECONDS: Final[float] = 8.0
_STEAM_WORKSHOP_MAX_ITEM_ID: Final[int] = (1 << 64) - 1
_STEAM_WORKSHOP_MAX_ITEM_ID_TEXT: Final[str] = str(_STEAM_WORKSHOP_MAX_ITEM_ID)
_STEAM_WORKSHOP_ORIGIN: Final[str] = "Steam Workshop"
_STEAM_WORKSHOP_METADATA_CACHE_TTL_SECONDS: Final[float] = 10 * 60
_STEAM_WORKSHOP_FAILURE_CACHE_TTL_SECONDS: Final[float] = 30.0


class GmodWorkshopSettings(Protocol):
    """The persisted GMod settings consumed by the read-only source."""

    @property
    def workshop_collection_id(self) -> str | None: ...

    @property
    def workshop_auto_update(self) -> bool: ...

    @property
    def client_content_workshop_ids(self) -> tuple[str, ...]: ...


class _WorkshopSessionFactory(Protocol):
    def __call__(self, *, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession: ...


class _SteamWorkshopResponseError(ValueError):
    """A malformed or unavailable public Steam response."""


class _SteamWorkshopCollectionUnavailableError(_SteamWorkshopResponseError):
    """The configured public Steam Workshop collection cannot be resolved."""


@dataclass(frozen=True, slots=True)
class _WorkshopRoles:
    server_mounted: bool = False
    client_required: bool = False

    def merge(
        self,
        *,
        server_mounted: bool = False,
        client_required: bool = False,
    ) -> _WorkshopRoles:
        return _WorkshopRoles(
            server_mounted=self.server_mounted or server_mounted,
            client_required=self.client_required or client_required,
        )


@dataclass(frozen=True, slots=True)
class _WorkshopItemMetadata:
    item_id: str
    title: str
    description: str | None
    added: datetime
    revision: str | None


@dataclass(frozen=True, slots=True)
class _ConfiguredWorkshopContent:
    collection_id: str | None
    client_item_ids: tuple[str, ...]
    auto_update: bool

    @property
    def cache_key(self) -> tuple[str | None, tuple[str, ...], bool]:
        return (self.collection_id, self.client_item_ids, self.auto_update)


@dataclass(frozen=True, slots=True)
class GmodWorkshopSourceState:
    """Typed GMod Workshop state exposed alongside generic source health."""

    collection_id: str | None
    collection_title: str | None
    auto_update: bool
    server_mounted_count: int
    client_content_ids: tuple[str, ...]
    client_required_count: int

    def __post_init__(self) -> None:
        if self.collection_id is not None and (not isinstance(self.collection_id, str) or not self.collection_id):
            raise ValueError("GMod Workshop collection ID must be non-empty text when set.")
        if self.collection_title is not None and (
            not isinstance(self.collection_title, str) or not self.collection_title
        ):
            raise ValueError("GMod Workshop collection title must be non-empty text when set.")
        if self.collection_id is None and self.collection_title is not None:
            raise ValueError("GMod Workshop collection title requires a collection ID.")
        if not isinstance(self.auto_update, bool):
            raise TypeError("GMod Workshop auto-update state must be a bool.")
        for value, label in (
            (self.server_mounted_count, "server-mounted count"),
            (self.client_required_count, "client-required count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"GMod Workshop {label} must be an integer.")
            if value < 0:
                raise ValueError(f"GMod Workshop {label} cannot be negative.")
        if not isinstance(self.client_content_ids, tuple):
            raise TypeError("GMod Workshop client content IDs must be a tuple.")
        if any(not isinstance(item_id, str) or not item_id for item_id in self.client_content_ids):
            raise ValueError("GMod Workshop client content IDs must be non-empty text.")
        if len(set(self.client_content_ids)) != len(self.client_content_ids):
            raise ValueError("GMod Workshop client content IDs must be unique.")
        if self.collection_id is not None and self.collection_id in self.client_content_ids:
            raise ValueError("GMod Workshop client content cannot include its configured collection ID.")
        if self.client_required_count != len(self.client_content_ids):
            raise ValueError("GMod Workshop client-required count must match explicit client content IDs.")


class GmodWorkshopSource:
    """Resolve configured GMod Workshop content through public Steam endpoints."""

    kind = ModSourceKind.STEAM_WORKSHOP
    label = ModSourceKind.STEAM_WORKSHOP.label
    refresh_policy = ModSourceRefreshPolicy.RETAIN_LAST_GOOD

    def __init__(
        self,
        *,
        settings: Callable[[], GmodWorkshopSettings],
        session_factory: _WorkshopSessionFactory = aiohttp.ClientSession,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._entries: tuple[ModInventoryEntry, ...] = ()
        self._metadata_by_item_id: dict[str, _WorkshopItemMetadata] = {}
        self._collection_titles_by_id: dict[str, str] = {}
        self._last_successful_refresh_at_seconds: float | None = None
        self._last_successful_configuration: tuple[str | None, tuple[str, ...], bool] | None = None
        self._last_failed_refresh_at_seconds: float | None = None
        self._last_failed_configuration: tuple[str | None, tuple[str, ...], bool] | None = None
        self._status = ModSourceStatus.ready(self.kind, label=self.label)

    @property
    def status(self) -> ModSourceStatus:
        return self._status

    @property
    def source_state(self) -> GmodWorkshopSourceState:
        """Return the current typed GMod-specific state without display formatting."""

        return self._source_state_for(self._configured_content(self._settings()))

    def invalidate(self) -> None:
        """Force the next refresh to bypass this source's metadata TTL."""

        self._last_successful_refresh_at_seconds = None
        self._last_failed_refresh_at_seconds = None
        self._last_failed_configuration = None

    async def refresh(self) -> None:
        """Refresh configured Workshop metadata while retaining the last good snapshot.

        Successful responses are source-cached independently of the short-lived
        aggregate mod-list cache.  This keeps ordinary Mods-page refreshes from
        turning into repeated public Steam requests. Recent failures are also
        briefly throttled; explicit invalidation always retries immediately.
        """

        configured: _ConfiguredWorkshopContent | None = None
        try:
            configured = self._configured_content(self._settings())
            if self._snapshot_is_current(configured):
                return
            if self._failure_is_current(configured):
                warning = self._status.warning or f"{self.label} unavailable."
                raise ModSourceRefreshError(warning)
            if configured.collection_id is None and not configured.client_item_ids:
                self._entries = ()
                self._record_success(configured=configured, unavailable_item_ids=frozenset())
                return

            roles_by_item_id: dict[str, _WorkshopRoles] = {}
            timeout = aiohttp.ClientTimeout(
                total=_STEAM_WORKSHOP_REQUEST_TIMEOUT_SECONDS,
                connect=_STEAM_WORKSHOP_CONNECT_TIMEOUT_SECONDS,
                sock_read=_STEAM_WORKSHOP_READ_TIMEOUT_SECONDS,
            )
            async with self._session_factory(timeout=timeout) as session:
                if configured.collection_id is not None:
                    for item_id in await self._collection_item_ids(session, configured.collection_id):
                        if item_id == configured.collection_id:
                            continue
                        self._merge_role(roles_by_item_id, item_id=item_id, server_mounted=True)
                for item_id in configured.client_item_ids:
                    if item_id == configured.collection_id:
                        continue
                    self._merge_role(roles_by_item_id, item_id=item_id, client_required=True)
                metadata_item_ids = tuple(roles_by_item_id)
                if configured.collection_id is not None:
                    # A collection is source configuration rather than a mod
                    # row, but PublishedFileDetails is where Steam exposes its
                    # display title.  Resolve it with the normal batch without
                    # treating a missing title as an inventory failure.
                    metadata_item_ids = (*metadata_item_ids, configured.collection_id)
                metadata_by_item_id, unavailable_item_ids = await self._item_metadata(
                    session,
                    metadata_item_ids,
                )
        except _SteamWorkshopResponseError as xcp:
            warning = (
                f"{self.label} collection is unavailable."
                if isinstance(xcp, _SteamWorkshopCollectionUnavailableError)
                else f"{self.label} unavailable."
            )
            self._record_failure(configured=configured, warning=warning)
            raise ModSourceRefreshError(warning) from xcp
        except (aiohttp.ClientError, OSError, TimeoutError) as xcp:
            warning = f"{self.label} unavailable."
            self._record_failure(configured=configured, warning=warning)
            raise ModSourceRefreshError(warning) from xcp

        if configured.collection_id is not None:
            collection_metadata = metadata_by_item_id.pop(configured.collection_id, None)
            if collection_metadata is not None:
                self._collection_titles_by_id[configured.collection_id] = collection_metadata.title
            unavailable_item_ids = unavailable_item_ids.difference({configured.collection_id})
        self._metadata_by_item_id.update(metadata_by_item_id)
        self._entries = tuple(
            self._entry_for_item(
                metadata=(
                    metadata_by_item_id.get(item_id)
                    or self._metadata_by_item_id.get(item_id)
                    or self._placeholder_metadata(item_id)
                ),
                roles=roles,
            )
            for item_id, roles in roles_by_item_id.items()
        )
        self._record_success(configured=configured, unavailable_item_ids=unavailable_item_ids)

    def list_entries(self) -> tuple[ModInventoryEntry, ...]:
        return self._entries

    def _configured_content(self, settings: GmodWorkshopSettings) -> _ConfiguredWorkshopContent:
        raw_collection_id = settings.workshop_collection_id
        collection_id = None if raw_collection_id is None else self._workshop_id(raw_collection_id)
        raw_client_item_ids = settings.client_content_workshop_ids
        if not isinstance(raw_client_item_ids, tuple):
            raise _SteamWorkshopResponseError("Configured client Workshop IDs are invalid.")
        client_item_ids = tuple(self._workshop_id(item_id) for item_id in raw_client_item_ids)
        if collection_id is not None:
            # Older persisted settings may have duplicated the collection in
            # resource.AddWorkshop content. Treat it as collection-only while
            # exposing a valid state and never re-emitting it in a manifest.
            client_item_ids = tuple(item_id for item_id in client_item_ids if item_id != collection_id)
        # The source predates this source-panel field. Keep existing in-process
        # adapters readable while the persisted Gmod_Settings contract remains
        # authoritative in production.
        raw_auto_update = getattr(settings, "workshop_auto_update", True)
        if not isinstance(raw_auto_update, bool):
            raise _SteamWorkshopResponseError("Configured Workshop auto-update is invalid.")
        return _ConfiguredWorkshopContent(
            collection_id=collection_id,
            client_item_ids=client_item_ids,
            auto_update=raw_auto_update,
        )

    def _snapshot_is_current(self, configured: _ConfiguredWorkshopContent) -> bool:
        refreshed_at_seconds = self._last_successful_refresh_at_seconds
        if refreshed_at_seconds is None or self._last_successful_configuration != configured.cache_key:
            return False
        elapsed_seconds = self._monotonic() - refreshed_at_seconds
        return 0.0 <= elapsed_seconds < _STEAM_WORKSHOP_METADATA_CACHE_TTL_SECONDS

    def _failure_is_current(self, configured: _ConfiguredWorkshopContent) -> bool:
        failed_at_seconds = self._last_failed_refresh_at_seconds
        if failed_at_seconds is None or self._last_failed_configuration != configured.cache_key:
            return False
        elapsed_seconds = self._monotonic() - failed_at_seconds
        return 0.0 <= elapsed_seconds < _STEAM_WORKSHOP_FAILURE_CACHE_TTL_SECONDS

    def _record_success(
        self,
        *,
        configured: _ConfiguredWorkshopContent,
        unavailable_item_ids: frozenset[str],
    ) -> None:
        self._last_successful_refresh_at_seconds = self._monotonic()
        self._last_successful_configuration = configured.cache_key
        self._last_failed_refresh_at_seconds = None
        self._last_failed_configuration = None
        if not unavailable_item_ids:
            self._status = ModSourceStatus.ready(self.kind, label=self.label)
            return
        item_label = "item" if len(unavailable_item_ids) == 1 else "items"
        self._status = ModSourceStatus.unavailable(
            self.kind,
            label=self.label,
            warning=(
                f"{self.label} metadata is unavailable for "
                f"{len(unavailable_item_ids)} configured {item_label}."
            ),
        )

    def _record_failure(
        self,
        *,
        configured: _ConfiguredWorkshopContent | None,
        warning: str,
    ) -> None:
        if configured is None:
            self._last_failed_refresh_at_seconds = None
            self._last_failed_configuration = None
        else:
            self._last_failed_refresh_at_seconds = self._monotonic()
            self._last_failed_configuration = configured.cache_key
        self._status = ModSourceStatus.unavailable(
            self.kind,
            label=self.label,
            warning=warning,
            using_cached_entries=bool(self._entries),
        )

    def _source_state_for(
        self,
        configured: _ConfiguredWorkshopContent,
    ) -> GmodWorkshopSourceState:
        return GmodWorkshopSourceState(
            collection_id=configured.collection_id,
            collection_title=(
                None
                if configured.collection_id is None
                else self._collection_titles_by_id.get(configured.collection_id)
            ),
            auto_update=configured.auto_update,
            server_mounted_count=sum(entry.server_loadable for entry in self._entries),
            client_content_ids=configured.client_item_ids,
            client_required_count=len(configured.client_item_ids),
        )

    def _placeholder_metadata(self, item_id: str) -> _WorkshopItemMetadata:
        return _WorkshopItemMetadata(
            item_id=item_id,
            title=f"Workshop item {item_id}",
            description="Steam Workshop metadata is currently unavailable.",
            added=self._now(),
            revision=None,
        )

    async def _collection_item_ids(
        self,
        session: aiohttp.ClientSession,
        collection_id: str,
    ) -> tuple[str, ...]:
        payload = await self._post_json(
            session,
            _STEAM_COLLECTION_DETAILS_URL,
            {
                "collectioncount": "1",
                "publishedfileids[0]": self._workshop_id(collection_id),
            },
        )
        response = self._response_mapping(payload)
        raw_collections = response.get("collectiondetails")
        collections = self._mapping_sequence(raw_collections, label="collection details")
        collection = next(
            (
                item
                for item in collections
                if self._optional_workshop_id(item.get("publishedfileid")) == collection_id
            ),
            None,
        )
        if collection is None or self._result_code(collection) != 1:
            raise _SteamWorkshopCollectionUnavailableError("Configured collection is unavailable.")
        raw_title = collection.get("title")
        if isinstance(raw_title, str) and (title := raw_title.strip()):
            self._collection_titles_by_id[collection_id] = title
        children = self._mapping_sequence(collection.get("children", ()), label="collection children")
        item_ids: list[str] = []
        seen_item_ids: set[str] = set()
        for child in children:
            item_id = self._optional_workshop_id(child.get("publishedfileid"))
            if item_id is None or item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            item_ids.append(item_id)
        return tuple(item_ids)

    async def _item_metadata(
        self,
        session: aiohttp.ClientSession,
        item_ids: tuple[str, ...],
    ) -> tuple[dict[str, _WorkshopItemMetadata], frozenset[str]]:
        metadata_by_item_id: dict[str, _WorkshopItemMetadata] = {}
        unavailable_item_ids: set[str] = set()
        for item_id_batch in self._batches(item_ids):
            payload = await self._post_json(
                session,
                _STEAM_PUBLISHED_FILE_DETAILS_URL,
                {
                    "itemcount": str(len(item_id_batch)),
                    **{
                        f"publishedfileids[{index}]": item_id
                        for index, item_id in enumerate(item_id_batch)
                    },
                },
            )
            response = self._response_mapping(payload)
            details = self._mapping_sequence(response.get("publishedfiledetails"), label="published file details")
            batch_metadata: dict[str, _WorkshopItemMetadata] = {}
            for detail in details:
                item_id = self._optional_workshop_id(detail.get("publishedfileid"))
                if item_id is None or item_id not in item_id_batch or item_id in batch_metadata:
                    continue
                metadata = self._metadata_from_detail(detail=detail, item_id=item_id)
                if metadata is not None:
                    batch_metadata[item_id] = metadata
            metadata_by_item_id.update(batch_metadata)
            unavailable_item_ids.update(set(item_id_batch).difference(batch_metadata))
        return metadata_by_item_id, frozenset(unavailable_item_ids)

    @staticmethod
    async def _post_json(
        session: aiohttp.ClientSession,
        url: str,
        data: Mapping[str, str],
    ) -> object:
        async with session.post(url, data=data) as response:
            response.raise_for_status()
            try:
                return await response.json(content_type=None)
            except ValueError as xcp:
                raise _SteamWorkshopResponseError("Steam response is not valid JSON.") from xcp

    def _metadata_from_detail(
        self,
        *,
        detail: Mapping[str, object],
        item_id: str,
    ) -> _WorkshopItemMetadata | None:
        if self._result_code(detail) != 1:
            return None
        raw_title = detail.get("title")
        if not isinstance(raw_title, str) or not (title := raw_title.strip()):
            return None
        raw_description = detail.get("description")
        description = raw_description.strip() if isinstance(raw_description, str) and raw_description.strip() else None
        added = self._timestamp_or_now(detail.get("time_created"), detail.get("time_updated"))
        revision = self._revision(detail.get("revision_change_number"))
        return _WorkshopItemMetadata(
            item_id=item_id,
            title=title,
            description=description,
            added=added,
            revision=revision,
        )

    def _entry_for_item(
        self,
        *,
        metadata: _WorkshopItemMetadata,
        roles: _WorkshopRoles,
    ) -> ModInventoryEntry:
        placement = ModPlacement.SERVER_ENABLED if roles.server_mounted else ModPlacement.CLIENT_ONLY
        mod_type = ModType.REGULAR if roles.server_mounted else ModType.CLIENT
        return ModInventoryEntry(
            reference=ModReference(source=self.kind, source_key=metadata.item_id),
            available_actions=frozenset(),
            name=metadata.item_id,
            friendly=metadata.title,
            enabled=placement.enabled,
            mod_type=mod_type,
            coremod=False,
            downloadable=False,
            # Workshop metadata describes configured remote content.  It does
            # not expose a local archive, but that is not a policy block.
            download_block_reason=None,
            download_block_label=None,
            origin=_STEAM_WORKSHOP_ORIGIN,
            version=metadata.revision,
            added=metadata.added,
            placement=placement,
            server_loadable=placement.server_loadable,
            client_pack_eligible=False,
            artifact=None,
            client_required=roles.client_required,
            description=metadata.description,
            mod_pages=(
                ModPageLink(
                    name="Workshop",
                    url=(
                        "https://steamcommunity.com/sharedfiles/filedetails/"
                        f"?id={metadata.item_id}"
                    ),
                ),
            ),
            # resource.AddWorkshop is intentionally represented by
            # client_required, never by the generic client-pack configuration.
            client_pack=ClientPackConfig(included_in_client=False),
        )

    @staticmethod
    def _merge_role(
        roles_by_item_id: dict[str, _WorkshopRoles],
        *,
        item_id: str,
        server_mounted: bool = False,
        client_required: bool = False,
    ) -> None:
        normalised_item_id = GmodWorkshopSource._workshop_id(item_id)
        roles_by_item_id[normalised_item_id] = roles_by_item_id.get(
            normalised_item_id,
            _WorkshopRoles(),
        ).merge(server_mounted=server_mounted, client_required=client_required)

    @staticmethod
    def _response_mapping(payload: object) -> Mapping[str, object]:
        if not isinstance(payload, Mapping):
            raise _SteamWorkshopResponseError("Steam response is not an object.")
        response = payload.get("response")
        if not isinstance(response, Mapping):
            raise _SteamWorkshopResponseError("Steam response payload is missing.")
        return response

    @staticmethod
    def _mapping_sequence(raw: object, *, label: str) -> tuple[Mapping[str, object], ...]:
        if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
            raise _SteamWorkshopResponseError(f"Steam {label} are invalid.")
        if not all(isinstance(item, Mapping) for item in raw):
            raise _SteamWorkshopResponseError(f"Steam {label} are invalid.")
        return tuple(item for item in raw if isinstance(item, Mapping))

    @staticmethod
    def _result_code(payload: Mapping[str, object]) -> int:
        result = payload.get("result")
        if isinstance(result, bool) or not isinstance(result, int):
            raise _SteamWorkshopResponseError("Steam item result is invalid.")
        return result

    @staticmethod
    def _workshop_id(raw: object) -> str:
        item_id = GmodWorkshopSource._optional_workshop_id(raw)
        if item_id is None:
            raise _SteamWorkshopResponseError("Steam Workshop item ID is invalid.")
        return item_id

    @staticmethod
    def _optional_workshop_id(raw: object) -> str | None:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            if raw <= 0 or raw > _STEAM_WORKSHOP_MAX_ITEM_ID:
                return None
            return str(raw)
        elif isinstance(raw, str):
            value = raw.strip()
        else:
            return None
        if (
            not value.isascii()
            or not value.isdecimal()
            or value.startswith("0")
            or len(value) > len(_STEAM_WORKSHOP_MAX_ITEM_ID_TEXT)
            or (
                len(value) == len(_STEAM_WORKSHOP_MAX_ITEM_ID_TEXT)
                and value > _STEAM_WORKSHOP_MAX_ITEM_ID_TEXT
            )
        ):
            return None
        return value

    def _timestamp_or_now(self, *raw_values: object) -> datetime:
        for raw_value in raw_values:
            if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
                continue
            try:
                return datetime.fromtimestamp(raw_value, timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        return self._now()

    @staticmethod
    def _revision(raw: object) -> str | None:
        revision = GmodWorkshopSource._optional_workshop_id(raw)
        return None if revision is None else f"Steam revision {revision}"

    @staticmethod
    def _batches(item_ids: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
        return tuple(
            item_ids[index : index + _STEAM_WORKSHOP_BATCH_SIZE]
            for index in range(0, len(item_ids), _STEAM_WORKSHOP_BATCH_SIZE)
        )


__all__ = (
    "GmodWorkshopSettings",
    "GmodWorkshopSource",
)
