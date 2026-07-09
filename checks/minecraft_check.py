from __future__ import annotations

import asyncio
import json
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Never, cast
from unittest.mock import AsyncMock, call, patch

import config
from _relay_embeds import build_app_lifecycle_embed
from apps._console import ConsoleResponseSource, execute_console_action
from apps import minecraft
from apps._config import Mod_Config, ModType
from apps.minecraft import (
    Activities as MinecraftActivities,
)
from apps.minecraft import (
    KubeJsRecipeAddonKind,
    KubeJsRecipeSupportStatus,
    Minecraft,
    Minecraft_Config,
    MinecraftCookingRecipe,
    MinecraftItemRegistrySnapshot,
    MinecraftRecipeBook,
    MinecraftRecipeIngredient,
    MinecraftRecipeItemStack,
    MinecraftRecipeKind,
    MinecraftRecipeRemoval,
    MinecraftRecipeRemovalFilter,
    MinecraftRecipeUnificationMode,
    MinecraftShapedRecipe,
    MinecraftShapelessRecipe,
    MinecraftStonecuttingRecipe,
    Mod_MC,
    _detect_minecraft_mod_version,
    _load_squaremap_web_address,
    _managed_kubejs_recipe_script_source,
    generated_minecraft_recipe_id,
    generated_minecraft_recipe_mutation_id,
)


class _RecordingActivityManager:
    def __init__(self) -> None:
        self.registered: list[object] = []
        self.deregistered: list[object] = []

    def register(self, provider: object) -> None:
        self.registered.append(provider)

    def deregister(self, provider: object) -> None:
        self.deregistered.append(provider)


class MinecraftConsoleActionTests(unittest.IsolatedAsyncioTestCase):
    def _console_app(self) -> Minecraft:
        app = cast(Minecraft, object.__new__(Minecraft))
        app.friendly = "Minecraft Test"
        app.cfg = SimpleNamespace(rcon_requires_online_players=False)
        app._relay = SimpleNamespace(send=AsyncMock(side_effect=["saved", "stopped"]))
        return app

    async def test_stop_server_sends_save_all_before_stop(self) -> None:
        app = self._console_app()
        action = next(action for action in app.console_actions if action.key == "stop_server")

        result = await execute_console_action(
            app=app,
            is_running=lambda: True,
            action=action,
            raw_value=None,
        )

        self.assertEqual(
            app._relay.send.await_args_list,
            [
                call("save-all"),
                call("stop"),
            ],
        )
        self.assertEqual(result.summary, "Minecraft Test: save-all and stop requested.")
        self.assertEqual(result.text, "stopped")
        self.assertEqual(result.source, ConsoleResponseSource.RCON)


