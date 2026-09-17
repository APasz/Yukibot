from __future__ import annotations

import json
import unittest
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import aiohttp

from apps._config import (
    ClientPackConfig,
    ModPlacement,
    ModType,
)
from apps._mod_catalog import (
    ModAction,
    ModArtifact,
    ModCatalog,
    ModInventoryEntry,
    ModReference,
    ModSourceKind,
    ModSourceRefreshPolicy,
    ModSourceStatus,
)
from apps.gmod.workshop import GmodWorkshopSource

_COLLECTION_ENDPOINT = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
_DETAILS_ENDPOINT = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


@dataclass(frozen=True, slots=True)
class _WorkshopSettings:
    workshop_collection_id: str | None
    client_content_workshop_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SteamRequest:
    url: str
    data: dict[str, str]


class _FakeResponse:
    def __init__(self, payload: object, *, json_error: BaseException | None = None) -> None:
        self._payload = payload
        self._json_error = json_error

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> bool:
        del exception_type, exception, traceback
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self, *, content_type: str | None = None) -> object:
        del content_type
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _SteamTransport:
    def __init__(self, responder: Callable[[str, Mapping[str, str]], object]) -> None:
        self._responder = responder
        self.requests: list[_SteamRequest] = []
        self.failure: BaseException | None = None
        self.json_error: BaseException | None = None

    def post(self, url: str, *, data: Mapping[str, str]) -> _FakeResponse:
        self.requests.append(_SteamRequest(url=url, data=dict(data)))
        if self.failure is not None:
            raise self.failure
        return _FakeResponse(self._responder(url, data), json_error=self.json_error)


class _FakeSession:
    def __init__(self, transport: _SteamTransport) -> None:
        self._transport = transport

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> bool:
        del exception_type, exception, traceback
        return False

    def post(self, url: str, *, data: Mapping[str, str]) -> _FakeResponse:
        return self._transport.post(url, data=data)


