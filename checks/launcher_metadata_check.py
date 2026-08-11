from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from modmux.models import Provider

import config
from apps._config import (
    BulkLauncherMetadataStatus,
    CurseForgeFileReference,
    CurseForgeModMetadata,
    LauncherMetadataCandidate,
    LauncherMetadataMatchReason,
    LauncherProviderUrls,
    ModPageCandidate,
    ModPageMatchConfidence,
    ModPageMatchReason,
    ModPageLink,
    ModPlatformMetadata,
    ModrinthModMetadata,
    ModType,
)
from apps._launcher_metadata import (
    BulkLauncherMetadataTarget,
    ModrinthSideSupport,
    _curseforge_fingerprint,
    _suggest_mod_type_from_modrinth,
    discover_mod_pages,
    discover_bulk_launcher_metadata,
    discover_launcher_metadata,
    launcher_project_page_url,
    resolve_launcher_metadata,
    resolve_launcher_metadata_resolution,
)


class LauncherMetadataTests(unittest.IsolatedAsyncioTestCase):
    def test_curseforge_fingerprint_uses_normalised_murmur2(self) -> None:
        self.assertEqual(_curseforge_fingerprint(b"abc"), 1621425345)
        self.assertEqual(_curseforge_fingerprint(b"a b\r\nc\t"), 1621425345)

    def test_derives_project_pages_from_launcher_file_urls(self) -> None:
        self.assertEqual(
            launcher_project_page_url(
                "https://modrinth.com/mod/example/version/example-version",
                Provider.MODRINTH,
            ),
            "https://modrinth.com/mod/example",
        )
        self.assertEqual(
            launcher_project_page_url(
                "https://www.curseforge.com/minecraft/mc-mods/example/files/12345?client=y",
                Provider.CURSEFORGE,
            ),
            "https://www.curseforge.com/minecraft/mc-mods/example",
        )

    def test_maps_modrinth_side_support_to_mod_types(self) -> None:
        cases = (
            (ModrinthSideSupport.REQUIRED, ModrinthSideSupport.OPTIONAL, ModType.REGULAR),
            (ModrinthSideSupport.UNSUPPORTED, ModrinthSideSupport.REQUIRED, ModType.SERVER),
            (ModrinthSideSupport.OPTIONAL, ModrinthSideSupport.UNSUPPORTED, ModType.CLIENT),
            (ModrinthSideSupport.UNKNOWN, ModrinthSideSupport.REQUIRED, None),
            (ModrinthSideSupport.UNSUPPORTED, ModrinthSideSupport.UNSUPPORTED, None),
        )
        for client, server, expected in cases:
            with self.subTest(client=client, server=server):
                self.assertIs(
                    _suggest_mod_type_from_modrinth(client=client, server=server),
                    expected,
                )

    async def test_bulk_discovery_resolves_exact_provider_files_in_batches(self) -> None:
        with TemporaryDirectory() as temp_dir:
            exact_path = Path(temp_dir) / "example.jar"
            unmatched_path = Path(temp_dir) / "unknown.jar"
            exact_path.write_bytes(b"mod-data")
            unmatched_path.write_bytes(b"unknown-data")
            exact_sha1 = "8ecde0e9b1c4d44fedbfd2c82c18f66388741396"
            exact_fingerprint = _curseforge_fingerprint(b"mod-data")
            request_paths: list[str] = []

            async def handle_request(request: httpx.Request) -> httpx.Response:
                request_paths.append(request.url.path)
                if request.url.path == "/v2/version_files":
                    request_payload = json.loads(request.content)
                    self.assertEqual(request_payload["algorithm"], "sha1")
                    self.assertEqual(len(request_payload["hashes"]), 2)
                    return httpx.Response(
                        200,
                        json={
                            exact_sha1: {
                                "id": "version-id",
                                "project_id": "project-id",
                                "version_number": "1.0.0",
                                "files": [
                                    {
                                        "url": "https://cdn.modrinth.com/example.jar",
                                        "filename": "renamed-example.jar",
                                        "size": len(b"mod-data"),
                                        "hashes": {
                                            "sha1": exact_sha1,
                                            "sha512": "4" * 128,
                                        },
                                    }
                                ],
                            }
                        },
                    )
                if request.url.path == "/v2/projects":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "id": "project-id",
                                "slug": "example",
                                "project_type": "mod",
                                "client_side": "required",
                                "server_side": "unsupported",
                            }
                        ],
                    )
                if request.url.path == "/v1/fingerprints/432":
                    request_payload = json.loads(request.content)
                    self.assertEqual(len(request_payload["fingerprints"]), 2)
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "exactMatches": [
                                    {
                                        "id": exact_fingerprint,
                                        "file": {
                                            "id": 456,
                                            "modId": 123,
                                            "fileFingerprint": exact_fingerprint,
                                        },
                                    }
                                ]
                            }
                        },
                    )
                if request.url.path == "/v1/mods":
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "id": 123,
                                    "slug": "example",
                                    "links": {
                                        "websiteUrl": (
                                            "https://www.curseforge.com/minecraft/mc-mods/example"
                                        )
                                    },
                                }
                            ]
                        },
                    )
                return httpx.Response(404, request=request)

            targets = (
                BulkLauncherMetadataTarget(
                    mod_name=exact_path.name,
                    friendly_name="Example",
                    local_path=exact_path,
                    existing_mod_pages=(),
                    existing_platforms=ModPlatformMetadata(),
                ),
                BulkLauncherMetadataTarget(
                    mod_name=unmatched_path.name,
                    friendly_name="Unknown",
                    local_path=unmatched_path,
                    existing_mod_pages=(),
                    existing_platforms=ModPlatformMetadata(),
                ),
            )
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                with patch.object(config, "env_opt", return_value="curseforge-test-key"):
                    discovery = await discover_bulk_launcher_metadata(
                        scope="minecraft",
                        targets=targets,
                        http=http,
                    )

        exact, unmatched = discovery.entries
        self.assertIs(exact.status, BulkLauncherMetadataStatus.EXACT)
        self.assertEqual(exact.matched_providers, (Provider.MODRINTH, Provider.CURSEFORGE))
        self.assertEqual(
            tuple(page.url for page in exact.mod_pages),
            (
                "https://modrinth.com/mod/example",
                "https://www.curseforge.com/minecraft/mc-mods/example",
            ),
        )
        self.assertIs(exact.suggested_mod_type, ModType.CLIENT)
        assert exact.platforms.modrinth is not None
        self.assertEqual(
            exact.platforms.modrinth.page_url,
            "https://modrinth.com/mod/example/version/version-id",
        )
        assert exact.platforms.curseforge is not None
        self.assertEqual(exact.platforms.curseforge.file_id, 456)
        self.assertIs(unmatched.status, BulkLauncherMetadataStatus.UNMATCHED)
        self.assertEqual(discovery.provider_errors, ())
        self.assertEqual(request_paths.count("/v2/version_files"), 1)
        self.assertEqual(request_paths.count("/v1/fingerprints/432"), 1)

    async def test_bulk_discovery_derives_missing_project_pages_from_saved_file_metadata(self) -> None:
        target = BulkLauncherMetadataTarget(
            mod_name="example.jar",
            friendly_name="Example",
            local_path=Path("/path/does/not/need/to/exist.jar"),
            existing_mod_pages=(),
            existing_platforms=ModPlatformMetadata(
                modrinth=ModrinthModMetadata(
                    page_url="https://modrinth.com/mod/example/version/version-id",
                    project_id="project-id",
                    version_id="version-id",
                    download_url="https://cdn.modrinth.com/example.jar",
                ),
                curseforge=CurseForgeModMetadata(
                    page_url=(
                        "https://www.curseforge.com/minecraft/mc-mods/example/files/456"
                    ),
                    project_id=123,
                    file_id=456,
                ),
            ),
        )

        discovery = await discover_bulk_launcher_metadata(
            scope="minecraft",
            targets=(target,),
        )

        entry = discovery.entries[0]
        self.assertIs(entry.status, BulkLauncherMetadataStatus.EXACT)
        self.assertEqual(
            tuple(page.url for page in entry.mod_pages),
            (
                "https://modrinth.com/mod/example",
                "https://www.curseforge.com/minecraft/mc-mods/example",
            ),
        )

    async def test_bulk_discovery_retains_other_provider_when_one_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")
            target = BulkLauncherMetadataTarget(
                mod_name=local_path.name,
                friendly_name="Example",
                local_path=local_path,
                existing_mod_pages=(),
                existing_platforms=ModPlatformMetadata(),
            )
            modrinth_lookup = AsyncMock(return_value={})
            curseforge_lookup = AsyncMock(side_effect=httpx.ConnectError("offline"))

            with (
                patch(
                    "apps._launcher_metadata._bulk_modrinth_exact_matches",
                    new=modrinth_lookup,
                ),
                patch(
                    "apps._launcher_metadata._bulk_curseforge_exact_matches",
                    new=curseforge_lookup,
                ),
            ):
                discovery = await discover_bulk_launcher_metadata(
                    scope="minecraft",
                    targets=(target,),
                )

        modrinth_lookup.assert_awaited_once()
        curseforge_lookup.assert_awaited_once()
        self.assertEqual(len(discovery.provider_errors), 1)
        self.assertIs(discovery.provider_errors[0].provider, Provider.CURSEFORGE)

    async def test_discovers_and_ranks_modrinth_file_pages_from_project_page(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")

            async def handle_request(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/v2/project/example/version")
                self.assertEqual(request.url.params["include_changelog"], "false")
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "exact-version",
                            "version_number": "2.0.0",
                            "version_type": "release",
                            "game_versions": ["1.20.1"],
                            "loaders": ["forge"],
                            "files": [
                                {
                                    "filename": "renamed-example.jar",
                                    "size": len(b"mod-data"),
                                    "hashes": {
                                        "sha1": "8ecde0e9b1c4d44fedbfd2c82c18f66388741396"
                                    },
                                }
                            ],
                        },
                        {
                            "id": "filename-version",
                            "version_number": "1.0.0",
                            "version_type": "beta",
                            "game_versions": ["1.20.1"],
                            "loaders": ["forge"],
                            "files": [
                                {
                                    "filename": "example.jar",
                                    "size": 999,
                                    "hashes": {"sha1": "1" * 40},
                                }
                            ],
                        },
                    ],
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                discovery = await discover_launcher_metadata(
                    scope="minecraft",
                    mod_pages=(
                        ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
                    ),
                    existing_urls=LauncherProviderUrls(),
                    local_path=local_path,
                    game_version="1.20.1",
                    loader="forge",
                    http=http,
                )

        self.assertEqual(len(discovery.providers), 1)
        candidates = discovery.providers[0].candidates
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            candidates[0].file_page_url,
            "https://modrinth.com/mod/example/version/exact-version",
        )
        self.assertEqual(candidates[0].match_reasons, (LauncherMetadataMatchReason.SHA1,))
        self.assertEqual(candidates[1].match_reasons, (LauncherMetadataMatchReason.FILENAME,))

    async def test_file_page_discovery_completes_other_provider_after_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")
            modrinth_candidate = LauncherMetadataCandidate(
                provider=Provider.MODRINTH,
                project_page_url="https://modrinth.com/mod/example",
                file_page_url="https://modrinth.com/mod/example/version/version-id",
                version="1.0.0",
                filename=local_path.name,
                match_reasons=(LauncherMetadataMatchReason.FILENAME,),
            )
            modrinth_lookup = AsyncMock(return_value=(modrinth_candidate,))
            curseforge_lookup = AsyncMock(side_effect=httpx.ConnectError("offline"))

            with (
                patch(
                    "apps._launcher_metadata._discover_modrinth_candidates",
                    new=modrinth_lookup,
                ),
                patch(
                    "apps._launcher_metadata._discover_curseforge_candidates",
                    new=curseforge_lookup,
                ),
            ):
                discovery = await discover_launcher_metadata(
                    scope="minecraft",
                    mod_pages=(
                        ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
                        ModPageLink(
                            name="CurseForge",
                            url="https://www.curseforge.com/minecraft/mc-mods/example",
                        ),
                    ),
                    existing_urls=LauncherProviderUrls(),
                    local_path=local_path,
                )

        modrinth_lookup.assert_awaited_once()
        curseforge_lookup.assert_awaited_once()
        results = {result.provider: result for result in discovery.providers}
        self.assertEqual(results[Provider.MODRINTH].candidates, (modrinth_candidate,))
        self.assertEqual(results[Provider.CURSEFORGE].error, "offline")

    async def test_file_page_discovery_can_limit_to_modrinth(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")
            modrinth_lookup = AsyncMock(return_value=())
            curseforge_lookup = AsyncMock(side_effect=AssertionError("must not be called"))
            with (
                patch(
                    "apps._launcher_metadata._discover_modrinth_candidates",
                    new=modrinth_lookup,
                ),
                patch(
                    "apps._launcher_metadata._discover_curseforge_candidates",
                    new=curseforge_lookup,
                ),
            ):
                discovery = await discover_launcher_metadata(
                    scope="minecraft",
                    mod_pages=(
                        ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
                        ModPageLink(
                            name="CurseForge",
                            url="https://minecraft.curseforge.com/projects/example",
                        ),
                    ),
                    existing_urls=LauncherProviderUrls(),
                    local_path=local_path,
                    providers=(Provider.MODRINTH,),
                )

        modrinth_lookup.assert_awaited_once()
        curseforge_lookup.assert_not_awaited()
        self.assertEqual(
            tuple(result.provider for result in discovery.providers),
            (Provider.MODRINTH,),
        )

    async def test_mouse_tweaks_resolution_uses_logical_client_mod_filename(self) -> None:
        logical_filename = "MouseTweaks-forge-mc1.20.1-2.25.1.jar"
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / f"{logical_filename}.client"
            local_path.write_bytes(b"locally-repacked-mouse-tweaks")

            async def handle_request(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/v2/project/mouse-tweaks/version")
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "7JVXOe3K",
                            "project_id": "aC3cM3Vq",
                            "version_number": "1.20.1-2.25.1-forge",
                            "version_type": "release",
                            "game_versions": ["1.20.1"],
                            "loaders": ["forge"],
                            "files": [
                                {
                                    "filename": logical_filename,
                                    "size": 76237,
                                    "hashes": {"sha1": "d751153e722a4e014691c83f39f5b07c6ec5333c"},
                                }
                            ],
                        }
                    ],
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                with patch.object(config, "env_opt", return_value=None):
                    discovery = await discover_launcher_metadata(
                        scope="minecraft",
                        mod_pages=(
                            ModPageLink(
                                name="Modrinth",
                                url=(
                                    "https://modrinth.com/mod/mouse-tweaks/version/"
                                    "1.20.1-2.25.1-forge"
                                ),
                            ),
                            ModPageLink(
                                name="CurseForge",
                                url="https://minecraft.curseforge.com/projects/mouse-tweaks",
                            ),
                        ),
                        existing_urls=LauncherProviderUrls(),
                        local_path=local_path,
                        local_filename=logical_filename,
                        game_version="1.20.1",
                        loader="forge",
                        http=http,
                    )

        results = {result.provider: result for result in discovery.providers}
        self.assertEqual(len(results[Provider.MODRINTH].candidates), 1)
        candidate = results[Provider.MODRINTH].candidates[0]
        self.assertEqual(
            candidate.file_page_url,
            "https://modrinth.com/mod/mouse-tweaks/version/7JVXOe3K",
        )
        self.assertEqual(
            candidate.match_reasons,
            (
                LauncherMetadataMatchReason.EXPLICIT_FILE_PAGE,
                LauncherMetadataMatchReason.FILENAME,
            ),
        )
        self.assertIn("not a valid CurseForge", results[Provider.CURSEFORGE].error or "")

    async def test_discovers_modrinth_project_from_exact_local_hash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")

            async def handle_request(request: httpx.Request) -> httpx.Response:
                if request.url.path.startswith("/v2/version_file/"):
                    self.assertEqual(request.url.params["algorithm"], "sha1")
                    return httpx.Response(200, json={"project_id": "project-id"})
                if request.url.path == "/v2/project/project-id":
                    return httpx.Response(
                        200,
                        json={
                            "slug": "example-mod",
                            "title": "Example Mod",
                            "description": "Example description",
                            "project_type": "mod",
                            "game_versions": ["1.20.1"],
                            "loaders": ["forge"],
                        },
                    )
                return httpx.Response(404, request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                discovery = await discover_mod_pages(
                    scope="minecraft",
                    existing_mod_pages=(
                        ModPageLink(
                            name="CurseForge",
                            url="https://www.curseforge.com/minecraft/mc-mods/example",
                        ),
                    ),
                    local_path=local_path,
                    friendly_name="Example Mod",
                    detected_version="2.0.0",
                    game_version="1.20.1",
                    loader="forge",
                    http=http,
                )

        candidate = discovery.providers[0].candidates[0]
        self.assertEqual(candidate.page.url, "https://modrinth.com/mod/example-mod")
        self.assertIs(candidate.confidence, ModPageMatchConfidence.EXACT)
        self.assertEqual(candidate.match_reasons, (ModPageMatchReason.FILE_HASH,))

    async def test_searches_modrinth_when_local_hash_is_unknown(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example-mod-2.0.0-forge.jar"
            local_path.write_bytes(b"locally-repacked")
            search_queries: list[str] = []

            async def handle_request(request: httpx.Request) -> httpx.Response:
                if request.url.path.startswith("/v2/version_file/"):
                    return httpx.Response(404, request=request)
                if request.url.path == "/v2/search":
                    search_query = request.url.params["query"]
                    search_queries.append(search_query)
                    if search_query == "Unhelpful Display Name":
                        return httpx.Response(200, json={"hits": []})
                    self.assertEqual(search_query, "example mod")
                    return httpx.Response(
                        200,
                        json={
                            "hits": [
                                {
                                    "project_id": "project-id",
                                    "slug": "example-mod",
                                    "title": "Example Mod",
                                    "description": "Example description",
                                    "project_type": "mod",
                                    "author": "author",
                                    "versions": ["1.20.1"],
                                    "categories": ["forge"],
                                }
                            ]
                        },
                    )
                return httpx.Response(404, request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                discovery = await discover_mod_pages(
                    scope="minecraft",
                    existing_mod_pages=(
                        ModPageLink(
                            name="CurseForge",
                            url="https://www.curseforge.com/minecraft/mc-mods/example",
                        ),
                    ),
                    local_path=local_path,
                    friendly_name="Unhelpful Display Name",
                    detected_version="2.0.0",
                    game_version="1.20.1",
                    loader="forge",
                    http=http,
                )

        self.assertEqual(search_queries, ["Unhelpful Display Name", "example mod"])
        candidate = discovery.providers[0].candidates[0]
        self.assertIs(candidate.confidence, ModPageMatchConfidence.STRONG)
        self.assertEqual(
            candidate.match_reasons,
            (
                ModPageMatchReason.NAME,
                ModPageMatchReason.GAME_VERSION,
                ModPageMatchReason.LOADER,
            ),
        )

    async def test_mod_page_discovery_completes_other_provider_after_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")
            modrinth_candidate = ModPageCandidate(
                provider=Provider.MODRINTH,
                page=ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
                project_id="project-id",
                title="Example",
                confidence=ModPageMatchConfidence.EXACT,
                match_reasons=(ModPageMatchReason.FILE_HASH,),
            )
            modrinth_lookup = AsyncMock(return_value=modrinth_candidate)
            curseforge_lookup = AsyncMock(side_effect=httpx.ConnectError("offline"))

            with (
                patch.object(config, "env_opt", return_value="curseforge-test-key"),
                patch(
                    "apps._launcher_metadata._modrinth_exact_project_candidate",
                    new=modrinth_lookup,
                ),
                patch(
                    "apps._launcher_metadata._curseforge_exact_project_candidates",
                    new=curseforge_lookup,
                ),
            ):
                discovery = await discover_mod_pages(
                    scope="minecraft",
                    existing_mod_pages=(),
                    local_path=local_path,
                    friendly_name="Example",
                )

        modrinth_lookup.assert_awaited_once()
        curseforge_lookup.assert_awaited_once()
        results = {result.provider: result for result in discovery.providers}
        self.assertEqual(results[Provider.MODRINTH].candidates, (modrinth_candidate,))
        self.assertEqual(results[Provider.CURSEFORGE].error, "offline")

    async def test_mod_page_discovery_can_limit_to_modrinth(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")
            modrinth_lookup = AsyncMock(return_value=None)
            modrinth_search = AsyncMock(return_value=())
            curseforge_lookup = AsyncMock(side_effect=AssertionError("must not be called"))
            with (
                patch(
                    "apps._launcher_metadata._modrinth_exact_project_candidate",
                    new=modrinth_lookup,
                ),
                patch(
                    "apps._launcher_metadata._search_modrinth_projects",
                    new=modrinth_search,
                ),
                patch(
                    "apps._launcher_metadata._curseforge_exact_project_candidates",
                    new=curseforge_lookup,
                ),
            ):
                discovery = await discover_mod_pages(
                    scope="minecraft",
                    existing_mod_pages=(),
                    local_path=local_path,
                    friendly_name="Example",
                    providers=(Provider.MODRINTH,),
                )

        modrinth_lookup.assert_awaited_once()
        modrinth_search.assert_awaited_once()
        curseforge_lookup.assert_not_awaited()
        self.assertEqual(
            tuple(result.provider for result in discovery.providers),
            (Provider.MODRINTH,),
        )

    async def test_modrinth_search_simplifies_versioned_filename_after_approximate_results(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "bettervillage-forge-1.20.1-3.2.0.jar"
            local_path.write_bytes(b"locally-repacked")
            search_queries: list[str] = []

            async def handle_request(request: httpx.Request) -> httpx.Response:
                if request.url.path.startswith("/v2/version_file/"):
                    return httpx.Response(404, request=request)
                if request.url.path != "/v2/search":
                    return httpx.Response(404, request=request)
                search_query = request.url.params["query"]
                search_queries.append(search_query)
                if search_query == "Better village 3.2.0":
                    return httpx.Response(
                        200,
                        json={
                            "hits": [
                                {
                                    "project_id": "unrelated-id",
                                    "slug": "village-improvements",
                                    "title": "Village Improvements",
                                    "project_type": "mod",
                                    "versions": ["1.20.1"],
                                    "categories": ["forge"],
                                }
                            ]
                        },
                    )
                self.assertEqual(search_query, "bettervillage")
                return httpx.Response(
                    200,
                    json={
                        "hits": [
                            {
                                "project_id": "dGVX5JbJ",
                                "slug": "better-village",
                                "title": "Better Villages",
                                "project_type": "mod",
                                "versions": ["1.20.1"],
                                "categories": ["forge"],
                            }
                        ]
                    },
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                discovery = await discover_mod_pages(
                    scope="minecraft",
                    existing_mod_pages=(
                        ModPageLink(
                            name="CurseForge",
                            url="https://www.curseforge.com/minecraft/mc-mods/better-village",
                        ),
                    ),
                    local_path=local_path,
                    friendly_name="Better village 3.2.0",
                    game_version="1.20.1",
                    loader="forge",
                    http=http,
                )

        self.assertEqual(search_queries, ["Better village 3.2.0", "bettervillage"])
        candidate = discovery.providers[0].candidates[0]
        self.assertEqual(candidate.page.url, "https://modrinth.com/mod/better-village")
        self.assertIs(candidate.confidence, ModPageMatchConfidence.STRONG)

    async def test_discovers_curseforge_project_from_exact_fingerprint(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")

            async def handle_request(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/v1/fingerprints":
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "exactMatches": [
                                    {"file": {"modId": 123}}
                                ]
                            }
                        },
                    )
                if request.url.path == "/v1/mods/123":
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "id": 123,
                                "name": "Example Mod",
                                "slug": "example-mod",
                                "summary": "Example description",
                                "links": {
                                    "websiteUrl": (
                                        "https://www.curseforge.com/minecraft/mc-mods/example-mod"
                                    )
                                },
                                "authors": [{"name": "author"}],
                                "latestFilesIndexes": [{"gameVersion": "1.20.1"}],
                                "categories": [{"name": "Forge"}],
                            }
                        },
                    )
                return httpx.Response(404, request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                with patch.object(config, "env_opt", return_value="curseforge-test-key"):
                    discovery = await discover_mod_pages(
                        scope="minecraft",
                        existing_mod_pages=(
                            ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
                        ),
                        local_path=local_path,
                        friendly_name="Example Mod",
                        game_version="1.20.1",
                        loader="forge",
                        http=http,
                    )

        candidate = discovery.providers[0].candidates[0]
        self.assertEqual(
            candidate.page.url,
            "https://www.curseforge.com/minecraft/mc-mods/example-mod",
        )
        self.assertIs(candidate.confidence, ModPageMatchConfidence.EXACT)
        self.assertEqual(candidate.match_reasons, (ModPageMatchReason.FILE_FINGERPRINT,))

    async def test_modrinth_resolution_uses_local_hash_for_renamed_remote_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")

            async def handle_request(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/v2/project/example":
                    return httpx.Response(
                        200,
                        json={"client_side": "required", "server_side": "required"},
                    )
                if request.url.path == "/v2/project/example/version":
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "id": "version-id",
                                "project_id": "project-id",
                                "version_number": "2.0.0",
                                "files": [
                                    {
                                        "filename": "primary.jar",
                                        "primary": True,
                                        "url": "https://cdn.modrinth.com/primary.jar",
                                        "hashes": {"sha1": "1" * 40, "sha512": "2" * 128},
                                        "size": 999,
                                    },
                                    {
                                        "filename": "renamed-example.jar",
                                        "primary": False,
                                        "url": "https://cdn.modrinth.com/renamed-example.jar",
                                        "hashes": {
                                            "sha1": "8ecde0e9b1c4d44fedbfd2c82c18f66388741396",
                                            "sha512": "3" * 128,
                                        },
                                        "size": len(b"mod-data"),
                                    },
                                ],
                            }
                        ],
                    )
                return httpx.Response(404, request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                metadata = await resolve_launcher_metadata(
                    scope="minecraft",
                    urls=LauncherProviderUrls(
                        modrinth="https://modrinth.com/mod/example/version/version-id"
                    ),
                    local_filename=local_path.name,
                    local_path=local_path,
                    http=http,
                )

        assert metadata.modrinth is not None
        self.assertEqual(metadata.modrinth.filename, "renamed-example.jar")

    async def test_discovery_preserves_providers_with_existing_file_pages(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")
            with self.assertRaisesRegex(ValueError, "No unresolved"):
                await discover_launcher_metadata(
                    scope="minecraft",
                    mod_pages=(
                        ModPageLink(name="Modrinth", url="https://modrinth.com/mod/example"),
                    ),
                    existing_urls=LauncherProviderUrls(
                        modrinth="https://modrinth.com/mod/example/version/current"
                    ),
                    local_path=local_path,
                )

    async def test_discovers_curseforge_file_page_from_project_page(self) -> None:
        with TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "example.jar"
            local_path.write_bytes(b"mod-data")

            async def handle_request(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/v1/games":
                    return httpx.Response(
                        200,
                        json={
                            "data": [{"id": 432, "slug": "minecraft"}],
                            "pagination": {"resultCount": 1, "totalCount": 1},
                        },
                    )
                if request.url.path == "/v1/mods/search":
                    return httpx.Response(200, json={"data": [{"id": 123}]})
                if request.url.path == "/v1/mods/123":
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "id": 123,
                                "gameId": 432,
                                "name": "Example",
                                "slug": "example",
                                "authors": [],
                                "latestFiles": [],
                            }
                        },
                    )
                if request.url.path == "/v1/mods/123/files":
                    self.assertEqual(request.url.params["pageSize"], "50")
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "id": 456,
                                    "displayName": "Example 2.0.0",
                                    "fileName": "example.jar",
                                    "fileLength": len(b"mod-data"),
                                    "releaseType": 1,
                                    "gameVersions": ["1.20.1", "Forge"],
                                    "hashes": [
                                        {
                                            "algo": 1,
                                            "value": "8ecde0e9b1c4d44fedbfd2c82c18f66388741396",
                                        }
                                    ],
                                }
                            ],
                            "pagination": {
                                "resultCount": 1,
                                "totalCount": 1,
                            },
                        },
                    )
                return httpx.Response(404, request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
                with patch.object(config, "env_opt", return_value="curseforge-test-key"):
                    discovery = await discover_launcher_metadata(
                        scope="minecraft",
                        mod_pages=(
                            ModPageLink(
                                name="CurseForge",
                                url="https://www.curseforge.com/minecraft/mc-mods/example",
                            ),
                        ),
                        existing_urls=LauncherProviderUrls(),
                        local_path=local_path,
                        game_version="1.20.1",
                        loader="forge",
                        http=http,
                    )

        candidates = discovery.providers[0].candidates
        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].file_page_url,
            "https://www.curseforge.com/minecraft/mc-mods/example/files/456",
        )
        self.assertEqual(
            candidates[0].match_reasons,
            (
                LauncherMetadataMatchReason.SHA1,
                LauncherMetadataMatchReason.FILENAME_AND_SIZE,
            ),
        )

    async def test_resolves_and_preserves_minecraft_provider_file_pages(self) -> None:
        modrinth_page = "https://modrinth.com/plugin/journeymap/version/1.20.1-5.10.3-forge"
        curseforge_page = "https://www.curseforge.com/minecraft/mc-mods/journeymap/files/5789363"

        def env_value(key: str) -> str | None:
            return "curseforge-test-key" if key == "CURSEFORGE_API_KEY" else None

        async def handle_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/project/journeymap":
                return httpx.Response(
                    200,
                    json={"client_side": "required", "server_side": "optional"},
                )
            if request.url.path == "/v2/project/journeymap/version":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "journeymap-version-id",
                            "project_id": "journeymap-project-id",
                            "version_number": "1.20.1-5.10.3-forge",
                            "files": [
                                {
                                    "filename": "different-loader.jar",
                                    "primary": True,
                                    "url": "https://cdn.modrinth.com/different-loader.jar",
                                    "hashes": {"sha1": "1" * 40, "sha512": "2" * 128},
                                    "size": 123,
                                },
                                {
                                    "filename": "journeymap.jar",
                                    "primary": False,
                                    "url": "https://cdn.modrinth.com/journeymap.jar",
                                    "hashes": {"sha1": "3" * 40, "sha512": "4" * 128},
                                    "size": 456,
                                },
                            ],
                        }
                    ],
                )
            if request.url.host == "api.curseforge.com" and request.url.path == "/v1/games":
                self.assertEqual(request.headers["x-api-key"], "curseforge-test-key")
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": 432, "slug": "minecraft"}],
                        "pagination": {"resultCount": 1, "totalCount": 1},
                    },
                )
            if request.url.host == "api.curseforge.com" and request.url.path == "/v1/mods/search":
                return httpx.Response(200, json={"data": [{"id": 32274}]})
            if (
                request.url.host == "api.curseforge.com"
                and request.url.path == "/v1/mods/32274/files/5789363"
            ):
                return httpx.Response(200, json={"data": {"modId": 32274, "id": 5789363}})
            if request.url.host == "api.curseforge.com" and request.url.path == "/v1/mods/32274":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": 32274,
                            "gameId": 432,
                            "name": "JourneyMap",
                            "slug": "journeymap",
                            "authors": [],
                            "latestFiles": [],
                        }
                    },
                )
            return httpx.Response(404, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            with patch.object(
                config,
                "env_opt",
                side_effect=env_value,
            ):
                metadata = await resolve_launcher_metadata(
                    scope="minecraft",
                    urls=LauncherProviderUrls(modrinth=modrinth_page, curseforge=curseforge_page),
                    local_filename="journeymap.jar",
                    http=http,
                )

        self.assertIsNotNone(metadata.modrinth)
        assert metadata.modrinth is not None
        self.assertEqual(metadata.modrinth.page_url, modrinth_page)
        self.assertEqual(metadata.modrinth.project_id, "journeymap-project-id")
        self.assertEqual(metadata.modrinth.version_id, "journeymap-version-id")
        self.assertEqual(metadata.modrinth.download_url, "https://cdn.modrinth.com/journeymap.jar")
        self.assertEqual(metadata.modrinth.filename, "journeymap.jar")
        self.assertEqual(metadata.modrinth.sha1, "3" * 40)
        self.assertEqual(metadata.modrinth.sha512, "4" * 128)
        self.assertEqual(metadata.modrinth.size, 456)
        self.assertIsNotNone(metadata.curseforge)
        assert metadata.curseforge is not None
        self.assertEqual(metadata.curseforge.page_url, curseforge_page)
        self.assertEqual(metadata.curseforge.project_id, 32274)
        self.assertEqual(metadata.curseforge.file_id, 5789363)
        self.assertTrue(metadata.curseforge.verified)

        persisted = ModPlatformMetadata.model_validate(metadata.model_dump(mode="json"))
        assert persisted.modrinth is not None
        assert persisted.curseforge is not None
        self.assertEqual(persisted.modrinth.page_url, modrinth_page)
        self.assertEqual(persisted.curseforge.page_url, curseforge_page)

    async def test_file_page_resolution_retains_each_successful_provider(self) -> None:
        modrinth_page = "https://modrinth.com/mod/example/version/version-id"
        curseforge_page = (
            "https://www.curseforge.com/minecraft/mc-mods/example/files/456"
        )
        modrinth_metadata = ModrinthModMetadata(
            page_url=modrinth_page,
            project_id="project-id",
            version_id="version-id",
            download_url="https://cdn.modrinth.com/example.jar",
        )
        curseforge_metadata = CurseForgeModMetadata(
            page_url=curseforge_page,
            project_id=123,
            file_id=456,
        )

        for failed_provider in (Provider.MODRINTH, Provider.CURSEFORGE):
            with self.subTest(failed_provider=failed_provider):
                modrinth_lookup = (
                    AsyncMock(side_effect=httpx.ConnectError("modrinth offline"))
                    if failed_provider is Provider.MODRINTH
                    else AsyncMock(return_value=(modrinth_metadata, ModType.CLIENT))
                )
                curseforge_lookup = (
                    AsyncMock(side_effect=httpx.ConnectError("curseforge offline"))
                    if failed_provider is Provider.CURSEFORGE
                    else AsyncMock(return_value=curseforge_metadata)
                )
                with (
                    patch("apps._launcher_metadata._resolve_modrinth", new=modrinth_lookup),
                    patch(
                        "apps._launcher_metadata._resolve_curseforge_source",
                        new=curseforge_lookup,
                    ),
                ):
                    resolution = await resolve_launcher_metadata_resolution(
                        scope="minecraft",
                        urls=LauncherProviderUrls(
                            modrinth=modrinth_page,
                            curseforge=curseforge_page,
                        ),
                        local_filename="example.jar",
                    )

                modrinth_lookup.assert_awaited_once()
                curseforge_lookup.assert_awaited_once()
                self.assertEqual(len(resolution.provider_errors), 1)
                self.assertIs(resolution.provider_errors[0].provider, failed_provider)
                if failed_provider is Provider.MODRINTH:
                    self.assertIsNone(resolution.platforms.modrinth)
                    self.assertEqual(resolution.platforms.curseforge, curseforge_metadata)
                    self.assertIsNone(resolution.suggested_mod_type)
                else:
                    self.assertEqual(resolution.platforms.modrinth, modrinth_metadata)
                    self.assertIsNone(resolution.platforms.curseforge)
                    self.assertIs(resolution.suggested_mod_type, ModType.CLIENT)

    async def test_file_page_resolution_can_limit_to_modrinth(self) -> None:
        modrinth_page = "https://modrinth.com/mod/example/version/version-id"
        modrinth_metadata = ModrinthModMetadata(
            page_url=modrinth_page,
            project_id="project-id",
            version_id="version-id",
            download_url="https://cdn.modrinth.com/example.jar",
        )
        modrinth_lookup = AsyncMock(return_value=(modrinth_metadata, ModType.CLIENT))
        curseforge_lookup = AsyncMock(side_effect=AssertionError("must not be called"))
        with (
            patch("apps._launcher_metadata._resolve_modrinth", new=modrinth_lookup),
            patch(
                "apps._launcher_metadata._resolve_curseforge_source",
                new=curseforge_lookup,
            ),
        ):
            resolution = await resolve_launcher_metadata_resolution(
                scope="minecraft",
                urls=LauncherProviderUrls(
                    modrinth=modrinth_page,
                    curseforge=(
                        "https://www.curseforge.com/minecraft/mc-mods/example/files/456"
                    ),
                ),
                local_filename="example.jar",
                providers=(Provider.MODRINTH,),
            )

        modrinth_lookup.assert_awaited_once()
        curseforge_lookup.assert_not_awaited()
        self.assertEqual(resolution.platforms.modrinth, modrinth_metadata)
        self.assertIsNone(resolution.platforms.curseforge)

    async def test_modrinth_project_suggests_client_only_mod_type(self) -> None:
        page_url = "https://modrinth.com/mod/client-mod/version/client-version"

        async def handle_request(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/project/client-mod":
                return httpx.Response(
                    200,
                    json={"client_side": "required", "server_side": "unsupported"},
                )
            if request.url.path == "/v2/project/client-mod/version":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "client-version",
                            "project_id": "client-project",
                            "version_number": "1.0.0",
                            "files": [
                                {
                                    "filename": "client.jar",
                                    "primary": True,
                                    "url": "https://cdn.modrinth.com/client.jar",
                                    "hashes": {"sha1": "1" * 40, "sha512": "2" * 128},
                                    "size": 123,
                                }
                            ],
                        }
                    ],
                )
            return httpx.Response(404, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            resolution = await resolve_launcher_metadata_resolution(
                scope="minecraft",
                urls=LauncherProviderUrls(modrinth=page_url),
                local_filename="client.jar",
                http=http,
            )

        self.assertIs(resolution.suggested_mod_type, ModType.CLIENT)
        self.assertIs(resolution.suggestion_provider, Provider.MODRINTH)

    async def test_rejects_provider_not_supported_by_app(self) -> None:
        with self.assertRaisesRegex(ValueError, "factorio does not support launcher metadata from: Modrinth"):
            await resolve_launcher_metadata(
                scope="factorio",
                urls=LauncherProviderUrls(
                    modrinth="https://modrinth.com/plugin/journeymap/version/1.20.1-5.10.3-forge"
                ),
                local_filename="journeymap.jar",
            )

    async def test_curseforge_file_page_requires_api_key(self) -> None:
        with patch.object(config, "env_opt", return_value=None):
            with self.assertRaisesRegex(ValueError, "CURSEFORGE_API_KEY is required"):
                await resolve_launcher_metadata(
                    scope="minecraft",
                    urls=LauncherProviderUrls(
                        curseforge="https://www.curseforge.com/minecraft/mc-mods/journeymap/files/5789363"
                    ),
                    local_filename="journeymap.jar",
                )

    async def test_curseforge_reference_does_not_require_api_key(self) -> None:
        with (
            patch.object(config, "env_opt", return_value=None),
            self.assertLogs("apps._launcher_metadata", level="WARNING") as captured,
        ):
            metadata = await resolve_launcher_metadata(
                scope="minecraft",
                urls=LauncherProviderUrls(
                    curseforge_reference=CurseForgeFileReference(project_id=32274, file_id=5789363)
                ),
                local_filename="journeymap.jar",
            )

        self.assertIsNotNone(metadata.curseforge)
        assert metadata.curseforge is not None
        self.assertEqual(metadata.curseforge.project_id, 32274)
        self.assertEqual(metadata.curseforge.file_id, 5789363)
        self.assertIsNone(metadata.curseforge.page_url)
        self.assertFalse(metadata.curseforge.verified)
        self.assertIn("unverified", captured.output[0])

    async def test_curseforge_reference_is_validated_when_api_key_is_available(self) -> None:
        requested_paths: list[str] = []

        async def handle_request(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            self.assertEqual(request.headers["x-api-key"], "curseforge-test-key")
            match request.url.path:
                case "/v1/mods/32274/files/5789363":
                    return httpx.Response(200, json={"data": {"modId": 32274, "id": 5789363}})
                case "/v1/mods/32274":
                    return httpx.Response(200, json={"data": {"summary": "JourneyMap"}})
                case unexpected_path:
                    self.fail(f"Unexpected CurseForge request: {unexpected_path}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            with patch.object(config, "env_opt", return_value="curseforge-test-key"):
                metadata = await resolve_launcher_metadata(
                    scope="minecraft",
                    urls=LauncherProviderUrls(
                        curseforge_reference=CurseForgeFileReference(
                            project_id=32274,
                            file_id=5789363,
                        )
                    ),
                    local_filename="journeymap.jar",
                    http=http,
                )

        self.assertIsNotNone(metadata.curseforge)
        assert metadata.curseforge is not None
        self.assertEqual(metadata.curseforge.project_id, 32274)
        self.assertEqual(metadata.curseforge.file_id, 5789363)
        self.assertEqual(metadata.curseforge.description, "JourneyMap")
        self.assertIsNone(metadata.curseforge.page_url)
        self.assertTrue(metadata.curseforge.verified)
        self.assertEqual(requested_paths, ["/v1/mods/32274/files/5789363", "/v1/mods/32274"])

    async def test_curseforge_reference_rejects_mismatched_api_response(self) -> None:
        async def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"modId": 999, "id": 5789363}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            with (
                patch.object(config, "env_opt", return_value="curseforge-test-key"),
                self.assertRaisesRegex(ValueError, "different project/file pair"),
            ):
                await resolve_launcher_metadata(
                    scope="minecraft",
                    urls=LauncherProviderUrls(
                        curseforge_reference=CurseForgeFileReference(
                            project_id=32274,
                            file_id=5789363,
                        )
                    ),
                    local_filename="journeymap.jar",
                    http=http,
                )

    def test_rejects_multiple_curseforge_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "either a CurseForge file page"):
            LauncherProviderUrls(
                curseforge="https://www.curseforge.com/minecraft/mc-mods/journeymap/files/5789363",
                curseforge_reference=CurseForgeFileReference(project_id=32274, file_id=5789363),
            )


if __name__ == "__main__":
    unittest.main()
