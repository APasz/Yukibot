from __future__ import annotations

import asyncio
import json
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
import httpx
from _mod_ops import ArchiveEntry, ModArchiveEntry
from apps._config import (
    ClientPackConfig,
    ClientPackPolicy,
    CurseForgeModMetadata,
    Mod_Config,
    ModDownloadBlockReason,
    ModPlacement,
    ModPlatformMetadata,
    ModrinthModMetadata,
    ModType,
)
from apps._mod import Mod
from apps.minecraft.pack_export import (
    MinecraftPackExportError,
    MinecraftPackSpec,
    PackFormat,
    PackPurpose,
    client_pack_kubejs_entries,
    discover_client_pack_kubejs_scripts,
    export_minecraft_pack,
)


class _PackMod(Mod):
    async def install(self, src: Path, atomic: bool = True) -> None:
        del src, atomic


class MinecraftPackExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.zips = self.root / "zips"
        self.overrides = self.root / "client-overrides"
        self.overrides.mkdir()
        (self.overrides / "options.txt").write_text("settings", encoding="utf-8")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _mod(
        self,
        name: str,
        *,
        placement: ModPlacement = ModPlacement.SERVER_ENABLED,
        mod_type: ModType = ModType.REGULAR,
        client_pack: ClientPackConfig | None = None,
        platforms: ModPlatformMetadata | None = None,
        download_block_reason: ModDownloadBlockReason | None = None,
    ) -> _PackMod:
        config_path = self.root / (
            f"{name}.client" if placement is ModPlacement.CLIENT_ONLY else name
        )
        config_path.write_bytes(name.encode())
        return _PackMod(
            Mod_Config(
                name=name,
                directory=self.root,
                placement=placement,
                mod_type=mod_type,
                download_block_reason=download_block_reason,
                client_pack=client_pack or ClientPackConfig(),
                platforms=platforms or ModPlatformMetadata(),
            )
        )

    def _entries(self) -> tuple[ArchiveEntry, ...]:
        shared = self._mod(
            "shared.jar",
            platforms=ModPlatformMetadata(
                modrinth=ModrinthModMetadata(
                    page_url="https://modrinth.com/mod/shared/version/shared-version",
                    project_id="shared-project",
                    version_id="shared-version",
                    download_url="https://cdn.modrinth.com/data/shared/shared.jar",
                    filename="remote-shared.jar",
                    sha1="1" * 40,
                    sha512="2" * 128,
                    size=101,
                ),
                curseforge=CurseForgeModMetadata(
                    page_url="https://www.curseforge.com/minecraft/mc-mods/shared/files/1001",
                    project_id=101,
                    file_id=1001,
                ),
            ),
        )
        client = self._mod(
            "client.jar",
            placement=ModPlacement.CLIENT_ONLY,
            mod_type=ModType.CLIENT,
            client_pack=ClientPackConfig(policy=ClientPackPolicy.OPTIONAL),
            platforms=ModPlatformMetadata(
                modrinth=ModrinthModMetadata(
                    page_url="https://modrinth.com/mod/client/version/client-version",
                    project_id="client-project",
                    version_id="client-version",
                    download_url="https://cdn.modrinth.com/data/client/client.jar",
                    filename="remote-client.jar",
                    sha1="3" * 40,
                    sha512="4" * 128,
                    size=202,
                ),
                curseforge=CurseForgeModMetadata(
                    page_url="https://www.curseforge.com/minecraft/mc-mods/client/files/2002",
                    project_id=202,
                    file_id=2002,
                ),
            ),
        )
        bundled = self._mod("bundled.jar")
        return (
            ModArchiveEntry.from_mod(shared),
            ModArchiveEntry.from_mod(client),
            ModArchiveEntry.from_mod(bundled),
            ArchiveEntry(self.overrides, PurePosixPath("overrides")),
        )

    @staticmethod
    def _spec(format: PackFormat) -> MinecraftPackSpec:
        return MinecraftPackSpec(
            purpose=PackPurpose.CLIENT,
            format=format,
            name="Example Pack",
            version_id="2026-07-04",
            minecraft_version="1.21.1",
            loader="fabric",
            loader_version="0.16.10",
            author="Example Author",
            summary="Example summary",
        )

    async def test_exports_modrinth_pack_with_env_hashes_and_bundled_fallback(self) -> None:
        requested_urls: list[str] = []

        async def handle_request(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["range"], "bytes=0-0")
            requested_urls.append(str(request.url))
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            with patch.object(config, "DIR_ZIPS", self.zips):
                archive_path = await export_minecraft_pack(
                    self._entries(),
                    self._spec(PackFormat.MODRINTH),
                    "example",
                    http=http,
                )

        self.assertEqual(archive_path.suffix, ".mrpack")
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "modrinth.index.json",
                    "overrides/mods/bundled.jar",
                    "overrides/options.txt",
                },
            )
            manifest = json.loads(archive.read("modrinth.index.json"))

        self.assertEqual(
            manifest["dependencies"],
            {"minecraft": "1.21.1", "fabric-loader": "0.16.10"},
        )
        self.assertEqual(manifest["name"], "Example Pack")
        self.assertEqual(manifest["summary"], "Example summary")
        files = {entry["path"]: entry for entry in manifest["files"]}
        self.assertEqual(
            requested_urls,
            [
                "https://cdn.modrinth.com/data/shared/shared.jar",
                "https://cdn.modrinth.com/data/client/client.jar",
            ],
        )
        self.assertEqual(
            files["mods/remote-shared.jar"]["env"],
            {"client": "required", "server": "required"},
        )
        self.assertEqual(
            files["mods/remote-client.jar"]["env"],
            {"client": "optional", "server": "unsupported"},
        )
        self.assertEqual(
            files["mods/remote-shared.jar"]["hashes"],
            {"sha1": "1" * 40, "sha512": "2" * 128},
        )
        self.assertEqual(files["mods/remote-shared.jar"]["fileSize"], 101)

    async def test_exports_curseforge_manifest_and_bundles_non_platform_mods(self) -> None:
        with patch.object(config, "DIR_ZIPS", self.zips):
            archive_path = await export_minecraft_pack(
                self._entries(),
                self._spec(PackFormat.CURSEFORGE),
                "example",
            )

        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"manifest.json", "overrides/mods/bundled.jar", "overrides/options.txt"},
            )
            manifest = json.loads(archive.read("manifest.json"))

        self.assertEqual(
            manifest["minecraft"],
            {
                "version": "1.21.1",
                "modLoaders": [{"id": "fabric-0.16.10", "primary": True}],
            },
        )
        self.assertEqual(manifest["version"], "2026-07-04")
        self.assertEqual(manifest["name"], "Example Pack")
        self.assertEqual(manifest["description"], "Example summary")
        self.assertEqual(
            manifest["files"],
            [
                {"projectID": 101, "fileID": 1001, "required": True},
                {"projectID": 202, "fileID": 2002, "required": False},
            ],
        )

    async def test_exports_generic_zip_without_platform_transformation(self) -> None:
        with patch.object(config, "DIR_ZIPS", self.zips):
            archive_path = await export_minecraft_pack(
                self._entries(),
                self._spec(PackFormat.GENERIC_ZIP),
                "example",
            )

        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"shared.jar", "client.jar", "bundled.jar", "overrides/options.txt"},
            )
            self.assertEqual(archive.getinfo("shared.jar").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.getinfo("client.jar").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.getinfo("bundled.jar").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.getinfo("overrides/options.txt").compress_type, zipfile.ZIP_DEFLATED)

    async def test_modrinth_remote_preflights_run_concurrently(self) -> None:
        active_requests = 0
        peak_active_requests = 0

        async def handle_request(request: httpx.Request) -> httpx.Response:
            nonlocal active_requests, peak_active_requests
            active_requests += 1
            peak_active_requests = max(peak_active_requests, active_requests)
            await asyncio.sleep(0.01)
            active_requests -= 1
            return httpx.Response(200, request=request)

        mods = tuple(
            self._mod(
                f"remote-{index}.jar",
                platforms=ModPlatformMetadata(
                    modrinth=ModrinthModMetadata(
                        page_url=f"https://modrinth.com/mod/remote-{index}/version/version-{index}",
                        project_id=f"project-{index}",
                        version_id=f"version-{index}",
                        download_url=f"https://cdn.modrinth.com/data/remote-{index}.jar",
                        filename=f"remote-{index}.jar",
                        sha1=str(index) * 40,
                        sha512=str(index) * 128,
                        size=100 + index,
                    )
                ),
            )
            for index in range(1, 4)
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            with patch.object(config, "DIR_ZIPS", self.zips):
                await export_minecraft_pack(
                    tuple(ModArchiveEntry.from_mod(mod) for mod in mods),
                    self._spec(PackFormat.MODRINTH),
                    "concurrent",
                    http=http,
                )

        self.assertEqual(peak_active_requests, len(mods))

    async def test_modrinth_export_rejects_incomplete_remote_metadata_before_preflight(self) -> None:
        incomplete = self._mod(
            "incomplete.jar",
            platforms=ModPlatformMetadata(
                modrinth=ModrinthModMetadata(
                    page_url="https://modrinth.com/mod/incomplete/version/1",
                    project_id="incomplete-project",
                    version_id="incomplete-version",
                    download_url="https://cdn.modrinth.com/incomplete.jar",
                )
            ),
        )

        with self.assertRaisesRegex(MinecraftPackExportError, "missing filename, sha1, sha512, size"):
            await export_minecraft_pack(
                (ModArchiveEntry.from_mod(incomplete),),
                self._spec(PackFormat.MODRINTH),
                "incomplete",
            )

    async def test_modrinth_export_rejects_unreachable_remote_url_before_writing(self) -> None:
        async def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            with (
                patch.object(config, "DIR_ZIPS", self.zips),
                self.assertRaisesRegex(MinecraftPackExportError, "not reachable"),
            ):
                await export_minecraft_pack(
                    self._entries(),
                    self._spec(PackFormat.MODRINTH),
                    "unreachable",
                    http=http,
                )

        self.assertFalse((self.zips / "unreachable.mrpack").exists())

    async def test_curseforge_export_rejects_duplicate_project_ids(self) -> None:
        first = self._mod(
            "first.jar",
            platforms=ModPlatformMetadata(
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1001)
            ),
        )
        second = self._mod(
            "second.jar",
            platforms=ModPlatformMetadata(
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1002)
            ),
        )

        with self.assertRaisesRegex(
            MinecraftPackExportError,
            "project 101 is referenced more than once",
        ):
            await export_minecraft_pack(
                (ModArchiveEntry.from_mod(first), ModArchiveEntry.from_mod(second)),
                self._spec(PackFormat.CURSEFORGE),
                "duplicate",
            )

    async def test_curseforge_export_lists_non_bundleable_non_curseforge_mods(self) -> None:
        blocked = self._mod(
            "blocked.jar",
            download_block_reason=ModDownloadBlockReason.OTHER,
        )

        with self.assertRaisesRegex(MinecraftPackExportError, "bundling is disabled: blocked.jar"):
            await export_minecraft_pack(
                (ModArchiveEntry.from_mod(blocked),),
                self._spec(PackFormat.CURSEFORGE),
                "blocked",
            )

    async def test_client_export_includes_server_only_entry(self) -> None:
        server = self._mod("server.jar", mod_type=ModType.SERVER)

        with patch.object(config, "DIR_ZIPS", self.zips):
            archive_path = await export_minecraft_pack(
                (ModArchiveEntry.from_mod(server),),
                self._spec(PackFormat.GENERIC_ZIP),
                "example",
            )

        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.namelist(), ["server.jar"])

    async def test_client_modrinth_pack_marks_server_only_entry_as_client_required(self) -> None:
        server = self._mod(
            "server.jar",
            mod_type=ModType.SERVER,
            platforms=ModPlatformMetadata(
                modrinth=ModrinthModMetadata(
                    page_url="https://modrinth.com/mod/server/version/server-version",
                    project_id="server-project",
                    version_id="server-version",
                    download_url="https://cdn.modrinth.com/data/server/server.jar",
                    filename="server.jar",
                    sha1="1" * 40,
                    sha512="2" * 128,
                    size=101,
                )
            ),
        )

        async def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http:
            with patch.object(config, "DIR_ZIPS", self.zips):
                archive_path = await export_minecraft_pack(
                    (ModArchiveEntry.from_mod(server),),
                    self._spec(PackFormat.MODRINTH),
                    "example",
                    http=http,
                )

        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("modrinth.index.json"))
        self.assertEqual(
            manifest["files"][0]["env"],
            {"client": "required", "server": "required"},
        )

    async def test_export_rejects_symlinks_inside_override_directory(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.overrides / "linked.txt").symlink_to(outside)

        with (
            patch.object(config, "DIR_ZIPS", self.zips),
            self.assertRaisesRegex(ValueError, "symbolic links"),
        ):
            await export_minecraft_pack(
                self._entries(),
                self._spec(PackFormat.GENERIC_ZIP),
                "example",
            )

    async def test_server_mrpack_bundles_local_mods_as_server_overrides(self) -> None:
        server = self._mod("server.jar", mod_type=ModType.SERVER)
        spec = replace(self._spec(PackFormat.MODRINTH), purpose=PackPurpose.SERVER)

        with patch.object(config, "DIR_ZIPS", self.zips):
            archive_path = await export_minecraft_pack(
                (ModArchiveEntry.from_mod(server),),
                spec,
                "server-example",
            )

        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"modrinth.index.json", "server-overrides/mods/server.jar"},
            )

    def test_discovers_configurable_kubejs_scripts_and_excludes_examples(self) -> None:
        server_scripts = self.root / "kubejs" / "server_scripts"
        startup_scripts = self.root / "kubejs" / "startup_scripts"
        nested_scripts = server_scripts / "recipes"
        nested_scripts.mkdir(parents=True)
        startup_scripts.mkdir(parents=True)
        (server_scripts / "example.js").write_text("example", encoding="utf-8")
        (server_scripts / "events.js").write_text("events", encoding="utf-8")
        (nested_scripts / "custom.js").write_text("recipes", encoding="utf-8")
        (startup_scripts / "registry.js").write_text("registry", encoding="utf-8")

        scripts = discover_client_pack_kubejs_scripts(
            self.root,
            excluded_paths=frozenset({"server_scripts/events.js"}),
        )

        self.assertEqual(
            tuple((script.relative_path, script.included) for script in scripts),
            (
                ("server_scripts/events.js", False),
                ("server_scripts/recipes/custom.js", True),
                ("startup_scripts/registry.js", True),
            ),
        )

        entries = client_pack_kubejs_entries(
            self.root,
            excluded_paths=frozenset({"server_scripts/events.js"}),
        )
        self.assertEqual(
            tuple(entry.archive_path.as_posix() for entry in entries),
            (
                "overrides/kubejs/server_scripts/recipes/custom.js",
                "overrides/kubejs/startup_scripts/registry.js",
            ),
        )


if __name__ == "__main__":
    unittest.main()
