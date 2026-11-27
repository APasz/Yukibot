from __future__ import annotations

import asyncio
import hashlib
import json
import os
import unittest
import zipfile
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

import config
import httpx
from _mod_ops import (
    ArchiveDataEntry,
    ArchiveEntry,
    ArchivePathRewrite,
    ClientPackValidationError,
    ModArchiveEntry,
    WritableArchiveEntry,
    client_pack_content_hash,
    client_pack_source_fingerprint,
)
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
    preflight_minecraft_pack,
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

    def _entries(self, *, include_bundled: bool = True) -> tuple[ArchiveEntry, ...]:
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
                    verified=True,
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
                    verified=True,
                ),
            ),
        )
        entries: tuple[ArchiveEntry, ...] = (
            ModArchiveEntry.from_mod(shared),
            ModArchiveEntry.from_mod(client),
        )
        if include_bundled:
            bundled = self._mod("bundled.jar")
            entries += (ModArchiveEntry.from_mod(bundled),)
        return (*entries, ArchiveEntry(self.overrides, PurePosixPath("overrides")))

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
            {"client": "required", "server": "unsupported"},
        )
        self.assertEqual(
            files["mods/remote-shared.jar"]["hashes"],
            {"sha1": "1" * 40, "sha512": "2" * 128},
        )
        self.assertEqual(files["mods/remote-shared.jar"]["fileSize"], 101)

    async def test_exports_curseforge_manifest_and_bundles_local_mods(self) -> None:
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
                {"projectID": 202, "fileID": 2002, "required": True},
            ],
        )

    async def test_curseforge_preflight_does_not_read_payloads_or_create_an_archive(self) -> None:
        entries = self._entries()
        with (
            patch.object(config, "DIR_ZIPS", self.zips),
            patch.object(Path, "open", side_effect=AssertionError("Preflight opened a payload")),
        ):
            await preflight_minecraft_pack(entries, self._spec(PackFormat.CURSEFORGE))

        self.assertFalse(self.zips.exists())

    async def test_curseforge_preflight_rejects_collisions_after_bundling(self) -> None:
        entries = (
            *self._entries(),
            ArchiveDataEntry(PurePosixPath("overrides/mods/bundled.jar"), b"collision"),
        )

        with self.assertRaisesRegex(ValueError, "Duplicate mod archive path"):
            await preflight_minecraft_pack(entries, self._spec(PackFormat.CURSEFORGE))

    async def test_curseforge_preflight_rejects_missing_bundled_sources(self) -> None:
        entries = self._entries()
        (self.root / "bundled.jar").unlink()

        with self.assertRaisesRegex(ValueError, "Unsupported mod archive source"):
            await preflight_minecraft_pack(entries, self._spec(PackFormat.CURSEFORGE))

    async def test_curseforge_preflight_rejects_symlinks_in_overrides(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (self.overrides / "linked.txt").symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "symbolic links"):
            await preflight_minecraft_pack(self._entries(), self._spec(PackFormat.CURSEFORGE))

    async def test_curseforge_preflight_rejects_invalid_zip_timestamps(self) -> None:
        entries = self._entries()
        os.utime(self.root / "bundled.jar", (1, 1))

        with self.assertRaisesRegex(ValueError, "ZIP.*1980"):
            await preflight_minecraft_pack(entries, self._spec(PackFormat.CURSEFORGE))

    async def test_preflight_and_export_reject_file_parent_conflicts(self) -> None:
        entries = (
            ArchiveDataEntry(PurePosixPath("config"), b"file"),
            ArchiveDataEntry(PurePosixPath("config/client.json"), b"child"),
        )
        with self.assertRaisesRegex(ValueError, "conflicting paths"):
            await preflight_minecraft_pack(entries, self._spec(PackFormat.GENERIC_ZIP))
        with patch.object(config, "DIR_ZIPS", self.zips), self.assertRaisesRegex(ValueError, "conflicting paths"):
            await export_minecraft_pack(entries, self._spec(PackFormat.GENERIC_ZIP), "conflicting")
        self.assertFalse((self.zips / "conflicting.zip").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires filesystem FIFO support")
    async def test_preflight_rejects_special_files_without_opening_them(self) -> None:
        os.mkfifo(self.overrides / "pipe")
        with self.assertRaisesRegex(ValueError, "Unsupported.*source"):
            await preflight_minecraft_pack(self._entries(), self._spec(PackFormat.CURSEFORGE))

    async def test_preflight_rejects_invalid_and_oversized_zip_member_names(self) -> None:
        for path in ("invalid\x00name.txt", "a" * 65536):
            with self.subTest(path=path[:30]), self.assertRaises(ValueError):
                await preflight_minecraft_pack(
                    (ArchiveDataEntry(PurePosixPath(path), b"data"),), self._spec(PackFormat.GENERIC_ZIP),
                )

    async def test_client_exports_and_publication_reject_windows_reserved_paths(self) -> None:
        for name in ("CON.txt", "config/nul.json", "config/file:stream", "config/trailing."):
            entries = (ArchiveDataEntry(PurePosixPath(name), b"data"),)
            with self.subTest(name=name):
                for hash_pack in (client_pack_content_hash, client_pack_source_fingerprint):
                    with self.assertRaisesRegex(ClientPackValidationError, "not supported on Windows"):
                        hash_pack(entries, format_name="test")
                with self.assertRaisesRegex(ValueError, "not supported on Windows"):
                    await preflight_minecraft_pack(entries, self._spec(PackFormat.GENERIC_ZIP))
                with patch.object(config, "DIR_ZIPS", self.zips), self.assertRaisesRegex(ValueError, "not supported on Windows"):
                    await export_minecraft_pack(entries, self._spec(PackFormat.GENERIC_ZIP), "invalid-windows")
                self.assertFalse((self.zips / "invalid-windows.zip").exists())

    def test_modrinth_metadata_rejects_nonportable_download_names(self) -> None:
        metadata = ModrinthModMetadata(
            page_url="https://modrinth.com/mod/example", project_id="project", version_id="version",
            download_url="https://cdn.modrinth.com/example.jar",
        )
        for filename in (
            "CON.jar", "bad:stream.jar", "mods\\example.jar", "example.jar.",
            "example.jar ", "example.jar\n", "example.jar\t", "", " ", True, 123,
        ):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                ModrinthModMetadata.model_validate({**metadata.model_dump(), "filename": filename})

        preserved = ModrinthModMetadata.model_validate({**metadata.model_dump(), "filename": " example.jar"})
        self.assertEqual(preserved.filename, " example.jar")

    def test_client_pack_fingerprinting_rejects_unreadable_directories(self) -> None:
        entries = (ArchiveEntry(self.overrides, PurePosixPath("overrides")),)
        with patch("os.scandir", side_effect=PermissionError("Unreadable directory")):
            with self.assertRaises(PermissionError):
                client_pack_source_fingerprint(entries, format_name="test")

    async def test_preflight_reports_unreadable_directories_as_export_errors(self) -> None:
        entries = self._entries()
        with patch("os.scandir", side_effect=PermissionError("Unreadable directory")):
            with self.assertRaisesRegex(MinecraftPackExportError, "source could not be read"):
                await preflight_minecraft_pack(entries, self._spec(PackFormat.CURSEFORGE))

    def test_client_pack_hash_preserves_published_hash_format(self) -> None:
        source = self.root / "compatibility"
        (source / "nested").mkdir(parents=True)
        (source / "empty").mkdir()
        (source / "ModInfo.xml.client").write_bytes(b"<xml/>")
        (source / "nested/payload.bin").write_bytes(b"a" * (1024**2 + 1))
        entries = (
            ArchiveEntry(source, PurePosixPath("mods/Example"), path_rewrites=(
                ArchivePathRewrite(PurePosixPath("ModInfo.xml.client"), PurePosixPath("ModInfo.xml")),
            )),
            ArchiveDataEntry(PurePosixPath("generated.txt"), b"generated"),
        )
        self.assertEqual(
            client_pack_content_hash(entries, format_name="published-hash-compatibility"),
            "612f1a43465c0fb132264eedbfd2958bbc6f312acfb7b19936a989efc0597f59",
        )

    async def test_cancelled_export_waits_for_worker_and_removes_partial_archive(self) -> None:
        started, stopped = Event(), Event()

        def write_archive(
            entries: Collection[WritableArchiveEntry], path: Path, *, cancelled: Event | None = None,
        ) -> None:
            del entries
            assert cancelled is not None
            path.write_bytes(b"partial ZIP")
            started.set()
            try:
                if not cancelled.wait(timeout=2):
                    raise TimeoutError("Archive worker did not receive cancellation")
                raise InterruptedError("Archive operation cancelled")
            finally:
                stopped.set()

        with (
            patch.object(config, "DIR_ZIPS", self.zips),
            patch("_mod_ops._write_mod_archive", side_effect=write_archive),
        ):
            task = asyncio.create_task(export_minecraft_pack(
                self._entries(), self._spec(PackFormat.CURSEFORGE), "cancelled", unique_output=True,
            ))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=3)
        self.assertTrue(stopped.is_set())
        self.assertEqual(tuple(self.zips.iterdir()), ())

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
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1001, verified=True)
            ),
        )
        second = self._mod(
            "second.jar",
            platforms=ModPlatformMetadata(
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1002, verified=True)
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

    async def test_curseforge_export_allows_unverified_metadata(self) -> None:
        unverified = self._mod(
            "unverified.jar",
            platforms=ModPlatformMetadata(
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1001)
            ),
        )

        with patch.object(config, "DIR_ZIPS", self.zips):
            archive_path = await export_minecraft_pack(
                (ModArchiveEntry.from_mod(unverified),),
                self._spec(PackFormat.CURSEFORGE),
                "unverified",
            )

        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["files"], [{"projectID": 101, "fileID": 1001, "required": True}])

    async def test_curseforge_export_allows_vanilla_profiles(self) -> None:
        spec = replace(self._spec(PackFormat.CURSEFORGE), loader="vanilla", loader_version=None)

        with patch.object(config, "DIR_ZIPS", self.zips):
            archive_path = await export_minecraft_pack(
                self._entries(),
                spec,
                "vanilla",
            )

        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["minecraft"]["modLoaders"], [])

    async def test_curseforge_export_rejects_non_downloadable_local_mod(self) -> None:
        blocked = self._mod(
            "blocked.jar",
            download_block_reason=ModDownloadBlockReason.OTHER,
        )

        with self.assertRaisesRegex(MinecraftPackExportError, "non-downloadable local mods without metadata: blocked.jar"):
            await export_minecraft_pack(
                (ModArchiveEntry.from_mod(blocked),),
                self._spec(PackFormat.CURSEFORGE),
                "blocked",
            )

    async def test_export_uses_unique_output_when_requested(self) -> None:
        entries = (ArchiveEntry(self.overrides, PurePosixPath("overrides")),)

        with patch.object(config, "DIR_ZIPS", self.zips):
            first = await export_minecraft_pack(
                entries,
                self._spec(PackFormat.GENERIC_ZIP),
                "example",
                unique_output=True,
            )
            second = await export_minecraft_pack(
                entries,
                self._spec(PackFormat.GENERIC_ZIP),
                "example",
                unique_output=True,
            )

        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_client_pack_hash_rejects_symlink_sources(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.overrides / "linked.txt").symlink_to(outside)

        with self.assertRaisesRegex(ClientPackValidationError, "symbolic links"):
            client_pack_content_hash(
                (ArchiveEntry(self.overrides, PurePosixPath("overrides")),),
                format_name="test",
            )

    def test_client_pack_hash_includes_launcher_manifest_metadata(self) -> None:
        mod = self._mod(
            "manifest.jar",
            platforms=ModPlatformMetadata(
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1001, verified=True)
            ),
        )
        entry = ModArchiveEntry.from_mod(mod)
        changed_entry = replace(
            entry,
            client_pack_policy=ClientPackPolicy.OPTIONAL,
            platforms=ModPlatformMetadata(
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1002, verified=True)
            ),
        )
        unverified_entry = replace(
            entry,
            platforms=ModPlatformMetadata(
                curseforge=CurseForgeModMetadata(project_id=101, file_id=1001, verified=False)
            ),
        )

        original_hash = client_pack_content_hash((entry,), format_name="test")
        changed_hash = client_pack_content_hash((changed_entry,), format_name="test")
        unverified_hash = client_pack_content_hash((unverified_entry,), format_name="test")

        self.assertNotEqual(original_hash, changed_hash)
        self.assertEqual(original_hash, unverified_hash)

    def test_client_pack_hash_rejects_duplicate_archive_members(self) -> None:
        source = self.overrides / "servers.dat"
        source.write_bytes(b"manual-server")

        with self.assertRaisesRegex(ClientPackValidationError, "Duplicate client-pack archive"):
            client_pack_content_hash(
                (
                    ArchiveEntry(source, PurePosixPath("overrides/servers.dat")),
                    ArchiveDataEntry(PurePosixPath("overrides/servers.dat"), b"generated-server"),
                ),
                format_name="test",
            )

    def test_client_pack_hash_rejects_file_directory_member_collision(self) -> None:
        nested_directory = self.overrides / "nested"
        nested_directory.mkdir()
        (nested_directory / "config.json").write_text("{}", encoding="utf-8")
        conflicting_file = self.root / "nested"
        conflicting_file.write_bytes(b"not a directory")

        with self.assertRaisesRegex(ClientPackValidationError, "[Dd]uplicate paths"):
            client_pack_content_hash(
                (
                    ArchiveEntry(self.overrides, PurePosixPath("overrides")),
                    ArchiveEntry(conflicting_file, PurePosixPath("overrides/nested")),
                ),
                format_name="test",
            )

    def test_client_pack_hash_verifies_expected_file_digests_without_changing_the_pack_hash(self) -> None:
        source = self.root / "asset.bin"
        content = b"verified asset"
        source.write_bytes(content)
        path = PurePosixPath("Mods/example/asset.bin")
        entries = (ArchiveEntry(source, path),)
        original = client_pack_content_hash(entries, format_name="test")
        verified = client_pack_content_hash(
            entries, format_name="test", expected_file_hashes={path: hashlib.sha256(content).hexdigest()},
        )
        self.assertEqual(original, verified)
        with self.assertRaisesRegex(ClientPackValidationError, "does not match.*asset.bin"):
            client_pack_content_hash(entries, format_name="test", expected_file_hashes={path: "0" * 64})
        with self.assertRaisesRegex(ClientPackValidationError, "missing.*absent.bin"):
            client_pack_content_hash(
                entries, format_name="test", expected_file_hashes={PurePosixPath("absent.bin"): "0" * 64},
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
