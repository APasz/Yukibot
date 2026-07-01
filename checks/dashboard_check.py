from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import hikari

import config
from _manager import App_Manager
from _security import Access_Control, Power_Level
from _sys import Stats_System
from cmd_dashboard import (
    DashboardActionKind,
    DashboardEditorService,
    DashboardEditorState,
    DashboardSection,
    _dashboard_embed_color,
)
from config import Name_Cache
from maintenance import MaintenanceService
from online import Online_Tracker
from restart_targets import RestartTarget


class _FakeCache:
    def __init__(
        self,
        users: dict[hikari.Snowflake, SimpleNamespace],
        members: dict[tuple[hikari.Snowflake, hikari.Snowflake], SimpleNamespace] | None = None,
    ) -> None:
        self._users = users
        self._members = members or {}

    def get_user(self, user_id: hikari.Snowflake) -> SimpleNamespace | None:
        return self._users.get(hikari.Snowflake(user_id))

    def get_guilds_view(self) -> dict[hikari.Snowflake, SimpleNamespace]:
        return {}

    def get_available_guilds_view(self) -> dict[hikari.Snowflake, SimpleNamespace]:
        return {}

    def get_users_view(self) -> dict[hikari.Snowflake, SimpleNamespace]:
        return self._users

    def get_members_view(self) -> dict[hikari.Snowflake, dict[hikari.Snowflake, SimpleNamespace]]:
        guild_members: dict[hikari.Snowflake, dict[hikari.Snowflake, SimpleNamespace]] = {}
        for (guild_id, user_id), member in self._members.items():
            guild_members.setdefault(guild_id, {})[user_id] = member
        return guild_members

    def get_member(
        self,
        guild_id: hikari.Snowflake,
        user_id: hikari.Snowflake,
    ) -> SimpleNamespace | None:
        return self._members.get((hikari.Snowflake(guild_id), hikari.Snowflake(user_id)))


class _FakeBot:
    def __init__(
        self,
        *,
        me: SimpleNamespace | None,
        users: dict[hikari.Snowflake, SimpleNamespace],
        members: dict[tuple[hikari.Snowflake, hikari.Snowflake], SimpleNamespace] | None = None,
    ) -> None:
        self._me = me
        self.cache = _FakeCache(users, members)

    def get_me(self) -> SimpleNamespace | None:
        return self._me


class _FakeMember:
    def __init__(self, role_color: int, *, roles: tuple[SimpleNamespace, ...] | None = None) -> None:
        self._roles = roles or (SimpleNamespace(color=role_color, position=1),)
        self._top_role = max(self._roles, key=lambda role: role.position)

    def get_top_role(self) -> SimpleNamespace:
        return self._top_role

    def get_roles(self) -> tuple[SimpleNamespace, ...]:
        return self._roles


class _FakeMaintenanceService(MaintenanceService):
    def __init__(self) -> None:
        pass


class _FakeAppManager(App_Manager):
    def __init__(self) -> None:
        pass


class _FakeNameCache(Name_Cache):
    def __init__(self) -> None:
        self.by_id = {}
        self.by_alias = {}
        self.by_platform_id = {}

    def _dump(self) -> None:
        return None

    def _queue_remote_mutation(self, kind: object, **payload: object) -> None:
        del kind, payload

    def _rebuild_aliases(self) -> None:
        self.by_alias = {}


class _FakeStatsSystem(Stats_System):
    def __init__(self) -> None:
        pass


class _FakeOnlineTracker(Online_Tracker):
    def __init__(self) -> None:
        pass


