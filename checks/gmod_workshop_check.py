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
    ModSourceRefreshError,
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
    workshop_auto_update: bool = True


@dataclass(slots=True)
class _MutableWorkshopSettings:
    workshop_collection_id: str | None
    client_content_workshop_ids: tuple[str, ...]
    workshop_auto_update: bool = True


@dataclass(slots=True)
class _MonotonicClock:
    seconds: float = 0.0

    def __call__(self) -> float:
        return self.seconds


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
        self.assertEqual(shared.mod_pages[0].name, "Workshop")
        self.assertEqual(
            shared.mod_pages[0].url,
            "https://steamcommunity.com/sharedfiles/filedetails/?id=200",
        )
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
        self.assertEqual(transport.requests[1].data["itemcount"], "2")
        self.assertEqual(transport.requests[1].data["publishedfileids[0]"], "200")
        self.assertEqual(transport.requests[1].data["publishedfileids[1]"], "100")

    async def test_collection_metadata_populates_typed_source_state_without_a_collection_row(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            if url == _COLLECTION_ENDPOINT:
                return _collection_payload("100", ("200",))
            if url == _DETAILS_ENDPOINT:
                return _details_payload(
                    _detail("200", title="Mounted addon"),
                    _detail("100", title="My server collection"),
                )
            raise AssertionError(f"Unexpected Steam endpoint: {url}")

        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings("100", ("200", "300"), workshop_auto_update=False),
            session_factory=_FakeSessionFactory(_SteamTransport(responder)),
        )

        await source.refresh()

        state = source.source_state
        self.assertEqual(state.collection_id, "100")
        self.assertEqual(state.collection_title, "My server collection")
        self.assertEqual(state.server_mounted_count, 1)
        self.assertEqual(state.client_content_ids, ("200", "300"))
        self.assertEqual(state.client_required_count, 2)
        self.assertFalse(state.auto_update)
        self.assertNotIn("100", tuple(entry.name for entry in source.list_entries()))

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

    async def test_private_metadata_uses_a_placeholder_without_losing_source_identity(self) -> None:
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
        self.assertEqual(len(entries), 3)
        self.assertEqual(tuple(entry.name for entry in entries), ("300", "200", "300"))
        self.assertEqual(
            tuple(entry.friendly for entry in entries),
            ("Duplicated title", "Workshop item 200", "Duplicated title"),
        )
        self.assertNotEqual(entries[0].reference.id, entries[1].reference.id)
        self.assertIs(entries[0].reference.source, ModSourceKind.LOCAL)
        self.assertIs(entries[1].reference.source, ModSourceKind.STEAM_WORKSHOP)
        self.assertTrue(entries[1].client_required)
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

    async def test_unresolved_workshop_metadata_becomes_a_placeholder(self) -> None:
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

        entries = catalog.list_entries()
        self.assertEqual(entries[0], local)
        self.assertEqual(entries[1].friendly, "Workshop item 200")
        self.assertTrue(entries[1].client_required)
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

    async def test_unavailable_collection_has_a_specific_source_warning(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _COLLECTION_ENDPOINT)
            return {
                "response": {
                    "collectiondetails": [
                        {
                            "publishedfileid": "100",
                            "result": 9,
                        }
                    ]
                }
            }

        workshop = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings("100", ()),
            session_factory=_FakeSessionFactory(_SteamTransport(responder)),
        )
        catalog = ModCatalog((workshop,))

        await catalog.refresh()

        self.assertEqual(catalog.list_entries(), ())
        workshop_status = catalog.source_statuses[0]
        self.assertFalse(workshop_status.healthy)
        self.assertEqual(workshop_status.warning, "Steam Workshop collection is unavailable.")

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
        workshop.invalidate()

        await catalog.refresh()

        self.assertEqual(catalog.list_entries(), expected_entries)
        workshop_status = catalog.source_statuses[1]
        self.assertFalse(workshop_status.healthy)
        self.assertTrue(workshop_status.using_cached_entries)
        self.assertEqual(workshop_status.warning, "Steam Workshop unavailable; showing cached data.")

    async def test_successful_workshop_refresh_uses_a_source_ttl(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(_detail("200", title="Cached addon"))

        clock = _MonotonicClock()
        transport = _SteamTransport(responder)
        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(transport),
            monotonic=clock,
        )

        await source.refresh()
        clock.seconds = 599.0
        await source.refresh()

        self.assertEqual([request.url for request in transport.requests], [_DETAILS_ENDPOINT])
        self.assertEqual(source.list_entries()[0].friendly, "Cached addon")

    async def test_auto_update_change_reuses_workshop_metadata_and_source_health(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(_detail("200", title="Cached addon"))

        settings = _MutableWorkshopSettings(None, ("200",), workshop_auto_update=True)
        clock = _MonotonicClock()
        transport = _SteamTransport(responder)
        source = GmodWorkshopSource(
            settings=lambda: settings,
            session_factory=_FakeSessionFactory(transport),
            monotonic=clock,
        )

        await source.refresh()
        expected_entries = source.list_entries()
        expected_status = source.status
        settings.workshop_auto_update = False
        clock.seconds = 1.0
        await source.refresh()

        self.assertEqual([request.url for request in transport.requests], [_DETAILS_ENDPOINT])
        self.assertEqual(source.list_entries(), expected_entries)
        self.assertEqual(source.status, expected_status)
        self.assertFalse(source.source_state.auto_update)
        clock.seconds = 600.0
        await source.refresh()

        self.assertEqual([request.url for request in transport.requests], [_DETAILS_ENDPOINT, _DETAILS_ENDPOINT])

    async def test_auto_update_change_preserves_unavailable_workshop_state(self) -> None:
        settings = _MutableWorkshopSettings(None, ("200",), workshop_auto_update=True)
        clock = _MonotonicClock()
        transport = _SteamTransport(lambda _url, _data: AssertionError("Steam should be unavailable"))
        transport.failure = aiohttp.ClientConnectionError("offline")
        source = GmodWorkshopSource(
            settings=lambda: settings,
            session_factory=_FakeSessionFactory(transport),
            monotonic=clock,
        )

        with self.assertRaises(ModSourceRefreshError):
            await source.refresh()
        expected_status = source.status
        settings.workshop_auto_update = False
        clock.seconds = 29.0
        with self.assertRaises(ModSourceRefreshError):
            await source.refresh()

        self.assertEqual([request.url for request in transport.requests], [_DETAILS_ENDPOINT])
        self.assertEqual(source.status, expected_status)
        self.assertFalse(source.status.healthy)
        self.assertFalse(source.source_state.auto_update)

    async def test_explicit_invalidation_bypasses_workshop_ttl(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(_detail("200", title="Cached addon"))

        clock = _MonotonicClock()
        transport = _SteamTransport(responder)
        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(transport),
            monotonic=clock,
        )

        await source.refresh()
        source.invalidate()
        await source.refresh()

        self.assertEqual([request.url for request in transport.requests], [_DETAILS_ENDPOINT, _DETAILS_ENDPOINT])

    async def test_failed_workshop_refresh_is_throttled_until_invalidated(self) -> None:
        clock = _MonotonicClock()
        transport = _SteamTransport(lambda _url, _data: AssertionError("Steam should be unavailable"))
        transport.failure = aiohttp.ClientConnectionError("offline")
        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(transport),
            monotonic=clock,
        )

        with self.assertRaises(ModSourceRefreshError):
            await source.refresh()
        clock.seconds = 29.0
        with self.assertRaises(ModSourceRefreshError):
            await source.refresh()
        source.invalidate()
        with self.assertRaises(ModSourceRefreshError):
            await source.refresh()

        self.assertEqual([request.url for request in transport.requests], [_DETAILS_ENDPOINT, _DETAILS_ENDPOINT])

    async def test_settings_change_bypasses_the_workshop_ttl(self) -> None:
        def responder(url: str, data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            item_id = data["publishedfileids[0]"]
            return _details_payload(_detail(item_id, title=f"Addon {item_id}"))

        settings = _MutableWorkshopSettings(None, ("200",))
        clock = _MonotonicClock()
        transport = _SteamTransport(responder)
        source = GmodWorkshopSource(
            settings=lambda: settings,
            session_factory=_FakeSessionFactory(transport),
            monotonic=clock,
        )

        await source.refresh()
        settings.client_content_workshop_ids = ("300",)
        await source.refresh()

        self.assertEqual(
            [request.data["publishedfileids[0]"] for request in transport.requests],
            ["200", "300"],
        )
        self.assertEqual(tuple(entry.name for entry in source.list_entries()), ("300",))

    async def test_partial_metadata_reuses_previous_item_metadata(self) -> None:
        responses = [
            _details_payload(
                _detail("200", title="Known addon", revision="10"),
                _detail("300", title="Fresh addon"),
            ),
            _details_payload(_detail("300", title="Fresh addon v2")),
        ]

        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return responses.pop(0)

        source = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200", "300")),
            session_factory=_FakeSessionFactory(_SteamTransport(responder)),
        )

        await source.refresh()
        source.invalidate()
        await source.refresh()

        entries_by_id = {entry.name: entry for entry in source.list_entries()}
        self.assertEqual(entries_by_id["200"].friendly, "Known addon")
        self.assertEqual(entries_by_id["200"].version, "Steam revision 10")
        self.assertEqual(entries_by_id["300"].friendly, "Fresh addon v2")
        self.assertFalse(source.status.healthy)
        self.assertIn("metadata is unavailable", source.status.warning or "")

    async def test_manual_refresh_failure_keeps_rows_and_sets_a_source_warning(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(_detail("200", title="Cached addon"))

        transport = _SteamTransport(responder)
        workshop = GmodWorkshopSource(
            settings=lambda: _WorkshopSettings(None, ("200",)),
            session_factory=_FakeSessionFactory(transport),
        )
        catalog = ModCatalog((_StaticLocalSource(()), workshop))
        await catalog.refresh()
        expected_entries = catalog.list_entries()
        transport.failure = aiohttp.ClientConnectionError("offline")

        await catalog.refresh_source(ModSourceKind.STEAM_WORKSHOP, invalidate=True)

        self.assertEqual(catalog.list_entries(), expected_entries)
        source_status = catalog.source_statuses[1]
        self.assertFalse(source_status.healthy)
        self.assertTrue(source_status.using_cached_entries)
        self.assertIn("showing cached data", source_status.warning or "")

    async def test_failed_configuration_change_does_not_show_previous_workshop_rows(self) -> None:
        def responder(url: str, _data: Mapping[str, str]) -> object:
            self.assertEqual(url, _DETAILS_ENDPOINT)
            return _details_payload(_detail("200", title="Previous addon"))

        settings = _MutableWorkshopSettings(None, ("200",))
        transport = _SteamTransport(responder)
        workshop = GmodWorkshopSource(
            settings=lambda: settings,
            session_factory=_FakeSessionFactory(transport),
        )
        catalog = ModCatalog((workshop,))
        await catalog.refresh()
        settings.client_content_workshop_ids = ("300",)
        catalog.invalidate_source(ModSourceKind.STEAM_WORKSHOP, discard_snapshot=True)
        transport.failure = aiohttp.ClientConnectionError("offline")

        await catalog.refresh_source(ModSourceKind.STEAM_WORKSHOP, invalidate=True)

        self.assertEqual(catalog.list_entries(), ())
        source_status = catalog.source_statuses[0]
        self.assertFalse(source_status.healthy)
        self.assertFalse(source_status.using_cached_entries)
        self.assertEqual(source_status.warning, "Steam Workshop unavailable.")
        self.assertEqual(workshop.source_state.client_content_ids, ("300",))