class _FakeSessionFactory:
    def __init__(self, transport: _SteamTransport) -> None:
        self._transport = transport
        self.timeouts: list[aiohttp.ClientTimeout] = []

    def __call__(self, *, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
        self.timeouts.append(timeout)
        return cast(aiohttp.ClientSession, cast(object, _FakeSession(self._transport)))


class _StaticLocalSource:
    kind = ModSourceKind.LOCAL
    label = ModSourceKind.LOCAL.label
    refresh_policy = ModSourceRefreshPolicy.FAIL_FAST

    def __init__(self, entries: tuple[ModInventoryEntry, ...]) -> None:
        self._entries = entries

    @property
    def status(self) -> ModSourceStatus:
        return ModSourceStatus.ready(self.kind, label=self.label)

    async def refresh(self) -> None:
        return None

    def list_entries(self) -> tuple[ModInventoryEntry, ...]:
        return self._entries


def _collection_payload(collection_id: str, child_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "response": {
            "collectiondetails": [
                {
                    "publishedfileid": collection_id,
                    "result": 1,
                    "children": [{"publishedfileid": item_id} for item_id in child_ids],
                }
            ]
        }
    }


def _detail(
    item_id: str,
    *,
    title: str,
    result: int = 1,
    description: str = "",
    revision: int | str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "publishedfileid": item_id,
        "result": result,
        "title": title,
        "description": description,
        "time_created": 1_700_000_000,
    }
    if revision is not None:
        payload["revision_change_number"] = revision
    return payload


def _details_payload(*details: Mapping[str, object]) -> dict[str, object]:
    return {"response": {"publishedfiledetails": list(details)}}


class GmodWorkshopSourceTests(unittest.IsolatedAsyncioTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._temporary_directory = TemporaryDirectory[str]()
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _local_entry(self, *, name: str, friendly: str) -> ModInventoryEntry:
        artifact_path = self.root / name
        artifact_path.write_bytes(b"local mod")
        return ModInventoryEntry(
            reference=ModReference.local(name),
            available_actions=frozenset({ModAction.DOWNLOAD, ModAction.DELETE}),
            name=name,
            friendly=friendly,
            enabled=True,
            mod_type=ModType.REGULAR,
            coremod=False,
            downloadable=True,
            download_block_reason=None,
            download_block_label=None,
            origin="Local filesystem",
            version="1.0.0",
            added=datetime(2026, 1, 1, tzinfo=timezone.utc),
            placement=ModPlacement.SERVER_ENABLED,
            server_loadable=True,
            client_pack_eligible=True,
            artifact=ModArtifact(path=artifact_path, archive_name=name),
            client_pack=ClientPackConfig(),
        )

    async def test_collection_and_client_content_merge_roles_without_a_collection_entry(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            if url == _COLLECTION_ENDPOINT:
                return _collection_payload("100", ("200", "400"))
            if url == _DETAILS_ENDPOINT:
                return _details_payload(
                    _detail("200", title="Shared addon", description="A shared addon.", revision="37"),
                    _detail("400", title="Server addon"),
                    _detail("300", title="Client addon"),
                )
            raise AssertionError(f"Unexpected Steam endpoint: {url}")

        transport = _SteamTransport(responder)
        factory = _FakeSessionFactory(transport)
        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings("100", ("200", "300")),
            session_factory=factory,
        )

        await source.refresh()

        entries = source.list_entries()
        self.assertEqual(tuple(entry.reference.source_key for entry in entries), ("200", "400", "300"))
        self.assertNotIn("100", tuple(entry.name for entry in entries))
        shared, server_only, client_only = entries
        self.assertIs(shared.reference.source, ModSourceKind.STEAM_WORKSHOP)
        self.assertEqual(shared.friendly, "Shared addon")
        self.assertEqual(shared.origin, "Steam Workshop")
        self.assertEqual(shared.version, "Steam revision 37")
        self.assertTrue(shared.enabled)
        self.assertTrue(shared.server_loadable)
        self.assertTrue(shared.client_required)
        self.assertFalse(server_only.client_required)
        self.assertTrue(server_only.server_loadable)
        self.assertIsNone(client_only.version)
        self.assertEqual(client_only.placement, ModPlacement.CLIENT_ONLY)
        self.assertFalse(client_only.server_loadable)
        self.assertTrue(client_only.client_required)
        for entry in entries:
            with self.subTest(item_id=entry.reference.source_key):
                self.assertFalse(entry.downloadable)
                self.assertFalse(entry.client_pack_eligible)
                self.assertIsNone(entry.artifact)
                self.assertFalse(entry.artifact_available)
                self.assertEqual(entry.available_actions, frozenset())
                self.assertFalse(entry.client_pack.included_in_client)
        self.assertEqual([request.url for request in transport.requests], [_COLLECTION_ENDPOINT, _DETAILS_ENDPOINT])
        self.assertTrue(factory.timeouts)
        self.assertIsNotNone(factory.timeouts[0].total)
        self.assertFalse(any("key" in request.data for request in transport.requests))

    async def test_configured_collection_is_never_an_inventory_entry(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            if url == _COLLECTION_ENDPOINT:
                return _collection_payload("100", ("100", "200"))
            if url == _DETAILS_ENDPOINT:
                return _details_payload(_detail("200", title="Collection member"))
            raise AssertionError(f"Unexpected Steam endpoint: {url}")

        transport = _SteamTransport(responder)
        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings("100", ("100", "200")),
            session_factory=_FakeSessionFactory(transport),
        )

        await source.refresh()

        self.assertEqual(tuple(entry.reference.source_key for entry in source.list_entries()), ("200",))
        self.assertTrue(source.list_entries()[0].client_required)
        self.assertEqual(transport.requests[1].data["itemcount"], "1")
        self.assertEqual(transport.requests[1].data["publishedfileids[0]"], "200")

    async def test_no_configured_workshop_content_does_not_call_steam(self) -> None:
        transport = _SteamTransport(lambda _url, _data: AssertionError("Steam should not be called"))
        factory = _FakeSessionFactory(transport)
        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ()),
            session_factory=factory,
        )

        await source.refresh()

        self.assertEqual(source.list_entries(), ())
        self.assertTrue(source.status.healthy)
        self.assertEqual(factory.timeouts, [])
        self.assertEqual(transport.requests, [])

    async def test_batching_uses_at_most_one_hundred_item_ids_per_request(self) -> None:
        item_ids = tuple(str(index) for index in range(1, 102))

        def responder(url: str, data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            count = int(data["itemcount"])
            requested_ids = tuple(data[f"publishedfileids[{index}]"] for index in range(count))
            return _details_payload(*(_detail(item_id, title=f"Addon {item_id}") for item_id in requested_ids))

        transport = _SteamTransport(responder)
        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, item_ids),
            session_factory=_FakeSessionFactory(transport),
        )

        await source.refresh()

        self.assertEqual([request.data["itemcount"] for request in transport.requests], ["100", "1"])
        self.assertEqual(len(source.list_entries()), len(item_ids))

    async def test_private_or_missing_metadata_preserves_local_entries_and_source_identity(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(
                _detail("200", title="Private addon", result=9),
                _detail("300", title="Duplicated title"),
            )

        local = self._local_entry(name="300", friendly="Duplicated title")
        transport = _SteamTransport(responder)
        workshop = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200", "300")),
            session_factory=_FakeSessionFactory(transport),
        )
        catalog = ModCatalog((_StaticLocalSource((local,)), workshop))

        await catalog.refresh()

        entries = catalog.list_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(tuple(entry.name for entry in entries), ("300", "300"))
        self.assertEqual(tuple(entry.friendly for entry in entries), ("Duplicated title", "Duplicated title"))
        self.assertNotEqual(entries[0].reference.id, entries[1].reference.id)
        self.assertIs(entries[0].reference.source, ModSourceKind.LOCAL)
        self.assertIs(entries[1].reference.source, ModSourceKind.STEAM_WORKSHOP)
        workshop_status = catalog.source_statuses[1]
        self.assertFalse(workshop_status.healthy)
        self.assertFalse(workshop_status.using_cached_entries)
        self.assertIn("metadata is unavailable", workshop_status.warning or "")

    async def test_malformed_metadata_does_not_break_the_local_inventory(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return {"response": {"publishedfiledetails": "malformed"}}

        local = self._local_entry(name="local.jar", friendly="Local mod")
        workshop = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(_SteamTransport(responder)),
        )
        catalog = ModCatalog((_StaticLocalSource((local,)), workshop))

        await catalog.refresh()

        self.assertEqual(catalog.list_entries(), (local,))
        workshop_status = catalog.source_statuses[1]
        self.assertFalse(workshop_status.healthy)
        self.assertFalse(workshop_status.using_cached_entries)
        self.assertIn("Steam Workshop unavailable", workshop_status.warning or "")

    async def test_oversized_malformed_workshop_id_does_not_break_the_local_inventory(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(_detail("9" * 5_000, title="Malformed addon"))

        local = self._local_entry(name="local.jar", friendly="Local mod")
        workshop = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(_SteamTransport(responder)),
        )
        catalog = ModCatalog((_StaticLocalSource((local,)), workshop))

        await catalog.refresh()

        self.assertEqual(catalog.list_entries(), (local,))
        workshop_status = catalog.source_statuses[1]
        self.assertFalse(workshop_status.healthy)
        self.assertFalse(workshop_status.using_cached_entries)
        self.assertIn("Steam Workshop metadata is unavailable", workshop_status.warning or "")

    async def test_invalid_json_does_not_break_the_local_inventory(self) -> None:
        local = self._local_entry(name="local.jar", friendly="Local mod")
        transport = _SteamTransport(lambda _url, _data: {})
        transport.json_error = json.JSONDecodeError("Invalid JSON", "{", 1)
        workshop = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(transport),
        )
        catalog = ModCatalog((_StaticLocalSource((local,)), workshop))

        await catalog.refresh()

        self.assertEqual(catalog.list_entries(), (local,))
        workshop_status = catalog.source_statuses[1]
        self.assertFalse(workshop_status.healthy)
        self.assertFalse(workshop_status.using_cached_entries)
        self.assertIn("Steam Workshop unavailable", workshop_status.warning or "")

    async def test_transient_steam_failure_retains_the_last_good_workshop_snapshot(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(_detail("200", title="Cached addon"))

        local = self._local_entry(name="local.jar", friendly="Local mod")
        transport = _SteamTransport(responder)
        workshop = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(transport),
        )
        catalog = ModCatalog((_StaticLocalSource((local,)), workshop))

        await catalog.refresh()
        expected_entries = catalog.list_entries()
        transport.failure = aiohttp.ClientConnectionError("offline")

        await catalog.refresh()

        self.assertEqual(catalog.list_entries(), expected_entries)
        workshop_status = catalog.source_statuses[1]
        self.assertFalse(workshop_status.healthy)
        self.assertTrue(workshop_status.using_cached_entries)
        self.assertEqual(workshop_status.warning, "Steam Workshop unavailable; showing cached data.")