class MinecraftModVersionDetectionTests(unittest.TestCase):
    def test_detects_versions_from_current_sample_file_names(self) -> None:
        cases = {
            "ChatImage-1.4.7+1.20.1+forge.jar": "1.4.7",
            "cloth-config-11.1.136-forge.jar": "11.1.136",
            "appleskin-forge-mc1.20.1-2.5.1.jar": "2.5.1",
            "chat_heads-0.13.18-forge-1.20.jar": "0.13.18",
            "Controlling-forge-1.20.1-12.0.2.jar": "12.0.2",
            "Jade-1.20.1-Forge-11.13.1.jar": "11.13.1",
            "modelfix-1.15.jar": "1.15",
            "modernfix-forge-5.23.1+mc1.20.1.jar": "5.23.1",
            "comforts-forge-6.4.0+1.20.1.jar": "6.4.0",
            "fallingleaves-1.20.1-2.1.2.jar": "2.1.2",
            "MouseTweaks-forge-mc1.20.1-2.25.1.jar": "2.25.1",
            "NoChatReports-FORGE-1.20.1-v2.2.2.jar": "2.2.2",
            "Searchables-forge-1.20.1-1.0.3.jar": "1.0.3",
            "bellsandwhistles-0.4.5-1.20.x-Create6.0+.jar": "0.4.5",
        }

        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_detect_minecraft_mod_version(name), expected)

    def test_detects_human_friendly_names_from_current_sample_file_names(self) -> None:
        cases = {
            "ChatImage-1.4.7+1.20.1+forge.jar": "Chat Image",
            "cloth-config-11.1.136-forge.jar": "Cloth Config",
            "NoChatReports-FORGE-1.20.1-v2.2.2.jar": "No Chat Reports",
            "bellsandwhistles-0.4.5-1.20.x-Create6.0+.jar": "Bellsandwhistles",
        }

        for name, expected in cases.items():
            with self.subTest(name=name):
                mod = Mod_MC(Mod_Config(name=name, directory=Path(".")))
                mod.sync_metadata()
                self.assertEqual(mod.friendly, expected)

    def test_prefers_embedded_mod_display_names_across_supported_loaders(self) -> None:
        metadata_cases = {
            "forge.jar": (
                "META-INF/mods.toml",
                'modLoader="javafml"\n[[mods]]\nmodId="example"\ndisplayName="Forge Display Name"\n',
                "Forge Display Name",
            ),
            "neoforge.jar": (
                "META-INF/neoforge.mods.toml",
                'modLoader="javafml"\n[[mods]]\nmodId="example"\ndisplayName="NeoForge Display Name"\n',
                "NeoForge Display Name",
            ),
            "fabric.jar": (
                "fabric.mod.json",
                json.dumps({"schemaVersion": 1, "id": "example", "name": "Fabric Display Name"}),
                "Fabric Display Name",
            ),
            "quilt.jar": (
                "quilt.mod.json",
                json.dumps(
                    {
                        "quilt_loader": {
                            "id": "example",
                            "metadata": {"name": "Quilt Display Name"},
                        }
                    }
                ),
                "Quilt Display Name",
            ),
        }

        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for filename, (metadata_path, metadata_text, expected) in metadata_cases.items():
                with self.subTest(filename=filename):
                    mod_path = directory / filename
                    with zipfile.ZipFile(mod_path, "w") as archive:
                        archive.writestr(metadata_path, metadata_text)
                    mod = Mod_MC(Mod_Config(name=filename, directory=directory))
                    mod.sync_metadata()
                    self.assertEqual(mod.friendly, expected)

    def test_prefers_embedded_version_over_filename_version(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "bellsandwhistles-0.4.5-1.20.x-Create6.0+.jar"
            mod_path = directory / filename
            with zipfile.ZipFile(mod_path, "w") as archive:
                archive.writestr(
                    "META-INF/mods.toml",
                    'modLoader="javafml"\n[[mods]]\nmodId="bellsandwhistles"\n'
                    'version="0.4.3-1.20.x"\ndisplayName="Create: Bells & Whistles"\n',
                )

            mod = Mod_MC(Mod_Config(name=filename, directory=directory))
            mod.sync_metadata()

            self.assertEqual(mod.friendly, "Create: Bells & Whistles")
            self.assertEqual(mod.cfg.version, "0.4.3-1.20.x")

    def test_reads_version_and_homepage_across_supported_loaders(self) -> None:
        metadata_cases = {
            "forge.jar": (
                "META-INF/mods.toml",
                'modLoader="javafml"\n[[mods]]\nmodId="example"\nversion="2.1.0"\n'
                'displayName="Forge Example"\ndisplayURL="https://modrinth.com/mod/example"\n',
                "2.1.0",
                "https://modrinth.com/mod/example",
            ),
            "fabric.jar": (
                "fabric.mod.json",
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "example",
                        "version": "3.2.1",
                        "name": "Fabric Example",
                        "contact": {"homepage": "https://example.com/fabric"},
                    }
                ),
                "3.2.1",
                "https://example.com/fabric",
            ),
            "quilt.jar": (
                "quilt.mod.json",
                json.dumps(
                    {
                        "quilt_loader": {
                            "id": "example",
                            "version": "4.3.2",
                            "metadata": {
                                "name": "Quilt Example",
                                "contact": {"homepage": "https://github.com/example/quilt"},
                            },
                        }
                    }
                ),
                "4.3.2",
                "https://github.com/example/quilt",
            ),
        }

        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for filename, (metadata_path, metadata_text, version, homepage) in metadata_cases.items():
                with self.subTest(filename=filename):
                    with zipfile.ZipFile(directory / filename, "w") as archive:
                        archive.writestr(metadata_path, metadata_text)
                    mod = Mod_MC(Mod_Config(name=filename, directory=directory))

                    mod.sync_metadata()

                    self.assertEqual(mod.version, version)
                    self.assertEqual(len(mod.cfg.mod_pages), 1)
                    self.assertEqual(mod.cfg.mod_pages[0].url, homepage)

    def test_reads_description_across_supported_loaders(self) -> None:
        metadata_cases = {
            "forge.jar": (
                "META-INF/mods.toml",
                'modLoader="javafml"\n[[mods]]\nmodId="example"\ndescription="Forge example description."\n',
                "Forge example description.",
            ),
            "fabric.jar": (
                "fabric.mod.json",
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "example",
                        "name": "Fabric Example",
                        "description": "Fabric example description.",
                    }
                ),
                "Fabric example description.",
            ),
            "quilt.jar": (
                "quilt.mod.json",
                json.dumps(
                    {
                        "quilt_loader": {
                            "id": "example",
                            "metadata": {
                                "name": "Quilt Example",
                                "description": "Quilt example description.",
                            },
                        }
                    }
                ),
                "Quilt example description.",
            ),
        }

        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for filename, (metadata_path, metadata_text, description) in metadata_cases.items():
                with self.subTest(filename=filename):
                    with zipfile.ZipFile(directory / filename, "w") as archive:
                        archive.writestr(metadata_path, metadata_text)
                    mod = Mod_MC(Mod_Config(name=filename, directory=directory))

                    mod.sync_metadata()

                    self.assertEqual(mod.description, description)

    def test_resolves_forge_version_placeholder_from_jar_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "example-forge-1.20.1-1.4.2.jar"
            with zipfile.ZipFile(directory / filename, "w") as archive:
                archive.writestr(
                    "META-INF/mods.toml",
                    'modLoader="javafml"\n[[mods]]\nmodId="example"\n'
                    'version="${file.jarVersion}"\ndisplayName="Example"\n',
                )
                archive.writestr(
                    "META-INF/MANIFEST.MF",
                    "Manifest-Version: 1.0\nImplementation-Version: 7.6.5\n",
                )
            mod = Mod_MC(Mod_Config(name=filename, directory=directory))

            mod.sync_metadata()

        self.assertEqual(mod.version, "7.6.5")

    def test_falls_back_to_filename_when_embedded_version_is_unresolved(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "example-forge-1.20.1-1.4.2.jar"
            with zipfile.ZipFile(directory / filename, "w") as archive:
                archive.writestr(
                    "META-INF/mods.toml",
                    'modLoader="javafml"\n[[mods]]\nmodId="example"\n'
                    'version="${file.jarVersion}"\ndisplayName="Example"\n',
                )
            mod = Mod_MC(Mod_Config(name=filename, directory=directory))

            mod.sync_metadata()

        self.assertEqual(mod.version, "1.4.2")

    def test_reads_metadata_from_disabled_mod_archive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "example-forge-1.20.1-1.0.0.jar"
            disabled_path = (directory / filename).with_suffix(".disabled")
            with zipfile.ZipFile(disabled_path, "w") as archive:
                archive.writestr(
                    "META-INF/mods.toml",
                    'modLoader="javafml"\n[[mods]]\nmodId="example"\ndisplayName="Disabled Example"\n',
                )

            mod = Mod_MC(Mod_Config(name=filename, directory=directory, enabled=False))
            mod.sync_metadata()

            self.assertEqual(mod.friendly, "Disabled Example")
            self.assertFalse(mod.cfg.enabled)

    def test_malformed_embedded_metadata_falls_back_to_filename(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            filename = "FallbackName-forge-1.20.1-1.0.0.jar"
            mod_path = directory / filename
            with zipfile.ZipFile(mod_path, "w") as archive:
                archive.writestr("META-INF/mods.toml", "this is not = valid toml [[[")

            mod = Mod_MC(Mod_Config(name=filename, directory=directory))
            mod.sync_metadata()

            self.assertEqual(mod.friendly, "Fallback Name")

    def test_squaremap_mod_uses_server_side_classification(self) -> None:
        mod = Mod_MC(Mod_Config(name="squaremap-forge-mc1.20.1-1.2.0.jar", directory=Path(".")))
        mod.sync_metadata()

        self.assertIs(mod.mod_type, ModType.SERVER)
        self.assertIsNone(mod.cfg.download_block_reason)

    def test_started_embed_includes_squaremap_link(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.scope = "minecraft"
            app.directory = directory
            app.manage_embed_color = 0x22C55E
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                    Mod_MC(Mod_Config(name="squaremap-forge-mc1.20.1-1.2.0.jar", directory=directory / "mods"))
                    ]
                ),
            )
            app.cfg = Minecraft_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=directory,
                apps_dir=directory,
                scope="minecraft",
                join_host="play.example.com",
                join_port=25565,
            )

            with patch.object(config, "PUBLIC_BASE_URL", "https://maps.example.com"):
                embed = build_app_lifecycle_embed(app, started=True)

        self.assertEqual(
            embed.description,
            "Join: `play.example.com:25565`\n[Squaremap](https://maps.example.com/squaremap/?world=minecraft_overworld)",
        )

    def test_started_embed_omits_squaremap_link_when_mod_is_disabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.friendly = "Minecraft Alpha"
            app.scope = "minecraft"
            app.directory = directory
            app.manage_embed_color = 0x22C55E
            disabled_squaremap_mod = Mod_MC(
                Mod_Config(
                    name="squaremap-forge-mc1.20.1-1.2.0.jar",
                    directory=directory / "mods",
                    enabled=False,
                )
            )
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [] if state is True else [disabled_squaremap_mod]
                ),
            )
            app.cfg = Minecraft_Config(
                name=app.name,
                instance_key="alpha",
                friendly_name=app.friendly,
                directory=directory,
                apps_dir=directory,
                scope="minecraft",
                join_host="play.example.com",
                join_port=25565,
            )

            with patch.object(config, "PUBLIC_BASE_URL", "https://maps.example.com"):
                embed = build_app_lifecycle_embed(app, started=True)

        self.assertEqual(embed.description, "Join: `play.example.com:25565`")


