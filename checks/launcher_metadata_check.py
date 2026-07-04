from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from modmux.models import Provider

import config
from apps._config import CurseForgeFileReference, LauncherProviderUrls, ModPlatformMetadata, ModType
from apps._launcher_metadata import (
    ModrinthSideSupport,
    _suggest_mod_type_from_modrinth,
    launcher_project_page_url,
    resolve_launcher_metadata,
    resolve_launcher_metadata_resolution,
)


class LauncherMetadataTests(unittest.IsolatedAsyncioTestCase):
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

        persisted = ModPlatformMetadata.model_validate(metadata.model_dump(mode="json"))
        assert persisted.modrinth is not None
        assert persisted.curseforge is not None
        self.assertEqual(persisted.modrinth.page_url, modrinth_page)
        self.assertEqual(persisted.curseforge.page_url, curseforge_page)

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
        self.assertIn("unverified", captured.output[0])

    async def test_curseforge_reference_is_validated_when_api_key_is_available(self) -> None:
        async def handle_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/mods/32274/files/5789363")
            self.assertEqual(request.headers["x-api-key"], "curseforge-test-key")
            return httpx.Response(200, json={"data": {"modId": 32274, "id": 5789363}})

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
        self.assertIsNone(metadata.curseforge.page_url)

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