class DashboardOAuthTests(unittest.TestCase):
    def test_dashboard_embed_color_uses_primary_guild_top_role_color(self) -> None:
        me_id = hikari.Snowflake("764270771350142976")
        guild_id = hikari.Snowflake(config.DISCORD_GUILD)
        bot = _FakeBot(
            me=SimpleNamespace(id=me_id),
            users={},
            members={(guild_id, me_id): _FakeMember(0x123456)},
        )

        self.assertEqual(_dashboard_embed_color(bot, None), 0x123456)

    def test_dashboard_embed_color_uses_highest_coloured_role_below_uncoloured_top_role(self) -> None:
        me_id = hikari.Snowflake("764270771350142976")
        guild_id = hikari.Snowflake(config.DISCORD_GUILD)
        bot = _FakeBot(
            me=SimpleNamespace(id=me_id),
            users={},
            members={
                (guild_id, me_id): _FakeMember(
                    0,
                    roles=(
                        SimpleNamespace(color=0, position=20),
                        SimpleNamespace(color=0x22C55E, position=10),
                    ),
                )
            },
        )

        self.assertEqual(_dashboard_embed_color(bot, None), 0x22C55E)

    def test_oauth_lines_use_cached_names_and_generate_missing_urls(self) -> None:
        yuki_id = "764270771350142976"
        erin_id = "1350601198637551659"
        bot = _FakeBot(
            me=SimpleNamespace(id=hikari.Snowflake(yuki_id), display_name="Yuki", username="Yuki"),
            users={
                hikari.Snowflake(erin_id): SimpleNamespace(display_name="Erin", username="Erin"),
            },
        )
        bot_config = config.BotConfiguration(
            OAuth=config.PersistedOAuthLinks(guild=None, user=None),
            KnownBots={
                erin_id: config.BotMetadataSnapshot(
                    profile=config.BotMetadataProfile(
                        id=erin_id,
                        label="Erin",
                        bot_profile=config.BotProfileName.ERIN,
                    ),
                    features=config.BotMetadataFeatures(
                        oauth=config.PersistedOAuthLinks(
                            guild="https://example.com/erin-guild",
                        )
                    ),
                )
            },
        )

        with patch("cmd_dashboard.config.load_bot_configuration", return_value=bot_config):
            lines = DashboardEditorService._oauth_lines(bot)  # type: ignore[arg-type]

        self.assertEqual(
            lines,
            [
                (
                    "[Yuki guild]"
                    "(https://discord.com/oauth2/authorize?client_id=764270771350142976&integration_type=0&scope=applications.commands+bot)"
                ),
                (
                    "[Yuki user]"
                    "(https://discord.com/oauth2/authorize?client_id=764270771350142976&integration_type=1&scope=applications.commands)"
                ),
                "[Erin guild](https://example.com/erin-guild)",
            ],
        )

    def test_oauth_lines_show_empty_state(self) -> None:
        bot = _FakeBot(me=None, users={})

        with patch("cmd_dashboard.config.load_bot_configuration", return_value=config.BotConfiguration()):
            lines = DashboardEditorService._oauth_lines(bot)  # type: ignore[arg-type]

        self.assertEqual(lines, ["None configured"])

    def test_oauth_lines_deduplicate_local_bot_known_bot_snapshot(self) -> None:
        yuki_id = "764270771350142976"
        bot = _FakeBot(
            me=SimpleNamespace(id=hikari.Snowflake(yuki_id), display_name="Yuki", username="Yuki"),
            users={},
        )
        bot_config = config.BotConfiguration(
            OAuth=config.PersistedOAuthLinks(guild=None, user=None),
            KnownBots={
                yuki_id: config.BotMetadataSnapshot(
                    profile=config.BotMetadataProfile(
                        id=yuki_id,
                        label="Yuki snapshot",
                        bot_profile=config.BotProfileName.YUKI,
                    ),
                    features=config.BotMetadataFeatures(
                        oauth=config.PersistedOAuthLinks(
                            guild="https://example.com/stale-guild",
                            user="https://example.com/stale-user",
                        )
                    ),
                )
            },
        )

        with patch("cmd_dashboard.config.load_bot_configuration", return_value=bot_config):
            lines = DashboardEditorService._oauth_lines(bot)  # type: ignore[arg-type]

        self.assertEqual(
            lines,
            [
                (
                    "[Yuki guild]"
                    "(https://discord.com/oauth2/authorize?client_id=764270771350142976&integration_type=0&scope=applications.commands+bot)"
                ),
                (
                    "[Yuki user]"
                    "(https://discord.com/oauth2/authorize?client_id=764270771350142976&integration_type=1&scope=applications.commands)"
                ),
            ],
        )

    def test_render_editor_uses_public_base_url_for_embed_url(self) -> None:
        service = DashboardEditorService()
        bot = _FakeBot(
            me=SimpleNamespace(
                id=hikari.Snowflake("764270771350142976"),
                display_name="Yuki",
                username="Yuki",
                display_avatar_url="https://example.com/yuki.png",
            ),
            users={},
        )
        acl = SimpleNamespace(
            level_of=lambda _user_id: SimpleNamespace(name="guest"),
            can=lambda _user_id, _level: False,
            LvL=SimpleNamespace(admin=object(), sudo=object()),
        )

        with (
            patch("cmd_dashboard.config.PUBLIC_BASE_URL", "https://wakusei.apasz.com"),
            patch.object(DashboardEditorService, "_runtime_lines", return_value=["runtime"]),
            patch.object(DashboardEditorService, "_system_lines", return_value=["system"]),
            patch.object(DashboardEditorService, "_service_lines", return_value=["services"]),
            patch.object(DashboardEditorService, "_oauth_lines", return_value=["oauth"]),
        ):
            embed, _components = service._render_editor(
                acl=acl,  # type: ignore[arg-type]
                actor_user_id=123,
                bot=bot,  # type: ignore[arg-type]
                guild_id=None,
                locale=hikari.Locale.EN_US,
                maintenance=_FakeMaintenanceService(),
                manager=_FakeAppManager(),
                names_cache=_FakeNameCache(),
                stats=_FakeStatsSystem(),
                state=DashboardEditorState(section=DashboardSection.HOME, page=0),
                tracker=_FakeOnlineTracker(),
            )

        self.assertEqual(embed.url, "https://wakusei.apasz.com")

    def test_maintenance_schedule_lines_show_available_targets(self) -> None:
        maintenance = MaintenanceService()
        maintenance._restart_schedules = {  # type: ignore[attr-defined]
            RestartTarget.BOT: config.PersistedRestartSchedule(
                enabled=True,
                interval_minutes=270,
                anchor_timestamp=int(datetime.fromisoformat("2026-05-27T04:30:00+10:00").timestamp()),
            ),
            RestartTarget.SYSTEM: config.PersistedRestartSchedule(enabled=False),
        }

        with patch(
            "cmd_dashboard.available_maintenance_restart_targets",
            return_value=(RestartTarget.BOT, RestartTarget.SYSTEM),
        ):
            lines = DashboardEditorService._maintenance_schedule_lines(maintenance)

        self.assertEqual(lines, ["bot: 4h 30m", "system: off"])

    def test_maintenance_warning_lines_show_configured_warning(self) -> None:
        maintenance = MaintenanceService()
        maintenance._restart_warning = config.PersistedRestartWarning(lead_minutes=25)  # type: ignore[attr-defined]

        with patch(
            "cmd_dashboard.available_maintenance_restart_targets",
            return_value=(RestartTarget.BOT, RestartTarget.SYSTEM),
        ):
            lines = DashboardEditorService._maintenance_warning_lines(maintenance)

        self.assertEqual(
            lines,
            [
                "configured: 25m",
                "final: 1m",
                "applies to: bot, system",
                "delivery: running apps with inbound relay",
            ],
        )

    def test_maintenance_modal_values_leave_disabled_fields_blank(self) -> None:
        maintenance = MaintenanceService()
        maintenance._restart_schedules = {  # type: ignore[attr-defined]
            RestartTarget.BOT: config.PersistedRestartSchedule(
                enabled=True,
                interval_minutes=270,
                anchor_timestamp=int(datetime.fromisoformat("2026-05-27T04:30:00+10:00").timestamp()),
            ),
            RestartTarget.SYSTEM: config.PersistedRestartSchedule(enabled=False),
        }

        values = DashboardEditorService._maintenance_modal_values(maintenance)

        self.assertEqual(values["bot"], "270")
        self.assertEqual(values["system"], "")

    def test_privileges_render_adds_visitor_button_without_guild_context(self) -> None:
        service = DashboardEditorService()
        bot = _FakeBot(
            me=SimpleNamespace(
                id=hikari.Snowflake("764270771350142976"),
                display_name="Yuki",
                username="Yuki",
                display_avatar_url="https://example.com/yuki.png",
            ),
            users={},
        )
        acl = SimpleNamespace(
            level_of=lambda _user_id: SimpleNamespace(name="admin"),
            can=lambda _user_id, _level: True,
            LvL=SimpleNamespace(admin=object(), sudo=object()),
            highest_manageable_level=lambda _user_id: SimpleNamespace(name="user"),
            serializable=lambda: {"visitor": [], "user": [], "admin": [], "sudo": [], "root": []},
            explicit_roles=lambda: {},
            next_promoted_level=lambda *_args: None,
            next_demoted_level=lambda *_args: None,
        )

        embed, components = service._render_editor(
            acl=acl,  # type: ignore[arg-type]
            actor_user_id=123,
            bot=bot,  # type: ignore[arg-type]
            guild_id=None,
            locale=hikari.Locale.EN_US,
            maintenance=_FakeMaintenanceService(),
            manager=_FakeAppManager(),
            names_cache=_FakeNameCache(),
            stats=_FakeStatsSystem(),
            state=DashboardEditorState(section=DashboardSection.PRIVILEGES, page=0),
            tracker=_FakeOnlineTracker(),
        )

        selection_field = next(field for field in embed.fields if field.name == "Selection")
        self.assertIn("Use Add Visitor to grant manual visitor access by Discord ID.", selection_field.value)
        first_row_labels = [component.label for component in components[0]._components]
        self.assertEqual(first_row_labels[0], "Add Visitor")

    def test_visitor_modal_submit_grants_manual_visitor_access(self) -> None:
        service = DashboardEditorService()
        bot = _FakeBot(
            me=SimpleNamespace(
                id=hikari.Snowflake("764270771350142976"),
                display_name="Yuki",
                username="Yuki",
                display_avatar_url="https://example.com/yuki.png",
            ),
            users={},
        )
        names_cache = _FakeNameCache()
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"admin": [100]}))
            acl = Access_Control(pointer)
            req = SimpleNamespace(
                user_id=100,
                action=service._build_state_action(
                    DashboardActionKind.SHOW_PRIVILEGES,
                    DashboardEditorState(section=DashboardSection.PRIVILEGES, page=0),
                ),
                values={"user_id": "200", "display": "Outside Visitor"},
                interaction=SimpleNamespace(guild_id=None),
            )
            deps = {
                "acl": acl,
                "bot": bot,
                "maintenance": _FakeMaintenanceService(),
                "manager": _FakeAppManager(),
                "names_cache": names_cache,
                "stats": _FakeStatsSystem(),
                "tracker": _FakeOnlineTracker(),
            }

            with (
                patch.object(service, "_require_bot", return_value=bot),  # type: ignore[arg-type]
                patch.object(service._editor, "resolve_locale", return_value=hikari.Locale.EN_US),
                patch.object(service, "_build_editor_response", side_effect=lambda **kwargs: kwargs),
            ):
                result = asyncio.run(service._on_visitor_modal_submit(req, deps))

        assert isinstance(result, dict)
        self.assertEqual(acl.level_of(200), Power_Level.visitor)
        self.assertTrue(names_cache.is_manual_user(200))
        self.assertEqual(names_cache.cached_display_name(200), "Outside Visitor")
        self.assertEqual(result["status"], "Added visitor access for Outside Visitor.")
        self.assertEqual(result["state"].selected_target_id, hikari.Snowflake(200))


if __name__ == "__main__":
    unittest.main()
