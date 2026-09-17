"""Read-only Steam Workshop inventory for Garry's Mod.

This source intentionally reports configured Workshop content without trying to
subscribe, download, alter collections, or otherwise manage Steam state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Protocol

import aiohttp

from apps._config import (
    ClientPackConfig,
    ModDownloadBlockReason,
    ModPageLink,
    ModPlacement,
    ModType,
)
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
_STEAM_WORKSHOP_DOWNLOAD_BLOCK_LABEL: Final[str] = "Steam Workshop item"


class GmodWorkshopSettings(Protocol):
    """The persisted GMod settings consumed by the read-only source."""

    @property
    def workshop_collection_id(self) -> str | None: ...

    @property
    def client_content_workshop_ids(self) -> tuple[str, ...]: ...


class _WorkshopSessionFactory(Protocol):
    def __call__(self, *, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession: ...


class _SteamWorkshopResponseError(ValueError):
    """A malformed or unavailable public Steam response."""


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
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._entries: tuple[ModInventoryEntry, ...] = ()
        self._status = ModSourceStatus.ready(self.kind, label=self.label)

    @property
    def status(self) -> ModSourceStatus:
        return self._status

    async def refresh(self) -> None:
        """Replace this source's snapshot only after a complete public lookup."""

        settings = self._settings()
        collection_id = settings.workshop_collection_id
        client_item_ids = settings.client_content_workshop_ids
        if collection_id is None and not client_item_ids:
            self._entries = ()
            self._status = ModSourceStatus.ready(self.kind, label=self.label)
            return

        try:
            roles_by_item_id: dict[str, _WorkshopRoles] = {}
            configured_collection_id = (
                None if collection_id is None else self._workshop_id(collection_id)
            )
            timeout = aiohttp.ClientTimeout(
                total=_STEAM_WORKSHOP_REQUEST_TIMEOUT_SECONDS,
                connect=_STEAM_WORKSHOP_CONNECT_TIMEOUT_SECONDS,
                sock_read=_STEAM_WORKSHOP_READ_TIMEOUT_SECONDS,
            )
            async with self._session_factory(timeout=timeout) as session:
                if configured_collection_id is not None:
                    for item_id in await self._collection_item_ids(session, configured_collection_id):
                        if item_id == configured_collection_id:
                            continue
                        self._merge_role(roles_by_item_id, item_id=item_id, server_mounted=True)
                for item_id in client_item_ids:
                    if self._workshop_id(item_id) == configured_collection_id:
                        continue
                    self._merge_role(roles_by_item_id, item_id=item_id, client_required=True)
                if not roles_by_item_id:
                    self._entries = ()
                    self._status = ModSourceStatus.ready(self.kind, label=self.label)
                    return

                metadata_by_item_id, unavailable_item_ids = await self._item_metadata(
                    session,
                    tuple(roles_by_item_id),
                )
        except _SteamWorkshopResponseError as xcp:
            raise ModSourceRefreshError(f"{self.label} unavailable.") from xcp
        except (aiohttp.ClientError, OSError, TimeoutError) as xcp:
            raise ModSourceRefreshError(f"{self.label} unavailable.") from xcp

        if not metadata_by_item_id:
            raise ModSourceRefreshError(f"{self.label} metadata is unavailable.")

        self._entries = tuple(
            self._entry_for_item(metadata=metadata_by_item_id[item_id], roles=roles)
            for item_id, roles in roles_by_item_id.items()
            if item_id in metadata_by_item_id
        )
        if unavailable_item_ids:
            item_label = "item" if len(unavailable_item_ids) == 1 else "items"
            self._status = ModSourceStatus.unavailable(
                self.kind,
                label=self.label,
                warning=(
                    f"{self.label} metadata is unavailable for "
                    f"{len(unavailable_item_ids)} configured {item_label}."
                ),
            )
        else:
            self._status = ModSourceStatus.ready(self.kind, label=self.label)

    def list_entries(self) -> tuple[ModInventoryEntry, ...]:
        return self._entries

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
            raise _SteamWorkshopResponseError("Configured collection is unavailable.")
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
            download_block_reason=ModDownloadBlockReason.OTHER,
            download_block_label=_STEAM_WORKSHOP_DOWNLOAD_BLOCK_LABEL,
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
                    name="Steam Workshop",
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