class MinecraftBackgroundTaskCancellationTests(unittest.TestCase):
    def test_delete_save_file_removes_current_world_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            world_dir = root / "world"
            world_dir.mkdir()
            (world_dir / "level.dat").write_text("save-data", encoding="utf-8")
            app = cast(Any, object.__new__(Minecraft))
            app.directory = root
            app.settings = None
            app.check_running = lambda: False

            deleted = app.delete_save_file(file_id="world/world")

            self.assertEqual(deleted.id, "world/world")
            self.assertFalse(world_dir.exists())

    def test_sync_kubejs_yuki_log_script_creates_bundled_script_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=state is not False,
                            )
                        )
                    ]
                ),
            )

            changed = app._sync_kubejs_yuki_log_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"

            self.assertTrue(changed)
            self.assertTrue(target_path.exists())
            script_source = target_path.read_text(encoding="utf-8")
            self.assertIn("[YUKI_MC_EVENT]", script_source)
            self.assertNotIn("PlayerEvents.advancement", script_source)

    def test_sync_kubejs_yuki_log_script_does_nothing_without_enabled_kubejs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: []
                    if state is True
                    else [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=False,
                            )
                        )
                    ]
                ),
            )

            changed = app._sync_kubejs_yuki_log_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"

            self.assertFalse(changed)
            self.assertFalse(target_path.exists())

    def test_sync_kubejs_yuki_log_script_uses_embedded_fallback_when_resource_is_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=state is not False,
                            )
                        )
                    ]
                ),
            )

            with patch.object(
                minecraft,
                "_KUBEJS_YUKI_LOG_SOURCE_PATH",
                directory / "missing" / "yuki_log.js",
            ):
                changed = app._sync_kubejs_yuki_log_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_log.js"

            self.assertTrue(changed)
            self.assertTrue(target_path.exists())
            self.assertIn("[YUKI_MC_EVENT]", target_path.read_text(encoding="utf-8"))

    def test_kubejs_recipe_support_status_detects_addons_and_unification(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        ),
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-create-1.0.0.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        ),
                        Mod_MC(
                            Mod_Config(
                                name="almostunified-1.0.0.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        ),
                    ]
                ),
            )

            status = app.kubejs_recipe_support_status()

            self.assertTrue(status.kubejs_enabled)
            self.assertEqual(tuple(addon.kind for addon in status.addons), (KubeJsRecipeAddonKind.CREATE,))
            self.assertEqual(status.addon_display_names, ("KubeJS Create",))
            self.assertEqual(status.unification_mode, MinecraftRecipeUnificationMode.EXPECTED_PRESENT)
            self.assertEqual(status.script_path, directory / "kubejs" / "server_scripts" / "yuki_recipes.js")

    def test_managed_kubejs_recipe_script_renders_vanilla_mutations(self) -> None:
        status = KubeJsRecipeSupportStatus(
            kubejs_enabled=True,
            script_path=Path("kubejs/server_scripts/yuki_recipes.js"),
            script_exists=False,
        )
        script_content = _managed_kubejs_recipe_script_source(
            status,
            mutations=(
                MinecraftRecipeRemoval(
                    MinecraftRecipeRemovalFilter(
                        output=MinecraftRecipeIngredient.item("minecraft:stone_pickaxe"),
                        recipe_type=MinecraftRecipeKind.SHAPED,
                    )
                ),
                MinecraftShapelessRecipe(
                    output=MinecraftRecipeItemStack("minecraft:gravel"),
                    ingredients=(MinecraftRecipeIngredient.item("minecraft:flint", count=3),),
                    recipe_id="kubejs:flint_to_gravel",
                ),
                MinecraftShapedRecipe(
                    output=MinecraftRecipeItemStack("minecraft:blast_furnace"),
                    pattern=("III", "IFI", "SSS"),
                    key={
                        "I": MinecraftRecipeIngredient.item("minecraft:iron_ingot"),
                        "F": MinecraftRecipeIngredient.item("minecraft:furnace"),
                        "S": MinecraftRecipeIngredient.item("minecraft:smooth_stone"),
                    },
                ),
                MinecraftCookingRecipe(
                    kind=MinecraftRecipeKind.SMELTING,
                    output=MinecraftRecipeItemStack("minecraft:stone", count=2),
                    ingredient=MinecraftRecipeIngredient.item("minecraft:cobblestone"),
                    experience=0.1,
                    cooking_time_ticks=100,
                ),
                MinecraftStonecuttingRecipe(
                    output=MinecraftRecipeItemStack("minecraft:stick", count=3),
                    ingredient=MinecraftRecipeIngredient.tag("minecraft:planks"),
                ),
            ),
        )

        self.assertIn(
            'event.remove({"output": "minecraft:stone_pickaxe", "type": "minecraft:crafting_shaped"})',
            script_content,
        )
        self.assertIn(
            'event.shapeless("minecraft:gravel", ["3x minecraft:flint"]).id("kubejs:flint_to_gravel")',
            script_content,
        )
        self.assertIn(
            'event.shaped("minecraft:blast_furnace", ["III", "IFI", "SSS"], '
            '{"F": "minecraft:furnace", "I": "minecraft:iron_ingot", "S": "minecraft:smooth_stone"})',
            script_content,
        )
        self.assertIn(
            'event.smelting("2x minecraft:stone", "minecraft:cobblestone").xp(0.1).cookingTime(100)',
            script_content,
        )
        self.assertIn('event.stonecutting("3x minecraft:stick", "#minecraft:planks")', script_content)

    def test_shaped_recipe_validation_rejects_missing_key_symbols(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing key symbols"):
            MinecraftShapedRecipe(
                output=MinecraftRecipeItemStack("minecraft:stone"),
                pattern=("AB",),
                key={"A": MinecraftRecipeIngredient.item("minecraft:dirt")},
            )

    def test_kubejs_recipe_book_save_and_load_uses_separate_yukibot_data_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            recipe_book = MinecraftRecipeBook(
                mutations=(
                    MinecraftShapelessRecipe(
                        output=MinecraftRecipeItemStack("minecraft:gravel"),
                        ingredients=(MinecraftRecipeIngredient.item("minecraft:flint", count=3),),
                        recipe_id="kubejs:flint_to_gravel",
                    ),
                )
            )

            app.save_kubejs_recipe_book(recipe_book)
            loaded_recipe_book = app.load_kubejs_recipe_book()
            recipe_book_path = directory / ".yukibot" / "recipes.json"

            self.assertTrue(recipe_book_path.exists())
            self.assertFalse((directory / "kubejs" / "recipes.json").exists())
            self.assertEqual(loaded_recipe_book.to_mapping(), recipe_book.to_mapping())

    def test_load_kubejs_recipe_book_fails_loudly_for_invalid_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            recipe_book_path = directory / ".yukibot" / "recipes.json"
            recipe_book_path.parent.mkdir()
            recipe_book_path.write_text("{broken", encoding="utf-8")
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory

            with self.assertRaisesRegex(ValueError, "Invalid Minecraft recipe book JSON"):
                app.load_kubejs_recipe_book()

    def test_load_kubejs_item_registry_reads_separate_yukibot_registry_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            item_registry_path = directory / ".yukibot" / "registries" / "items.json"
            item_registry_path.parent.mkdir(parents=True)
            item_registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generated_at_epoch_ms": 1234567890,
                        "item_ids": ["minecraft:stone", "minecraft:dirt", "minecraft:stone"],
                        "block_item_ids": ["minecraft:stone"],
                    }
                ),
                encoding="utf-8",
            )
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory

            item_registry = app.load_kubejs_item_registry()

            self.assertEqual(
                item_registry,
                MinecraftItemRegistrySnapshot(
                    generated_at_epoch_ms=1234567890,
                    item_ids=("minecraft:dirt", "minecraft:stone"),
                    block_item_ids=("minecraft:stone",),
                    item_types_classified=True,
                ),
            )

    def test_minecraft_item_registry_snapshot_accepts_legacy_unclassified_payload(self) -> None:
        item_registry = MinecraftItemRegistrySnapshot.from_mapping(
            {
                "schema_version": 1,
                "generated_at_epoch_ms": 1234567890,
                "item_ids": ["minecraft:stone"],
            }
        )

        self.assertFalse(item_registry.item_types_classified)
        self.assertEqual(item_registry.block_item_ids, ())
        self.assertNotIn("block_item_ids", item_registry.to_mapping())

    def test_load_kubejs_item_registry_fails_loudly_for_invalid_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            item_registry_path = directory / ".yukibot" / "registries" / "items.json"
            item_registry_path.parent.mkdir(parents=True)
            item_registry_path.write_text("{broken", encoding="utf-8")
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory

            with self.assertRaisesRegex(ValueError, "Invalid Minecraft item registry JSON"):
                app.load_kubejs_item_registry()

    def test_resolve_minecraft_item_icon_path_caches_loose_kubejs_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            asset_path = directory / "kubejs" / "assets" / "minecraft" / "textures" / "item" / "dirt.png"
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(b"png-bits")
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(Any, SimpleNamespace(list_mods=lambda state=None: []))

            resolved_path = app.resolve_minecraft_item_icon_path("minecraft:dirt")

            self.assertEqual(
                resolved_path,
                directory / ".yukibot" / "assets" / "item_icons" / "minecraft" / "dirt.png",
            )
            assert resolved_path is not None
            self.assertEqual(resolved_path.read_bytes(), b"png-bits")

    def test_resolve_minecraft_item_icon_path_extracts_enabled_mod_jar_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            mods_directory = directory / "mods"
            mods_directory.mkdir(parents=True, exist_ok=True)
            mod_path = mods_directory / "create-1.0.0.jar"
            with zipfile.ZipFile(mod_path, "w") as archive:
                archive.writestr("assets/create/textures/item/andesite_alloy.png", b"create-png")
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [Mod_MC(Mod_Config(name=mod_path.name, directory=mods_directory, enabled=True))]
                ),
            )

            resolved_path = app.resolve_minecraft_item_icon_path("create:andesite_alloy")

            self.assertEqual(
                resolved_path,
                directory / ".yukibot" / "assets" / "item_icons" / "create" / "andesite_alloy.png",
            )
            assert resolved_path is not None
            self.assertEqual(resolved_path.read_bytes(), b"create-png")

    def test_minecraft_resource_paths_reject_filesystem_traversal(self) -> None:
        app = object.__new__(Minecraft)

        for item_id in ("minecraft:../../private/avatar", "minecraft:/private/avatar"):
            with self.subTest(item_id=item_id):
                with self.assertRaisesRegex(ValueError, "invalid resource path"):
                    app.resolve_minecraft_item_icon_path(item_id)

    def test_minecraft_item_archive_index_is_built_once_for_concurrent_icon_requests(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            archive_path = directory / "example.jar"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("assets/example/textures/item/widget.png", b"widget")
            app = object.__new__(Minecraft)
            app._minecraft_item_icon_archive_cache_lock = threading.Lock()
            start_barrier = threading.Barrier(8)

            def build_index() -> dict[str, tuple[Path, ...]]:
                start_barrier.wait()
                return app._minecraft_item_icon_archive_paths_by_namespace()

            with patch.object(
                Minecraft,
                "_minecraft_item_icon_archive_candidates",
                return_value=(archive_path,),
            ) as archive_candidates:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    indexes = tuple(executor.map(lambda _index: build_index(), range(8)))

            self.assertEqual(archive_candidates.call_count, 1)
            self.assertTrue(all(index["example"] == (archive_path,) for index in indexes))

    def test_generated_recipe_ids_use_player_output_and_collision_suffixes(self) -> None:
        recipe_id = generated_minecraft_recipe_id(
            minecraft_username="YukiPlayer",
            output_item_id="minecraft:stone",
            existing_recipe_ids={"yukibot:yukiplayer/minecraft/stone"},
        )
        removal_id = generated_minecraft_recipe_mutation_id(
            minecraft_username="YukiPlayer",
            mutation=MinecraftRecipeRemoval(
                MinecraftRecipeRemovalFilter(output=MinecraftRecipeIngredient.tag("c:iron_ingots"))
            ),
            existing_recipe_ids=(),
        )

        self.assertEqual(recipe_id, "yukibot:yukiplayer/minecraft/stone_2")
        self.assertEqual(removal_id, "yukibot:yukiplayer/remove/output/tag/c/iron_ingots")

    def test_removal_recipe_type_is_validated_before_persistence(self) -> None:
        with self.assertRaisesRegex(ValueError, "recipe type must be a namespaced Minecraft id"):
            MinecraftRecipeRemovalFilter(recipe_type="crafting_shaped")

    def test_sync_kubejs_recipe_script_creates_managed_scaffold_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        ),
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-immersive-engineering-1.0.0.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        ),
                    ]
                ),
            )

            changed = app._sync_kubejs_recipe_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_recipes.js"
            recipe_book_path = directory / ".yukibot" / "recipes.json"
            script_content = target_path.read_text(encoding="utf-8")

            self.assertTrue(changed)
            self.assertTrue(target_path.exists())
            self.assertTrue(recipe_book_path.exists())
            self.assertIn("Managed by YukiBot", script_content)
            self.assertIn("ServerEvents.recipes", script_content)
            self.assertIn("KubeJS Immersive Engineering", script_content)

    def test_sync_kubejs_item_registry_script_creates_managed_startup_script_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        )
                    ]
                ),
            )

            changed = app._sync_kubejs_item_registry_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_item_registry.js"
            registry_directory = directory / ".yukibot" / "registries"
            script_content = target_path.read_text(encoding="utf-8")

            self.assertTrue(changed)
            self.assertTrue(target_path.exists())
            self.assertTrue(registry_directory.exists())
            self.assertNotIn("StartupEvents.postInit", script_content)
            self.assertIn(".yukibot/registries/items.json", script_content)
            self.assertIn("block_item_ids", script_content)
            self.assertIn("BuiltInRegistries.BLOCK.containsKey", script_content)

    def test_migrate_legacy_yukibot_data_copies_recipe_book_and_item_registry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            legacy_recipe_book_path = directory / "yukibot" / "recipes.json"
            legacy_recipe_book_path.parent.mkdir(parents=True)
            legacy_recipe_book_path.write_text('{"schema_version":1,"mutations":[]}\n', encoding="utf-8")
            legacy_item_registry_path = directory / "yukibot" / "registries" / "items.json"
            legacy_item_registry_path.parent.mkdir(parents=True)
            legacy_item_registry_path.write_text(
                '{"schema_version":1,"generated_at_epoch_ms":1,"item_ids":["minecraft:stone"]}\n',
                encoding="utf-8",
            )
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory

            app._migrate_legacy_yukibot_data()

            self.assertEqual(
                (directory / ".yukibot" / "recipes.json").read_text(encoding="utf-8"),
                legacy_recipe_book_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (directory / ".yukibot" / "registries" / "items.json").read_text(encoding="utf-8"),
                legacy_item_registry_path.read_text(encoding="utf-8"),
            )

    def test_sync_kubejs_recipe_script_uses_persisted_recipe_book(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        )
                    ]
                ),
            )
            app.save_kubejs_recipe_book(
                MinecraftRecipeBook(
                    mutations=(
                        MinecraftRecipeRemoval(
                            MinecraftRecipeRemovalFilter(recipe_id="minecraft:stick"),
                        ),
                        MinecraftStonecuttingRecipe(
                            output=MinecraftRecipeItemStack("minecraft:stick", count=3),
                            ingredient=MinecraftRecipeIngredient.tag("minecraft:planks"),
                        ),
                    )
                )
            )

            changed = app._sync_kubejs_recipe_script()
            script_content = (directory / "kubejs" / "server_scripts" / "yuki_recipes.js").read_text(encoding="utf-8")

            self.assertTrue(changed)
            self.assertIn('event.remove({"id": "minecraft:stick"})', script_content)
            self.assertIn('event.stonecutting("3x minecraft:stick", "#minecraft:planks")', script_content)

    def test_append_kubejs_recipe_mutation_persists_and_regenerates_script(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        )
                    ]
                ),
            )

            recipe_book = app.append_kubejs_recipe_mutation(
                MinecraftShapelessRecipe(
                    output=MinecraftRecipeItemStack("minecraft:gravel"),
                    ingredients=(MinecraftRecipeIngredient.item("minecraft:flint", count=3),),
                    recipe_id="kubejs:flint_to_gravel",
                )
            )
            loaded_recipe_book = app.load_kubejs_recipe_book()
            script_content = (directory / "kubejs" / "server_scripts" / "yuki_recipes.js").read_text(encoding="utf-8")

            self.assertEqual(len(recipe_book.mutations), 1)
            self.assertEqual(loaded_recipe_book.to_mapping(), recipe_book.to_mapping())
            self.assertIn(
                'event.shapeless("minecraft:gravel", ["3x minecraft:flint"]).id("kubejs:flint_to_gravel")',
                script_content,
            )

    def test_recipe_mutation_rolls_back_book_when_script_write_fails(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        )
                    ]
                ),
            )

            with patch.object(Minecraft, "_write_kubejs_recipe_script", side_effect=OSError("read-only")):
                with self.assertRaisesRegex(OSError, "recipe changes were rolled back"):
                    app.append_kubejs_recipe_mutation(
                        MinecraftShapelessRecipe(
                            output=MinecraftRecipeItemStack("minecraft:gravel"),
                            ingredients=(MinecraftRecipeIngredient.item("minecraft:flint"),),
                            recipe_id="yukibot:yuki/minecraft/gravel",
                        )
                    )

            self.assertEqual(app.load_kubejs_recipe_book().mutations, ())

    def test_replace_and_remove_kubejs_recipe_mutation_persist_and_regenerate_script(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=True,
                            )
                        )
                    ]
                ),
            )

            app.append_kubejs_recipe_mutation(
                MinecraftShapelessRecipe(
                    output=MinecraftRecipeItemStack("minecraft:gravel"),
                    ingredients=(MinecraftRecipeIngredient.item("minecraft:flint"),),
                    recipe_id="kubejs:flint_to_gravel",
                )
            )
            replaced_recipe_book = app.replace_kubejs_recipe_mutation(
                0,
                MinecraftShapelessRecipe(
                    output=MinecraftRecipeItemStack("minecraft:sand"),
                    ingredients=(MinecraftRecipeIngredient.item("minecraft:gravel"),),
                    recipe_id="kubejs:gravel_to_sand",
                ),
            )
            removed_recipe_book = app.remove_kubejs_recipe_mutation(0)
            final_script_content = (directory / "kubejs" / "server_scripts" / "yuki_recipes.js").read_text(encoding="utf-8")

            self.assertEqual(len(replaced_recipe_book.mutations), 1)
            self.assertEqual(replaced_recipe_book.mutations[0].to_mapping()["id"], "kubejs:gravel_to_sand")
            self.assertEqual(removed_recipe_book.mutations, ())
            self.assertNotIn("kubejs:flint_to_gravel", final_script_content)
            self.assertNotIn("kubejs:gravel_to_sand", final_script_content)

    def test_sync_kubejs_recipe_script_does_nothing_without_enabled_kubejs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: []
                    if state is True
                    else [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=False,
                            )
                        )
                    ]
                ),
            )

            changed = app._sync_kubejs_recipe_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_recipes.js"

            self.assertFalse(changed)
            self.assertFalse(target_path.exists())

    def test_sync_kubejs_item_registry_script_does_nothing_without_enabled_kubejs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            app = object.__new__(Minecraft)
            app.name = "minecraft_alpha"
            app.directory = directory
            app.mods = cast(
                Any,
                SimpleNamespace(
                    list_mods=lambda state=None: []
                    if state is True
                    else [
                        Mod_MC(
                            Mod_Config(
                                name="kubejs-forge-2001.6.5-build.26.jar",
                                directory=directory / "mods",
                                enabled=False,
                            )
                        )
                    ]
                ),
            )

            changed = app._sync_kubejs_item_registry_script()
            target_path = directory / "kubejs" / "server_scripts" / "yuki_item_registry.js"

            self.assertFalse(changed)
            self.assertFalse(target_path.exists())

    def test_load_squaremap_web_address_accepts_quoted_value_with_comment(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yml"
            config_path.write_text('web-address: "http://localhost:8080" # dev web root\n', encoding="utf-8")

            self.assertEqual(_load_squaremap_web_address(config_path), "http://localhost:8080")

    def test_cancel_background_task_handles_foreign_event_loop(self) -> None:
        app = object.__new__(Minecraft)
        app.name = "minecraft_alpha"

        task_ready = threading.Event()
        task_holder: dict[str, asyncio.Task[object]] = {}

        def run_task_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def wait_forever() -> None:
                current_task = asyncio.current_task()
                if current_task is None:
                    raise RuntimeError("Expected the background task to be available.")
                task_holder["task"] = current_task
                task_ready.set()
                await asyncio.Future()

            task = loop.create_task(wait_forever())
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()

        thread = threading.Thread(target=run_task_loop)
        thread.start()
        self.assertTrue(task_ready.wait(timeout=1.0))

        task = task_holder["task"]
        asyncio.run(app._cancel_background_task(task, label="test background task", timeout_seconds=1.0))

        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(task.done())


class MinecraftActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_minecraft_activities_stop_deregisters_registered_provider(self) -> None:
        activity_manager = _RecordingActivityManager()

        async def background_worker() -> Never:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel_background_task(
            task: asyncio.Task[None],
            *,
            label: str,
            timeout_seconds: float = 5.0,
        ) -> None:
            del label, timeout_seconds
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        app = SimpleNamespace(
            activity_manager=activity_manager,
            _cancel_background_task=cancel_background_task,
            providers=[],
        )
        app.set_activity_providers = lambda providers: setattr(app, "providers", list(providers))
        app.register_enabled_activity_providers = lambda: [activity_manager.register(provider) for provider in app.providers]
        app.deregister_activity_providers = lambda: [
            activity_manager.deregister(provider) for provider in app.providers
        ]
        activities = MinecraftActivities(cast(Any, app))
        provider = activities.providers[0]
        provider.task_funcs = [background_worker]

        await activities.start()
        await activities.stop()

        self.assertEqual(activity_manager.registered, [provider])
        self.assertEqual(activity_manager.deregistered, [provider])
        self.assertEqual(activities.tasks, set())


if __name__ == "__main__":
    unittest.main()
