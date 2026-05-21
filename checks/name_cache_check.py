from __future__ import annotations

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
) -> hikari.Member:
    member = create_autospec(hikari.Member, instance=True)
    member.id = hikari.Snowflake(user_id)
    member.guild_id = hikari.Snowflake(guild_id)
    member.username = username
    member.global_name = global_name
    member.nickname = nickname
    member.display_name = nickname or global_name or username
    return member


class NameCacheTests(unittest.TestCase):
    def test_serializable_excludes_derived_names(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name"},
                guild_names={100: "guild-name"},
            )

            payload = cast(dict[str, dict[str, Any]], cache.serializable())

            self.assertNotIn("names", payload["1"])
            self.assertEqual(payload["1"]["global_name"], "global-name")
            self.assertEqual(payload["1"]["guild_names"], {"100": "guild-name"})

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

    def test_cached_display_name_prefers_primary_guild_name(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = _make_cache(Path(tmp) / "discord_names.json")
            cache.by_id[1] = config.UserNames(
                account="user-name",
                global_name="global-name",
                names={"user-name", "global-name", "primary-name", "other-name"},
                guild_names={int(config.DISCORD_GUILD): "primary-name", 200: "other-name"},
            )

            self.assertEqual(cache.cached_display_name(1), "primary-name")
            self.assertEqual(cache.cached_display_name(1, preferred_guild_id=200), "other-name")

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


if __name__ == "__main__":
    unittest.main()
