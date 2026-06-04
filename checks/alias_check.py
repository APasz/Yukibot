from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, create_autospec, patch

import hikari

import cmd_alias
import config
from _manager import App_Manager
from _security import Power_Level


class _FakeAcl:
    LvL = Power_Level

    async def perm_check(self, user_id: int, required: Power_Level) -> bool:
        del user_id, required
        return True


def _component_custom_ids(components: list[hikari.api.MessageActionRowBuilder]) -> tuple[str, ...]:
    custom_ids: list[str] = []
    for row in components:
        for component in row.components:
            custom_id = getattr(cast(object, component), "custom_id", None)
            if isinstance(custom_id, str):
                custom_ids.append(custom_id)
    return tuple(custom_ids)


class AliasTargetResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_numeric_target_creates_manual_name_cache_entry(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {}
            cache.by_alias = {}
            cache.by_platform_id = {}

            user_id = await cmd_alias._resolve_alias_target_user_id(
                actor_user_id=1,
                requested_user="123456789012345678",
                target_display_name="Web Alice",
                acl=_FakeAcl(),  # type: ignore[arg-type]
                names_cache=cache,
            )

            self.assertEqual(user_id, 123456789012345678)
            self.assertTrue(cache.is_manual_user(user_id))
            self.assertEqual(cache.cached_display_name(user_id), "Web Alice")

    async def test_manual_name_is_rejected_for_resolved_target(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {
                123: config.UserNames(
                    account="tester",
                    global_name="Tester",
                    names={"tester", "Tester"},
                )
            }
            cache.by_alias = {}
            cache.by_platform_id = {}
            cache._rebuild_aliases()

            with self.assertRaisesRegex(
                ValueError,
                r"`manual_name` is only valid for a raw Discord user ID that is not already in the name cache\.",
            ):
                await cmd_alias._resolve_alias_target_user_id(
                    actor_user_id=1,
                    requested_user="Tester",
                    target_display_name="Web Tester",
                    acl=_FakeAcl(),  # type: ignore[arg-type]
                    names_cache=cache,
                )

    async def test_display_override_modal_sets_web_override(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {}
            cache.by_alias = {}
            cache.by_platform_id = {}
            manager = create_autospec(App_Manager, instance=True)
            manager.apps = {}
            service = cmd_alias.AliasEditorService()

            response = await service._on_display_override_modal_submit(
                SimpleNamespace(
                    action=service._action_codec.build(
                        cmd_alias.AliasActionKind.SET_DISPLAY_OVERRIDE,
                        page=0,
                        value=config.DisplayNameCategory.WEB.value,
                    ),
                    user_id=42,
                    scope_id=42,
                    values={cmd_alias._DISPLAY_OVERRIDE_MODAL_FIELD_ID: "Portal Alice"},
                    interaction=SimpleNamespace(locale=hikari.Locale.EN_US, guild_locale=hikari.Locale.EN_US),
                ),
                {"names_cache": cache, "manager": manager},
            )

            self.assertIsNotNone(response)
            self.assertEqual(cache.get_display_override(42, config.DisplayNameCategory.WEB), "Portal Alice")

    async def test_steam_modal_sets_platform_id(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {}
            cache.by_alias = {}
            cache.by_platform_id = {}
            manager = create_autospec(App_Manager, instance=True)
            manager.apps = {}
            service = cmd_alias.AliasEditorService()

            response = await service._on_steam_modal_submit(
                SimpleNamespace(
                    action=service._build_section_state_action(
                        cmd_alias.AliasActionKind.SET_STEAM,
                        cmd_alias.AliasEditorState(section=cmd_alias.AliasEditorSection.LINKED_ACCOUNTS, page=0),
                    ),
                    user_id=42,
                    scope_id=42,
                    values={cmd_alias._STEAM_MODAL_FIELD_ID: "76561198000000001"},
                    interaction=SimpleNamespace(locale=hikari.Locale.EN_US, guild_locale=hikari.Locale.EN_US),
                ),
                {"names_cache": cache, "manager": manager},
            )

            self.assertIsNotNone(response)
            self.assertEqual(cache.get_platform_id(42, "steam"), "76561198000000001")

    async def test_steam_input_accepts_profiles_url(self) -> None:
        service = cmd_alias.AliasEditorService()

        result = await service._resolve_steam_input("https://steamcommunity.com/profiles/76561198215517873/")

        self.assertEqual(result, "76561198215517873")

    async def test_steam_input_resolves_vanity_url(self) -> None:
        service = cmd_alias.AliasEditorService()

        with patch.object(service, "_resolve_steam_vanity", new=AsyncMock(return_value="76561198215517873")) as resolve_mock:
            result = await service._resolve_steam_input("https://steamcommunity.com/id/APasz/")

        self.assertEqual(result, "76561198215517873")
        resolve_mock.assert_awaited_once_with("APasz")

    async def test_minecraft_profile_modal_sets_alias_and_uuid(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {}
            cache.by_alias = {}
            cache.by_platform_id = {}
            manager = create_autospec(App_Manager, instance=True)
            manager.apps = {}
            service = cmd_alias.AliasEditorService()

            response = await service._on_minecraft_profile_modal_submit(
                SimpleNamespace(
                    action=service._build_section_state_action(
                        cmd_alias.AliasActionKind.SET_MINECRAFT_PROFILE,
                        cmd_alias.AliasEditorState(section=cmd_alias.AliasEditorSection.LINKED_ACCOUNTS, page=0),
                    ),
                    user_id=42,
                    scope_id=42,
                    values={
                        cmd_alias._MINECRAFT_PROFILE_NAME_FIELD_ID: "Alice",
                        cmd_alias._MINECRAFT_PROFILE_UUID_FIELD_ID: "123E4567E89B12D3A456426614174000",
                    },
                    interaction=SimpleNamespace(locale=hikari.Locale.EN_US, guild_locale=hikari.Locale.EN_US),
                ),
                {"names_cache": cache, "manager": manager},
            )

            self.assertIsNotNone(response)
            self.assertEqual(
                cache.by_id[42].games["minecraft"],
                ("Alice", "123e4567-e89b-12d3-a456-426614174000"),
            )

    async def test_minecraft_profile_modal_looks_up_uuid_from_username(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {}
            cache.by_alias = {}
            cache.by_platform_id = {}
            manager = create_autospec(App_Manager, instance=True)
            manager.apps = {}
            service = cmd_alias.AliasEditorService()

            with patch.object(
                service,
                "_lookup_minecraft_profiles",
                new=AsyncMock(
                    return_value=(
                        cmd_alias.MinecraftLookupCandidate(
                            name="APasz",
                            uuid="123e4567-e89b-12d3-a456-426614174000",
                        ),
                    )
                ),
            ):
                response = await service._on_minecraft_profile_modal_submit(
                    SimpleNamespace(
                        action=service._action_codec.build(cmd_alias.AliasActionKind.SET_MINECRAFT_PROFILE, page=0),
                        user_id=42,
                        scope_id=42,
                        values={
                            cmd_alias._MINECRAFT_PROFILE_NAME_FIELD_ID: "apasz",
                            cmd_alias._MINECRAFT_PROFILE_UUID_FIELD_ID: "",
                        },
                        interaction=SimpleNamespace(locale=hikari.Locale.EN_US, guild_locale=hikari.Locale.EN_US),
                    ),
                    {"names_cache": cache, "manager": manager},
                )

            self.assertIsNotNone(response)
            self.assertEqual(
                cache.by_id[42].games["minecraft"],
                ("APasz", "123e4567-e89b-12d3-a456-426614174000"),
            )

    async def test_minecraft_profile_lookup_multiple_results_requires_selection(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {}
            cache.by_alias = {}
            cache.by_platform_id = {}
            manager = create_autospec(App_Manager, instance=True)
            manager.apps = {}
            service = cmd_alias.AliasEditorService()
            candidates = (
                cmd_alias.MinecraftLookupCandidate("Alice", "123e4567-e89b-12d3-a456-426614174000"),
                cmd_alias.MinecraftLookupCandidate("Alice_", "123e4567-e89b-12d3-a456-426614174001"),
            )

            with patch.object(service, "_lookup_minecraft_profiles", new=AsyncMock(return_value=candidates)):
                response = await service._on_minecraft_profile_modal_submit(
                    SimpleNamespace(
                        action=service._action_codec.build(cmd_alias.AliasActionKind.SET_MINECRAFT_PROFILE, page=0),
                        user_id=42,
                        scope_id=42,
                        values={
                            cmd_alias._MINECRAFT_PROFILE_NAME_FIELD_ID: "alice",
                            cmd_alias._MINECRAFT_PROFILE_UUID_FIELD_ID: "",
                        },
                        interaction=SimpleNamespace(locale=hikari.Locale.EN_US, guild_locale=hikari.Locale.EN_US),
                    ),
                    {"names_cache": cache, "manager": manager},
                )

            self.assertIsNotNone(response)
            self.assertEqual(
                service._pending_minecraft_lookup_candidates(42, 42),
                candidates,
            )

            choose_response = await service._on_editor_action(
                SimpleNamespace(
                    action=service._action_codec.build(cmd_alias.AliasActionKind.CHOOSE_MINECRAFT_LOOKUP, page=0),
                    user_id=42,
                    scope_id=42,
                    values=("123e4567-e89b-12d3-a456-426614174001",),
                    locale=hikari.Locale.EN_US,
                    interaction=SimpleNamespace(),
                ),
                {"names_cache": cache, "manager": manager},
            )

            self.assertIsNotNone(choose_response)
            self.assertEqual(
                cache.by_id[42].games["minecraft"],
                ("Alice_", "123e4567-e89b-12d3-a456-426614174001"),
            )

    async def test_editor_action_opens_minecraft_profile_modal(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {}
            cache.by_alias = {}
            cache.by_platform_id = {}
            manager = create_autospec(App_Manager, instance=True)
            manager.apps = {}
            service = cmd_alias.AliasEditorService()
            interaction = SimpleNamespace(create_modal_response=AsyncMock())

            response = await service._on_editor_action(
                SimpleNamespace(
                    action=service._action_codec.build(cmd_alias.AliasActionKind.SET_MINECRAFT_PROFILE, page=0),
                    user_id=42,
                    scope_id=42,
                    values=(),
                    locale=hikari.Locale.EN_US,
                    interaction=interaction,
                ),
                {"names_cache": cache, "manager": manager},
            )

            self.assertIsNone(response)
            interaction.create_modal_response.assert_awaited_once()

    def test_minecraft_profile_modal_id_fits_discord_limit(self) -> None:
        service = cmd_alias.AliasEditorService()
        user_id = hikari.Snowflake(123456789012345678)

        action = service._action_codec.build(cmd_alias.AliasActionKind.SET_MINECRAFT_PROFILE, page=0)
        modal_id = service._minecraft_profile_modal.build_id(action, scope_id=user_id, user_id=user_id)

        self.assertLessEqual(len(modal_id), 100)

    def test_app_scope_editor_component_custom_ids_are_unique(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = object.__new__(config.Name_Cache)
            cache.pointer = Path(tmp) / "discord_names.json"
            cache.by_id = {42: config.UserNames()}
            cache.by_alias = {}
            cache.by_platform_id = {}
            manager = SimpleNamespace(
                apps={
                    "valheim_alpha": SimpleNamespace(scope="valheim"),
                    "minecraft_alpha": SimpleNamespace(scope="minecraft"),
                }
            )
            service = cmd_alias.AliasEditorService()

            _embed, components = service._render_editor(
                target_user_id=42,
                actor_user_id=42,
                locale=hikari.Locale.EN_US,
                names_cache=cache,
                manager=manager,  # type: ignore[arg-type]
                state=cmd_alias.AliasEditorState(section=cmd_alias.AliasEditorSection.APP_SCOPES, page=0),
            )

        custom_ids = _component_custom_ids(components)
        self.assertEqual(len(custom_ids), len(set(custom_ids)))


if __name__ == "__main__":
    unittest.main()
