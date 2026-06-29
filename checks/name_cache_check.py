from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import create_autospec

import hikari

import config


def _make_cache(pointer: Path) -> config.Name_Cache:
    cache = object.__new__(config.Name_Cache)
    cache.pointer = pointer
    cache.by_id = {}
    cache.by_alias = {}
    cache.by_platform_id = {}
    return cache


def _make_member(
    *,
    user_id: int,
    guild_id: int,
    username: str,
    global_name: str | None,
    nickname: str | None,
    avatar_hash: str | None = None,
) -> hikari.Member:
    member = create_autospec(hikari.Member, instance=True)
    member.id = hikari.Snowflake(user_id)
    member.guild_id = hikari.Snowflake(guild_id)
    member.username = username
    member.global_name = global_name
    member.nickname = nickname
    member.avatar_hash = avatar_hash
    member.display_name = nickname or global_name or username
    return member


class NameCacheTests(unittest.TestCase):
    def test_serializable_excludes_derived_names(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                avatar_hash="avatar-123",
                names={"user-name", "global-name"},
                guild_names={100: "guild-name"},
            )

            payload = cast(dict[str, dict[str, Any]], cache.serializable())

            self.assertNotIn("names", payload["1"])
            self.assertEqual(payload["1"]["global_name"], "global-name")
            self.assertEqual(payload["1"]["avatar_hash"], "avatar-123")
            self.assertEqual(payload["1"]["guild_names"], {"100": "guild-name"})

    def test_set_names_caches_discord_avatar_hash(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")

            cache.set_names(
                _make_member(
                    user_id=1,
                    guild_id=100,
                    username="user-name",
                    global_name="global-name",
                    nickname=None,
                    avatar_hash="avatar-123",
                )
            )

            self.assertEqual(cache.by_id[1].avatar_hash, "avatar-123")
            self.assertEqual(cache.discord_avatar_hash(1), "avatar-123")

    def test_set_names_mutation_updates_cached_discord_avatar_hash(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(avatar_hash="old-avatar")

            changed = cache.apply_mutation_event(
                {
                    "kind": config.NameMutationKind.SET_NAMES.value,
                    "user_id": 1,
                    "avatar_hash": "new-avatar",
                }
            )

            self.assertTrue(changed)
            self.assertEqual(cache.discord_avatar_hash(1), "new-avatar")

    def test_serializable_includes_game_profiles_and_platform_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                games={"minecraft": ("Alice", "123e4567-e89b-12d3-a456-426614174000")},
                platform_ids={"steam": "76561198000000001"},
            )

            payload = cast(dict[str, dict[str, Any]], cache.serializable())

            self.assertEqual(
                payload["1"]["games"],
                {"minecraft": ["Alice", "123e4567-e89b-12d3-a456-426614174000"]},
            )
            self.assertEqual(payload["1"]["platform_ids"], {"steam": "76561198000000001"})

    def test_normalise_user_rebuilds_known_names_from_sources(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            entry = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"stale-name"},
                guild_names={100: "guild-name"},
            )

            changed = cache._normalise_user(entry)

            self.assertTrue(changed)
            self.assertEqual(entry.names, {"user-name", "global-name", "guild-name"})

    def test_set_names_replaces_guild_name_for_same_guild(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")

            cache.set_names(
                _make_member(
                    user_id=1,
                    guild_id=100,
                    username="user-name",
                    global_name="global-name",
                    nickname="old-guild-name",
                )
            )
            cache.set_names(
                _make_member(
                    user_id=1,
                    guild_id=100,
                    username="user-name",
                    global_name="global-name",
                    nickname="new-guild-name",
                )
            )
            cache.set_names(
                _make_member(
                    user_id=1,
                    guild_id=200,
                    username="user-name",
                    global_name="global-name",
                    nickname="other-guild-name",
                )
            )

            entry = cache.by_id[1]

            self.assertEqual(entry.global_name, "global-name")
            self.assertEqual(entry.guild_names, {100: "new-guild-name", 200: "other-guild-name"})
            self.assertEqual(entry.names, {"user-name", "global-name", "new-guild-name", "other-guild-name"})
            self.assertNotIn("old-guild-name", entry.names)
            self.assertIsNone(cache.resolve_to_id("old-guild-name"))
            self.assertEqual(cache.resolve_to_id("new-guild-name"), 1)

    def test_set_names_removes_guild_name_when_nickname_is_cleared(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")

            cache.set_names(
                _make_member(
                    user_id=1,
                    guild_id=100,
                    username="user-name",
                    global_name="global-name",
                    nickname="guild-name",
                )
            )
            cache.set_names(
                _make_member(
                    user_id=1,
                    guild_id=100,
                    username="user-name",
                    global_name="global-name",
                    nickname=None,
                )
            )

            entry = cache.by_id[1]

            self.assertEqual(entry.guild_names, {})
            self.assertEqual(entry.names, {"user-name", "global-name"})
            self.assertIsNone(cache.resolve_to_id("guild-name"))

    def test_remove_guild_name_clears_known_name(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.set_names(
                _make_member(
                    user_id=1,
                    guild_id=100,
                    username="user-name",
                    global_name="global-name",
                    nickname="guild-name",
                )
            )

            changed = cache.remove_guild_name(1, 100)

            self.assertTrue(changed)
            self.assertEqual(cache.by_id[1].guild_names, {})
            self.assertEqual(cache.by_id[1].names, {"user-name", "global-name"})
            self.assertIsNone(cache.resolve_to_id("guild-name"))

    def test_cached_display_name_prefers_discord_identity_over_guild_names(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "primary-name", "other-name"},
                guild_names={int(config.DISCORD_GUILD): "primary-name", 200: "other-name"},
            )

            self.assertEqual(cache.cached_display_name(1), "global-name [user-name]")
            self.assertEqual(cache.cached_display_name(1, preferred_guild_id=200), "global-name [user-name]")

    def test_cached_display_name_can_use_discord_override(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "guild-name"},
                guild_names={100: "guild-name"},
                display_overrides=config.DisplayNameOverrides(discord="Relay Name"),
            )

            self.assertEqual(
                cache.cached_display_name(1, category=config.DisplayNameCategory.DISCORD, preferred_guild_id=100),
                "Relay Name",
            )
            self.assertEqual(cache.cached_display_name(1, preferred_guild_id=100), "global-name [user-name]")
            self.assertEqual(cache.relay_mention_name(1, preferred_guild_id=100), "global-name")

    def test_relay_mention_name_prefers_scope_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "guild-name"},
                guild_names={100: "guild-name"},
                games={"minecraft": ("InGameName", None)},
            )

            resolved = cache.relay_mention_name(1, scope="minecraft", preferred_guild_id=100)

        self.assertEqual(resolved, "InGameName")

    def test_relay_display_name_prefers_scope_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "guild-name"},
                guild_names={100: "guild-name"},
                games={"minecraft": ("InGameName", None)},
            )

            resolved = cache.relay_display_name(1, scope="minecraft", preferred_guild_id=100)

        self.assertEqual(resolved, "InGameName")

    def test_get_game_alias_returns_none_when_user_has_no_alias_in_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name"},
            )

            resolved = cache.get_game_alias(1, "minecraft")

        self.assertIsNone(resolved)

    def test_relay_mention_name_falls_back_to_discord_global_name(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "guild-one", "guild-two"},
                guild_names={100: "guild-one", 200: "guild-two"},
            )

            resolved = cache.relay_mention_name(1, scope="minecraft", preferred_guild_id=200)

        self.assertEqual(resolved, "global-name")

    def test_relay_display_name_ignores_guild_names_when_scope_alias_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "guild-one", "guild-two"},
                guild_names={100: "guild-one", 200: "guild-two"},
            )

            resolved = cache.relay_display_name(1, scope="minecraft", preferred_guild_id=200)

        self.assertEqual(resolved, "global-name")

    def test_web_display_name_prefers_web_override_before_scope_and_discord_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "guild-name"},
                guild_names={100: "guild-name"},
                games={"steam": ("SteamName", None)},
                display_overrides=config.DisplayNameOverrides(web="Portal Name"),
            )

            resolved = cache.web_display_name(1, scope="minecraft", platforms=("steam",))

        self.assertEqual(resolved, "Portal Name")

    def test_relay_display_name_prefers_platform_alias_after_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                games={"steam": ("SteamName", None)},
            )

            resolved = cache.relay_display_name(1, scope="minecraft", platforms=("steam",))

        self.assertEqual(resolved, "SteamName")

    def test_discord_fallback_name_prefers_combined_discord_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="nameA",
                global_name="nameB",
                names={"nameA", "nameB"},
            )

            resolved = cache.discord_fallback_name(1, scope="minecraft", fallback_display_name="nameB")

        self.assertEqual(resolved, "nameB [nameA]")

    def test_command_resolution_prefers_unique_global_name_when_alias_is_ambiguous(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-one",
                global_name="shared-name",
                names={"user-one", "shared-name", "guild-name"},
                guild_names={100: "guild-name"},
            )
            cache.by_id[2] = config.UserNames(
                account="user-two",
                global_name="other-global",
                names={"user-two", "other-global", "shared-name"},
                guild_names={200: "shared-name"},
            )
            cache._rebuild_aliases()

            self.assertIsNone(cache.resolve_to_id("shared-name"))
            self.assertEqual(cache.resolve_to_id("shared-name", prefer_global_name=True), 1)
            self.assertEqual(cache.resolve_name("shared-name").status, config.NameResolutionStatus.AMBIGUOUS)
            self.assertEqual(cache.resolve_name("shared-name", prefer_global_name=True).user_id, 1)

    def test_numeric_alias_resolves_before_numeric_discord_user_id(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[123] = config.UserNames(account="discord-user", names={"discord-user"})
            cache.by_id[456] = config.UserNames(account="alias-owner", names={"alias-owner"}, nicknames={"123"})
            cache._rebuild_aliases()

            result = cache.resolve_name("123")

            self.assertEqual(result.status, config.NameResolutionStatus.UNIQUE)
            self.assertEqual(result.user_id, 456)

    def test_set_names_mutation_scoped_to_guild_preserves_other_guild_names(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="Erin",
                names={"user-name", "Erin", "guild-one", "guild-two"},
                guild_names={100: "guild-one", 200: "guild-two"},
            )
            cache._rebuild_aliases()

            changed = cache.apply_mutation_event(
                {
                    "kind": config.NameMutationKind.SET_NAMES.value,
                    "user_id": 1,
                    "account": "user-name",
                    "global_name": "Erin",
                    "guild_id": 100,
                    "guild_name": "guild-one-renamed",
                }
            )

            self.assertTrue(changed)
            self.assertEqual(cache.by_id[1].guild_names, {100: "guild-one-renamed", 200: "guild-two"})
            self.assertEqual(cache.resolve_to_id("guild-one-renamed"), 1)
            self.assertEqual(cache.resolve_to_id("guild-two"), 1)

    def test_set_names_mutation_scoped_to_guild_can_clear_single_guild_name(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="Erin",
                names={"user-name", "Erin", "guild-one", "guild-two"},
                guild_names={100: "guild-one", 200: "guild-two"},
            )
            cache._rebuild_aliases()

            changed = cache.apply_mutation_event(
                {
                    "kind": config.NameMutationKind.SET_NAMES.value,
                    "user_id": 1,
                    "guild_id": 100,
                    "guild_name": None,
                }
            )

            self.assertTrue(changed)
            self.assertEqual(cache.by_id[1].guild_names, {200: "guild-two"})
            self.assertIsNone(cache.resolve_to_id("guild-one"))
            self.assertEqual(cache.resolve_to_id("guild-two"), 1)

    def test_upsert_manual_user_creates_resolvable_display_and_game_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")

            changed = cache.upsert_manual_user(
                123,
                display_name="Web Alice",
                game_aliases={"Minecraft": "AliceGame"},
            )

            self.assertTrue(changed)
            self.assertTrue(cache.is_manual_user(123))
            self.assertEqual(cache.cached_display_name(123), "Web Alice")
            self.assertEqual(cache.resolve_to_id("Web Alice"), 123)
            self.assertEqual(cache.resolve_to_id("AliceGame", scope="minecraft"), 123)

    def test_upsert_manual_user_mutation_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")

            changed = cache.apply_mutation_event(
                {
                    "kind": config.NameMutationKind.UPSERT_MANUAL_USER.value,
                    "user_id": 123,
                    "display_name": "Web Alice",
                    "nicknames": ["Alice"],
                    "game_aliases": {"minecraft": "AliceGame"},
                }
            )

            self.assertTrue(changed)
            self.assertTrue(cache.is_manual_user(123))
            self.assertEqual(cache.resolve_to_id("Alice"), 123)
            self.assertEqual(cache.resolve_to_id("AliceGame", scope="minecraft"), 123)

    def test_set_display_override_mutation_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")

            changed = cache.apply_mutation_event(
                {
                    "kind": config.NameMutationKind.SET_DISPLAY_OVERRIDE.value,
                    "user_id": 123,
                    "category": config.DisplayNameCategory.WEB.value,
                    "display_name": "Portal Alice",
                }
            )

            self.assertTrue(changed)
            self.assertEqual(cache.get_display_override(123, config.DisplayNameCategory.WEB), "Portal Alice")

    def test_add_name_rejects_alias_used_by_another_user(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(account="user-one", names={"user-one"}, nicknames={"shared"})
            cache.by_id[2] = config.UserNames(account="user-two", names={"user-two"})
            cache._rebuild_aliases()

            with self.assertRaisesRegex(ValueError, r"General alias `shared` is already used by another user\."):
                cache.add_name(2, "shared", False)

    def test_set_game_alias_rejects_alias_used_by_another_user_in_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(games={"minecraft": ("shared", None)})
            cache.by_id[2] = config.UserNames()

            with self.assertRaisesRegex(ValueError, r"Minecraft alias `shared` is already used by another user\."):
                cache.set_game_alias(2, "minecraft", "shared")

    def test_set_platform_id_rejects_id_used_by_another_user(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(platform_ids={"steam": "76561198000000001"})
            cache.by_id[2] = config.UserNames()
            cache._rebuild_aliases()

            with self.assertRaisesRegex(ValueError, r"Steam ID `76561198000000001` is already linked to another user\."):
                cache.set_platform_id(2, "steam", "76561198000000001")

    def test_set_platform_id_persists_to_disk_and_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "discord_names.json"
            cache = _make_cache(pointer)

            changed = cache.set_platform_id(1, "steam", "76561198000000001")

            self.assertTrue(changed)
            payload = cast(dict[str, object], json.loads(pointer.read_text("utf-8")))
            self.assertEqual(
                cast(dict[str, object], payload["1"])["platform_ids"],
                {"steam": "76561198000000001"},
            )
            reloaded = config.Name_Cache._entries_from_serialized(payload)
            self.assertEqual(reloaded[1].platform_ids, {"steam": "76561198000000001"})

    def test_set_game_alias_preserves_existing_uuid(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                games={"minecraft": ("OldName", "123e4567-e89b-12d3-a456-426614174000")}
            )

            cache.set_game_alias(1, "minecraft", "NewName")

            self.assertEqual(
                cache.by_id[1].games["minecraft"],
                ("NewName", "123e4567-e89b-12d3-a456-426614174000"),
            )

    def test_get_game_uuid_returns_linked_uuid(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                games={"minecraft": ("Alice", "123e4567-e89b-12d3-a456-426614174000")}
            )

            self.assertEqual(cache.get_game_uuid(1, "minecraft"), "123e4567-e89b-12d3-a456-426614174000")

    def test_resolve_game_alias_to_id_ignores_non_game_alias_fallbacks(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(nicknames={"Alice"})
            cache.by_id[123] = config.UserNames()
            cache.by_id[7] = config.UserNames(
                games={"minecraft": ("Alice", "123e4567-e89b-12d3-a456-426614174000")}
            )

            self.assertEqual(cache.resolve_game_alias_to_id("Alice", "minecraft"), 7)
            self.assertIsNone(cache.resolve_game_alias_to_id("123", "minecraft"))

    def test_parse_mentions_resolves_scoped_game_aliases_to_discord_mentions(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(nicknames={"Alice"})
            cache.by_id[7] = config.UserNames(
                games={"minecraft": ("Alice", "123e4567-e89b-12d3-a456-426614174000")}
            )
            cache._rebuild_aliases()

            parsed_text, mentions = cache.parse_mentions("hello @Alice", scope="minecraft")

            self.assertEqual(parsed_text, "hello <@7>")
            self.assertEqual(mentions, {7})

    def test_parse_mentions_resolves_platform_aliases_to_discord_mentions(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[7] = config.UserNames(
                games={"steam": ("SteamAlice", None)},
            )
            cache._rebuild_aliases()

            parsed_text, mentions = cache.parse_mentions(
                "hello @SteamAlice",
                platforms=("steam",),
            )

            self.assertEqual(parsed_text, "hello <@7>")
            self.assertEqual(mentions, {7})

    def test_set_game_profile_normalises_minecraft_uuid(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")

            changed = cache.set_game_profile(1, "minecraft", "Alice", "123E4567E89B12D3A456426614174000")

            self.assertTrue(changed)
            self.assertEqual(
                cache.by_id[1].games["minecraft"],
                ("Alice", "123e4567-e89b-12d3-a456-426614174000"),
            )

    def test_set_game_profile_persists_to_disk_and_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "discord_names.json"
            cache = _make_cache(pointer)

            changed = cache.set_game_profile(1, "minecraft", "Alice", "123E4567E89B12D3A456426614174000")

            self.assertTrue(changed)
            payload = cast(dict[str, object], json.loads(pointer.read_text("utf-8")))
            self.assertEqual(
                cast(dict[str, object], payload["1"])["games"],
                {"minecraft": ["Alice", "123e4567-e89b-12d3-a456-426614174000"]},
            )
            reloaded = config.Name_Cache._entries_from_serialized(payload)
            self.assertEqual(
                reloaded[1].games["minecraft"],
                ("Alice", "123e4567-e89b-12d3-a456-426614174000"),
            )

    def test_set_game_uuid_persists_without_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "discord_names.json"
            cache = _make_cache(pointer)

            changed = cache.set_game_uuid(1, "minecraft", "123E4567E89B12D3A456426614174000")

            self.assertTrue(changed)
            self.assertEqual(
                cache.by_id[1].games["minecraft"],
                (None, "123e4567-e89b-12d3-a456-426614174000"),
            )
            payload = cast(dict[str, object], json.loads(pointer.read_text("utf-8")))
            self.assertEqual(
                cast(dict[str, object], payload["1"])["games"],
                {"minecraft": [None, "123e4567-e89b-12d3-a456-426614174000"]},
            )
            reloaded = config.Name_Cache._entries_from_serialized(payload)
            self.assertEqual(
                reloaded[1].games["minecraft"],
                (None, "123e4567-e89b-12d3-a456-426614174000"),
            )

    def test_set_game_profile_rejects_duplicate_minecraft_uuid(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                games={"minecraft": ("Alice", "123e4567-e89b-12d3-a456-426614174000")}
            )
            cache.by_id[2] = config.UserNames()

            with self.assertRaisesRegex(
                ValueError,
                r"Minecraft UUID `123e4567-e89b-12d3-a456-426614174000` is already used by another user\.",
            ):
                cache.set_game_profile(2, "minecraft", "Bob", "123e4567-e89b-12d3-a456-426614174000")


if __name__ == "__main__":
    unittest.main()
