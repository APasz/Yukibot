from __future__ import annotations

import json
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from _mod_ops import ArchiveEntry, ModArchiveEntry
from apps._config import (
    ClientPackConfig,
    ClientPackPolicy,
    CurseForgeModMetadata,
    Mod_Config,
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
        with patch.object(config, "DIR_ZIPS", self.zips):
            archive_path = await export_minecraft_pack(
                self._entries(),
                self._spec(PackFormat.MODRINTH),
                "example",
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
        files = {entry["path"]: entry for entry in manifest["files"]}
        self.assertEqual(files["mods/shared.jar"]["env"], {"client": "required", "server": "required"})
        self.assertEqual(files["mods/client.jar"]["env"], {"client": "optional", "server": "unsupported"})
        self.assertEqual(len(files["mods/shared.jar"]["hashes"]["sha1"]), 40)
        self.assertEqual(len(files["mods/shared.jar"]["hashes"]["sha512"]), 128)

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

    async def test_client_export_rejects_server_only_entry(self) -> None:
        server = self._mod("server.jar", mod_type=ModType.SERVER)

        with self.assertRaisesRegex(MinecraftPackExportError, "server-only"):
            await export_minecraft_pack(
                (ModArchiveEntry.from_mod(server),),
                self._spec(PackFormat.GENERIC_ZIP),
                "example",
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


if __name__ == "__main__":
    unittest.main()
