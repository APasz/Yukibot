from __future__ import annotations

import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, TypeVar

import hikari
import lightbulb
import psutil
from hikari_ui import (
    Editor,
    EditorButton,
    EditorLayout,
    EditorPageState,
    EditorRequest,
    EditorResponse,
    InteractionDeferral,
    PagedActionCodec,
)

import config
from _manager import App_Manager
from _security import Access_Control, Power_Level
from _sys import Stats_System
from config import Name_Cache
from online import Online_Tracker

log = logging.getLogger(__name__)

_DASHBOARD_EDITOR_PREFIX = "dashboard:"
_DEFAULT_DASHBOARD_EMBED_COLOR = 0xB00F0F
_PAGE_SIZE = 25
_PRIVILEGE_PAGE_SIZE = 15
_USER_ID_INDENT = "᲼" * 2

ValueT = TypeVar("ValueT")


class DashboardActionKind(enum.StrEnum):
    CLOSE = "cl"
    CONFIRM_DEMOTE_UNKNOWN = "xu"
    DEMOTE = "dm"
    DEMOTE_UNKNOWN = "du"
    PAGE = "pg"
    PROMOTE = "pm"
    REFRESH = "rf"
    SELECT_TARGET = "st"
    SHOW_HOME = "hm"
    SHOW_PRIVILEGES = "pv"


class DashboardSection(enum.StrEnum):
    HOME = "hm"
    PRIVILEGES = "pv"


@dataclass(frozen=True, slots=True)
class DashboardEditorState:
    section: DashboardSection
    page: int
    selected_target_id: hikari.Snowflake | None = None


@dataclass(frozen=True, slots=True)
class PagedItems(Generic[ValueT]):
    visible: tuple[ValueT, ...]
    total_count: int
    page_state: EditorPageState


@dataclass(frozen=True, slots=True)
class PrivilegeEntry:
    user_id: hikari.Snowflake
    label: str
    level: Power_Level


@dataclass(frozen=True, slots=True)
class UnknownPrivilegeEntry:
    user_id: hikari.Snowflake
    label: str
    level: Power_Level


def _editor_flags(is_public: bool) -> hikari.MessageFlag | hikari.UndefinedType:
    if is_public:
        return hikari.UNDEFINED
    return hikari.MessageFlag.EPHEMERAL


def _display_value(values: Sequence[str]) -> str:
    return "\n".join(values) if values else "None"


def _page_count(count: int, *, page_size: int = _PAGE_SIZE) -> int:
    return max(1, (count + page_size - 1) // page_size)


def _clamp_page(page: int, total_pages: int) -> int:
    if page < 0:
        return 0
    if page >= total_pages:
        return total_pages - 1
    return page


def _page_slice(values: Sequence[ValueT], page: int, *, page_size: int = _PAGE_SIZE) -> Sequence[ValueT]:
    start = page * page_size
    end = start + page_size
    return values[start:end]


def _paginate(values: Sequence[ValueT], page: int, *, page_size: int = _PAGE_SIZE) -> PagedItems[ValueT]:
    total_pages = _page_count(len(values), page_size=page_size)
    current_page = _clamp_page(page, total_pages)
    return PagedItems(
        visible=tuple(_page_slice(values, current_page, page_size=page_size)),
        total_count=len(values),
        page_state=EditorPageState(page=current_page, total_pages=total_pages),
    )


def _extract_user_id(value: object | None) -> hikari.Snowflake | None:
    if value is None:
        return None
    if isinstance(value, hikari.Snowflake):
        return value
    if isinstance(value, int):
        return hikari.Snowflake(value)
    if hasattr(value, "id"):
        ident = getattr(value, "id")
        if isinstance(ident, (int, hikari.Snowflake)):
            return hikari.Snowflake(ident)
    if isinstance(value, str) and value.isdigit():
        return hikari.Snowflake(value)
    return None


def _format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _format_duration(delta_seconds: float) -> str:
    seconds = max(0, int(delta_seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _truncate_text(value: str, /, *, limit: int) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    if limit <= 3:
        return stripped[:limit]
    return stripped[: limit - 3].rstrip() + "..."


def _role_count_lines(acl: Access_Control) -> list[str]:
    payload = acl.serializable()
    lines: list[str] = []
    for level in (Power_Level.user, Power_Level.admin, Power_Level.sudo, Power_Level.root):
        entries = payload.get(level.name, [])
        count = len(entries) if isinstance(entries, list) else 0
        lines.append(f"{level.name}: {count}")
    return lines


def _level_letter(level: Power_Level) -> str:
    return level.name[:1].upper()


def _format_configured_user_block(entries: Sequence[PrivilegeEntry]) -> str:
    if not entries:
        return "None"

    lines = []
    for entry in entries:
        safe_label = _truncate_text(entry.label.replace("`", "'"), limit=64)
        lines.append(
            "\n".join(
                [
                    f"`{_level_letter(entry.level)}` {safe_label}",
                    f"-# {_USER_ID_INDENT}{int(entry.user_id)}",
                ]
            )
        )
    return "\n".join(lines)


def _format_unknown_user_block(entries: Sequence[UnknownPrivilegeEntry]) -> str:
    if not entries:
        return "None"

    lines = []
    for entry in entries:
        safe_label = _truncate_text(entry.label.replace("`", "'"), limit=64)
        lines.append(
            "\n".join(
                [
                    f"`{_level_letter(entry.level)}` {safe_label}",
                    f"-# {_USER_ID_INDENT}{int(entry.user_id)}",
                ]
            )
        )
    return "\n".join(lines)


def _user_label(
    *,
    bot: hikari.GatewayBot,
    names_cache: Name_Cache,
    user_id: hikari.Snowflake,
    guild_id: hikari.Snowflake | None,
) -> str:
    if guild_id is not None and (member := bot.cache.get_member(guild_id, user_id)):
        return member.display_name
    if user := bot.cache.get_user(user_id):
        return user.display_name or user.username

    return names_cache.cached_display_name(int(user_id), f"User {user_id}", preferred_guild_id=guild_id)


def _is_user_in_any_guild(bot: hikari.GatewayBot, user_id: hikari.Snowflake) -> bool:
    members_view = bot.cache.get_members_view()
    return any(user_id in guild_members for guild_members in members_view.values())


def _dashboard_embed_guild_ids(current_guild_id: hikari.Snowflake | None) -> tuple[hikari.Snowflake, ...]:
    primary_guild_id = hikari.Snowflake(config.DISCORD_GUILD)
    if current_guild_id is None or current_guild_id == primary_guild_id:
        return (primary_guild_id,)
    return (primary_guild_id, current_guild_id)


def _dashboard_embed_color(bot: hikari.GatewayBot, current_guild_id: hikari.Snowflake | None) -> int:
    """Resolve the dashboard embed color from the bot's highest cached role."""
    me = bot.get_me()
    if me is None:
        return _DEFAULT_DASHBOARD_EMBED_COLOR

    for guild_id in _dashboard_embed_guild_ids(current_guild_id):
        member = bot.cache.get_member(guild_id, me.id)
        if member is None:
            continue

        top_role = member.get_top_role()
        if top_role is None:
            continue

        role_color = int(top_role.color)
        if role_color != 0:
            return role_color

    return _DEFAULT_DASHBOARD_EMBED_COLOR


class DashboardEditorService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(DashboardActionKind)
        self._pending_unknown_demotions: dict[hikari.Snowflake, tuple[UnknownPrivilegeEntry, ...]] = {}
        self._editor = Editor(
            prefix=_DASHBOARD_EDITOR_PREFIX,
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
            defer_resolver=self._defer_editor_action,
        )

    async def open_editor(
        self,
        *,
        ctx: lightbulb.Context,
        acl: Access_Control,
        bot: hikari.GatewayBot,
        manager: App_Manager,
        names_cache: Name_Cache,
        stats: Stats_System,
        tracker: Online_Tracker,
        is_public: bool = False,
        status: str = "Dashboard opened.",
    ) -> None:
        self._pending_unknown_demotions.pop(hikari.Snowflake(ctx.user.id), None)
        locale = self._editor.resolve_locale(ctx.interaction)
        embed, components = self._render_editor(
            acl=acl,
            actor_user_id=ctx.user.id,
            bot=bot,
            guild_id=ctx.guild_id,
            locale=locale,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            state=DashboardEditorState(section=DashboardSection.HOME, page=0),
            tracker=tracker,
        )
        await ctx.respond(
            status,
            embed=embed,
            components=components,
            flags=_editor_flags(is_public),
        )

    async def route_component(
        self,
        interaction: hikari.ComponentInteraction,
        *,
        acl: Access_Control,
        bot: hikari.GatewayBot,
        manager: App_Manager,
        names_cache: Name_Cache,
        stats: Stats_System,
        tracker: Online_Tracker,
    ) -> bool:
        return await self._editor.route(
            interaction,
            acl=acl,
            bot=bot,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            tracker=tracker,
        )

    async def _authorise_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        acl = self._require_acl(deps)
        try:
            await acl.perm_check(req.user_id, acl.LvL.guest)
        except Exception:
            return False
        return True

    async def _defer_editor_action(
        self,
        req: EditorRequest,
        deps: Mapping[str, object],
    ) -> InteractionDeferral | None:
        del deps
        action = self._action_codec.parse(req.action)
        if action is None:
            return None
        state = self._state_from_action(action)
        if action.kind in {
            DashboardActionKind.CONFIRM_DEMOTE_UNKNOWN,
            DashboardActionKind.DEMOTE,
            DashboardActionKind.DEMOTE_UNKNOWN,
            DashboardActionKind.PROMOTE,
            DashboardActionKind.SHOW_PRIVILEGES,
        }:
            return InteractionDeferral.update()
        if (
            action.kind
            in {
                DashboardActionKind.REFRESH,
            }
            and state is not None
            and state.section is DashboardSection.PRIVILEGES
        ):
            return InteractionDeferral.update()
        return None

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        acl = self._require_acl(deps)
        bot = self._require_bot(deps)
        manager = self._require_manager(deps)
        names_cache = self._require_names_cache(deps)
        stats = self._require_stats(deps)
        tracker = self._require_tracker(deps)

        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown dashboard action.")

        actor_user_id = int(req.user_id)
        state = self._state_from_action(action)

        if action.kind is DashboardActionKind.CLOSE:
            self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), None)
            return EditorResponse.close("Dashboard closed.")

        if (
            action.kind
            in {
                DashboardActionKind.DEMOTE,
                DashboardActionKind.DEMOTE_UNKNOWN,
                DashboardActionKind.PAGE,
                DashboardActionKind.PROMOTE,
                DashboardActionKind.REFRESH,
                DashboardActionKind.SELECT_TARGET,
                DashboardActionKind.SHOW_HOME,
                DashboardActionKind.SHOW_PRIVILEGES,
                DashboardActionKind.CONFIRM_DEMOTE_UNKNOWN,
            }
            and state is None
        ):
            return EditorResponse.ephemeral("Dashboard state is invalid.")

        assert state is not None

        if state.section is DashboardSection.PRIVILEGES or action.kind is DashboardActionKind.SHOW_PRIVILEGES:
            if not self._privileges_enabled():
                return EditorResponse.ephemeral("Privileges are only available on Yuki.")
            try:
                await acl.perm_check(actor_user_id, acl.LvL.admin)
            except Exception:
                return EditorResponse.ephemeral("Admin access is required for the privileges page.")

        if action.kind is DashboardActionKind.REFRESH:
            if state.section is DashboardSection.PRIVILEGES:
                self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), None)
                reloaded = acl.reload()
                status = (
                    "Privileges reloaded from authority."
                    if reloaded
                    else "Privileges reload failed; showing cached data."
                )
            else:
                status = "Dashboard refreshed."
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=status,
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.PAGE:
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status="Page updated.",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.SHOW_HOME:
            self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), None)
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=DashboardEditorState(
                    section=DashboardSection.HOME, page=0, selected_target_id=state.selected_target_id
                ),
                status="Showing dashboard home.",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.SHOW_PRIVILEGES:
            self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), None)
            reloaded = acl.reload()
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=DashboardEditorState(
                    section=DashboardSection.PRIVILEGES, page=0, selected_target_id=state.selected_target_id
                ),
                status="Showing privileges." if reloaded else "Showing privileges with cached authority data.",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.SELECT_TARGET:
            if not req.values:
                return EditorResponse.ephemeral("Choose a user first.")
            target_user_id = _extract_user_id(req.values[0])
            if target_user_id is None:
                return EditorResponse.ephemeral("Invalid user selection.")
            self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), None)

            next_state = DashboardEditorState(
                section=DashboardSection.PRIVILEGES,
                page=state.page,
                selected_target_id=target_user_id,
            )
            label = _user_label(
                bot=bot,
                names_cache=names_cache,
                user_id=target_user_id,
                guild_id=req.interaction.guild_id,
            )
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=next_state,
                status=f"Selected {label}.",
                tracker=tracker,
            )

        if action.kind in {DashboardActionKind.PROMOTE, DashboardActionKind.DEMOTE}:
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("Choose a user first.")
            self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), None)
            await self._set_deferred_status(
                req,
                "Updating privileges...",
            )
            label = _user_label(
                bot=bot,
                names_cache=names_cache,
                user_id=state.selected_target_id,
                guild_id=req.interaction.guild_id,
            )
            try:
                new_level = (
                    acl.promote(actor_user_id, int(state.selected_target_id))
                    if action.kind is DashboardActionKind.PROMOTE
                    else acl.demote(actor_user_id, int(state.selected_target_id))
                )
            except Exception as xcp:
                return EditorResponse.ephemeral(str(xcp))

            verb = "Promoted" if action.kind is DashboardActionKind.PROMOTE else "Demoted"
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=f"{verb} {label} to {new_level.name.title()}.",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.DEMOTE_UNKNOWN:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.sudo)
            except Exception:
                return EditorResponse.ephemeral("Sudo access is required to demote unknown users.")

            await self._set_deferred_status(
                req,
                "Fetching unresolved users...",
            )
            try:
                pending_unknown = await self._scan_unknown_privileged_users(
                    acl=acl,
                    actor_user_id=actor_user_id,
                    bot=bot,
                    names_cache=names_cache,
                    guild_id=req.interaction.guild_id,
                )
            except Exception as xcp:
                log.warning("Unknown-user privilege scan failed: %s", xcp, exc_info=True)
                return EditorResponse.ephemeral(f"Failed to refresh guild membership for verification: {xcp}")
            self._pending_unknown_demotions[hikari.Snowflake(actor_user_id)] = pending_unknown
            if not pending_unknown:
                status = "No unresolvable configured users were found."
            else:
                status = (
                    f"Found {len(pending_unknown)} unresolvable configured user(s). Confirm to demote them to Guest."
                )

            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=status,
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.CONFIRM_DEMOTE_UNKNOWN:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.sudo)
            except Exception:
                return EditorResponse.ephemeral("Sudo access is required to demote unknown users.")

            await self._set_deferred_status(
                req,
                "Demoting unresolved configured users...",
            )
            pending_unknown = self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), ())
            removed_user_ids = acl.demote_to_guest_many(
                actor_user_id,
                (int(entry.user_id) for entry in pending_unknown),
            )
            if state.selected_target_id is not None and int(state.selected_target_id) in removed_user_ids:
                state = DashboardEditorState(section=state.section, page=state.page, selected_target_id=None)

            if not pending_unknown:
                status = "No pending unknown-user demotion was found. Scan again first."
            elif not removed_user_ids:
                status = "No scanned unresolvable users could be demoted with your access level."
            else:
                status = f"Demoted {len(removed_user_ids)} unresolvable configured user(s) to Guest."

            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=status,
                tracker=tracker,
            )

        return EditorResponse.ephemeral("Unsupported dashboard action.")

    def _build_editor_response(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        bot: hikari.GatewayBot,
        guild_id: hikari.Snowflake | None,
        locale: hikari.Locale,
        manager: App_Manager,
        names_cache: Name_Cache,
        stats: Stats_System,
        state: DashboardEditorState,
        status: str,
        tracker: Online_Tracker,
    ) -> EditorResponse:
        embed, components = self._render_editor(
            acl=acl,
            actor_user_id=actor_user_id,
            bot=bot,
            guild_id=guild_id,
            locale=locale,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            state=state,
            tracker=tracker,
        )
        return EditorResponse.update(status, components=components, embeds=[embed])

    def _render_editor(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        bot: hikari.GatewayBot,
        guild_id: hikari.Snowflake | None,
        locale: hikari.Locale,
        manager: App_Manager,
        names_cache: Name_Cache,
        stats: Stats_System,
        state: DashboardEditorState,
        tracker: Online_Tracker,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        editor_ctx = self._editor.context(
            scope_id=actor_user_id,
            user_id=actor_user_id,
            locale=locale,
        )
        layout = EditorLayout(editor_ctx)
        me = bot.get_me()
        embed = hikari.Embed(
            title=f"{me.display_name if me is not None else 'Yuki'} Dashboard",
            description="Operational snapshot for the current bot instance.",
            color=_dashboard_embed_color(bot, guild_id),
        )

        if state.section is DashboardSection.HOME:
            self._render_home_section(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                embed=embed,
                layout=layout,
                manager=manager,
                names_cache=names_cache,
                state=state,
                stats=stats,
                tracker=tracker,
            )
        else:
            self._render_privileges_section(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                embed=embed,
                guild_id=guild_id,
                layout=layout,
                names_cache=names_cache,
                state=state,
            )

        return embed, layout.build()

    def _render_home_section(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        bot: hikari.GatewayBot,
        embed: hikari.Embed,
        layout: EditorLayout,
        manager: App_Manager,
        names_cache: Name_Cache,
        state: DashboardEditorState,
        stats: Stats_System,
        tracker: Online_Tracker,
    ) -> None:
        embed.description = f"Viewer level: {acl.level_of(actor_user_id).name.title()}"
        embed.add_field(
            name="Runtime",
            value=_display_value(self._runtime_lines(bot, acl, actor_user_id)),
            inline=True,
        )
        embed.add_field(
            name="System",
            value=_display_value(self._system_lines(stats)),
            inline=True,
        )
        embed.add_field(
            name="Services",
            value=_display_value(self._service_lines(manager)),
            inline=True,
        )
        embed.add_field(
            name="Tracking",
            value=_display_value(self._tracking_lines(acl, names_cache, tracker)),
            inline=True,
        )

        extra_buttons: list[EditorButton] = [
            EditorButton(self._build_state_action(DashboardActionKind.REFRESH, state), "Refresh")
        ]
        if self._privileges_enabled() and acl.can(actor_user_id, acl.LvL.admin):
            extra_buttons.append(
                EditorButton(
                    self._build_state_action(DashboardActionKind.SHOW_PRIVILEGES, state),
                    "Privileges",
                    style=hikari.ButtonStyle.PRIMARY,
                )
            )

        layout.page_footer(
            self._action_codec.build(DashboardActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1),
            extra_buttons=tuple(extra_buttons),
        )

    def _render_privileges_section(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        bot: hikari.GatewayBot,
        embed: hikari.Embed,
        guild_id: hikari.Snowflake | None,
        layout: EditorLayout,
        names_cache: Name_Cache,
        state: DashboardEditorState,
    ) -> None:
        highest_manageable = acl.highest_manageable_level(actor_user_id)
        highest_label = highest_manageable.name.title() if highest_manageable is not None else "None"
        embed.title = "Dashboard Privileges"
        embed.description = (
            f"Your level: {acl.level_of(actor_user_id).name.title()}\nHighest manageable level: {highest_label}"
        )

        entries = self._privilege_entries(acl=acl, bot=bot, guild_id=guild_id, names_cache=names_cache)
        page = _paginate(entries, state.page, page_size=_PRIVILEGE_PAGE_SIZE)
        pending_unknown = self._pending_unknown_demotions.get(hikari.Snowflake(actor_user_id), ())

        embed.add_field(name="Role Counts", value=_display_value(_role_count_lines(acl)), inline=False)
        embed.add_field(
            name=f"Configured Users ({page.total_count})",
            value=_format_configured_user_block(page.visible),
            inline=False,
        )
        if pending_unknown:
            embed.add_field(
                name=f"Pending Unknown Demotion ({len(pending_unknown)})",
                value=_format_unknown_user_block(pending_unknown),
                inline=False,
            )
        embed.add_field(
            name="Selected User",
            value=_display_value(
                self._selected_target_lines(
                    acl=acl,
                    actor_user_id=actor_user_id,
                    bot=bot,
                    guild_id=guild_id,
                    names_cache=names_cache,
                    target_user_id=state.selected_target_id,
                )
            ),
            inline=False,
        )

        if guild_id is not None:
            layout.add_user_select(
                self._build_state_action(DashboardActionKind.SELECT_TARGET, state),
                placeholder="Pick a user to inspect or change",
            )
        else:
            embed.add_field(
                name="Selection",
                value="User selection is only available when `/dashboard` is opened in a guild.",
                inline=False,
            )

        promote_to = None
        demote_to = None
        if state.selected_target_id is not None:
            promote_to = acl.next_promoted_level(actor_user_id, int(state.selected_target_id))
            demote_to = acl.next_demoted_level(actor_user_id, int(state.selected_target_id))
        can_demote_unknown = acl.can(actor_user_id, acl.LvL.sudo)
        action_buttons: list[EditorButton] = [
            EditorButton(
                self._build_state_action(DashboardActionKind.PROMOTE, state),
                "Promote",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=promote_to is None,
            ),
            EditorButton(
                self._build_state_action(DashboardActionKind.DEMOTE, state),
                "Demote",
                style=hikari.ButtonStyle.DANGER,
                is_disabled=demote_to is None,
            ),
        ]
        if pending_unknown:
            action_buttons.extend(
                [
                    EditorButton(
                        self._build_state_action(DashboardActionKind.CONFIRM_DEMOTE_UNKNOWN, state),
                        "Confirm Unknown",
                        style=hikari.ButtonStyle.DANGER,
                    ),
                ]
            )
        else:
            action_buttons.append(
                EditorButton(
                    self._build_state_action(DashboardActionKind.DEMOTE_UNKNOWN, state),
                    "Demote Unknown",
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=not can_demote_unknown,
                )
            )

        layout.add_buttons(*action_buttons)

        prev_action = None
        next_action = None
        if page.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                DashboardActionKind.PAGE,
                DashboardEditorState(
                    section=DashboardSection.PRIVILEGES,
                    page=max(0, page.page_state.page - 1),
                    selected_target_id=state.selected_target_id,
                ),
            )
            next_action = self._build_state_action(
                DashboardActionKind.PAGE,
                DashboardEditorState(
                    section=DashboardSection.PRIVILEGES,
                    page=min(page.page_state.total_pages - 1, page.page_state.page + 1),
                    selected_target_id=state.selected_target_id,
                ),
            )

        layout.page_footer(
            self._action_codec.build(DashboardActionKind.CLOSE, page=state.page),
            page_state=EditorPageState(
                page=page.page_state.page,
                total_pages=page.page_state.total_pages,
                is_subpage=True,
            ),
            back_action=self._build_state_action(
                DashboardActionKind.SHOW_HOME,
                DashboardEditorState(
                    section=DashboardSection.PRIVILEGES, page=0, selected_target_id=state.selected_target_id
                ),
            ),
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(EditorButton(self._build_state_action(DashboardActionKind.REFRESH, state), "Refresh"),),
        )

    @staticmethod
    def _runtime_lines(bot: hikari.GatewayBot, acl: Access_Control, actor_user_id: int) -> list[str]:
        process = psutil.Process()
        started_at = datetime.fromtimestamp(process.create_time(), tz=timezone.utc)
        uptime = datetime.now(timezone.utc) - started_at
        total_guilds = len(bot.cache.get_guilds_view())
        available_guilds = len(bot.cache.get_available_guilds_view())
        cached_users = len(bot.cache.get_users_view())
        return [
            f"authority: {config.DATA_AUTHORITY_MODE.value}",
            f"uptime: {_format_duration(uptime.total_seconds())}",
            f"guilds: {available_guilds}/{total_guilds} available",
            f"cached users: {cached_users}",
            f"guest access: {config.GUESTS_ALLOWED}",
        ]

    @staticmethod
    def _system_lines(stats: Stats_System) -> list[str]:
        hot_core = max(stats.cpu.r_per_core, default=0)
        ram_total = _format_bytes(int(stats.ram.raw.total))
        ram_used = _format_bytes(int(stats.ram.used))
        disk_total = _format_bytes(int(stats.disk.usage.total))
        disk_free = _format_bytes(int(stats.disk.usage.free))
        return [
            f"cpu: {stats.cpu.r_total}% (hot core {hot_core}%)",
            f"ram: {stats.ram.percent}% ({ram_used} / {ram_total})",
            f"swap: {stats.ram.swap_percent}%",
            f"disk: {stats.disk.percent}% ({disk_free} free / {disk_total})",
        ]

    @staticmethod
    def _service_lines(manager: App_Manager) -> list[str]:
        activity_manager = manager.activity_manager
        current_app = manager.get_current
        lines = [
            f"apps loaded: {len(manager.apps)}",
            f"current app: {manager.current or 'None'}",
        ]
        if current_app is not None and current_app.check_running() and current_app.cfg.join_display_address is not None:
            lines.append(f"join address: {current_app.cfg.join_display_address}")
        if activity_manager is None:
            lines.append("activity manager: unavailable")
            return lines

        last_update_age = datetime.now(timezone.utc) - activity_manager.last_update
        lines.extend(
            [
                f"activity providers: {len(activity_manager.providers)}",
                f"presence text: {activity_manager.state or 'None'}",
                f"last activity update: {_format_duration(last_update_age.total_seconds())} ago",
            ]
        )
        return lines

    @staticmethod
    def _tracking_lines(acl: Access_Control, names_cache: Name_Cache, tracker: Online_Tracker) -> list[str]:
        lines = []
        lines.extend(_role_count_lines(acl))
        return lines

    def _privilege_entries(
        self,
        *,
        acl: Access_Control,
        bot: hikari.GatewayBot,
        guild_id: hikari.Snowflake | None,
        names_cache: Name_Cache,
    ) -> tuple[PrivilegeEntry, ...]:
        entries = [
            PrivilegeEntry(
                user_id=hikari.Snowflake(user_id),
                label=_user_label(
                    bot=bot,
                    names_cache=names_cache,
                    user_id=hikari.Snowflake(user_id),
                    guild_id=guild_id,
                ),
                level=level,
            )
            for user_id, level in acl.explicit_roles().items()
        ]
        return tuple(
            sorted(
                entries,
                key=lambda entry: (-int(entry.level), entry.label.casefold(), int(entry.user_id)),
            )
        )

    async def _scan_unknown_privileged_users(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        bot: hikari.GatewayBot,
        names_cache: Name_Cache,
        guild_id: hikari.Snowflake | None,
    ) -> tuple[UnknownPrivilegeEntry, ...]:
        acl.reload()
        guild_ids = await self._guild_ids_for_membership_scan(bot)
        unresolved: list[UnknownPrivilegeEntry] = []
        for user_id, level in acl.explicit_roles().items():
            if level is Power_Level.guest:
                continue
            if not acl.can_manage_target(actor_user_id, user_id):
                continue
            snowflake = hikari.Snowflake(user_id)
            if await self._user_is_resolvable(bot=bot, guild_ids=guild_ids, user_id=snowflake):
                continue
            unresolved.append(
                UnknownPrivilegeEntry(
                    user_id=snowflake,
                    label=_user_label(
                        bot=bot,
                        names_cache=names_cache,
                        user_id=snowflake,
                        guild_id=guild_id,
                    ),
                    level=level,
                )
            )
        return tuple(
            sorted(
                unresolved,
                key=lambda entry: (-int(entry.level), entry.label.casefold(), int(entry.user_id)),
            )
        )

    @staticmethod
    async def _guild_ids_for_membership_scan(bot: hikari.GatewayBot) -> tuple[hikari.Snowflake, ...]:
        guild_ids = {hikari.Snowflake(guild_id) for guild_id in bot.cache.get_guilds_view()}
        async for guild in bot.rest.fetch_my_guilds():
            guild_ids.add(hikari.Snowflake(guild.id))
        return tuple(sorted(guild_ids))

    @staticmethod
    async def _user_is_resolvable(
        *,
        bot: hikari.GatewayBot,
        guild_ids: Sequence[hikari.Snowflake],
        user_id: hikari.Snowflake,
    ) -> bool:
        if _is_user_in_any_guild(bot, user_id):
            return True

        for guild_id in guild_ids:
            try:
                await bot.rest.fetch_member(guild_id, user_id)
            except hikari.NotFoundError:
                continue
            return True
        return False

    @staticmethod
    async def _set_deferred_status(
        req: EditorRequest,
        content: str,
    ) -> None:
        await req.interaction.edit_initial_response(content=content)

    @staticmethod
    def _privileges_enabled() -> bool:
        return config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.YUKI

    def _selected_target_lines(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        bot: hikari.GatewayBot,
        guild_id: hikari.Snowflake | None,
        names_cache: Name_Cache,
        target_user_id: hikari.Snowflake | None,
    ) -> list[str]:
        if target_user_id is None:
            return ["No user selected.", "Use the selector to inspect a member."]

        label = _user_label(
            bot=bot,
            names_cache=names_cache,
            user_id=target_user_id,
            guild_id=guild_id,
        )
        current_level = acl.level_of(int(target_user_id))
        promote_to = acl.next_promoted_level(actor_user_id, int(target_user_id))
        demote_to = acl.next_demoted_level(actor_user_id, int(target_user_id))
        return [
            f"user: {label} (`{int(target_user_id)}`)",
            f"current: {current_level.name.title()}",
            f"promote: {promote_to.name.title() if promote_to is not None else 'Unavailable'}",
            f"demote: {demote_to.name.title() if demote_to is not None else 'Unavailable'}",
        ]

    def _build_state_action(self, kind: DashboardActionKind, state: DashboardEditorState) -> str:
        return self._action_codec.build(kind, page=state.page, value=self._pack_state(state))

    @staticmethod
    def _pack_state(state: DashboardEditorState) -> str:
        target_id = int(state.selected_target_id) if state.selected_target_id is not None else 0
        return f"{state.section.value},{target_id}"

    def _state_from_action(self, action: object) -> DashboardEditorState | None:
        page = getattr(action, "page", None)
        raw_value = getattr(action, "value", None)
        if not isinstance(page, int) or not isinstance(raw_value, str):
            return None
        parts = raw_value.split(",", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            return None
        try:
            section = DashboardSection(parts[0])
        except ValueError:
            return None
        target_user_id = hikari.Snowflake(parts[1]) if parts[1] != "0" else None
        return DashboardEditorState(section=section, page=page, selected_target_id=target_user_id)

    @staticmethod
    def _require_acl(deps: Mapping[str, object]) -> Access_Control:
        value = deps.get("acl")
        if not isinstance(value, Access_Control):
            raise TypeError("Dashboard editor requires Access_Control")
        return value

    @staticmethod
    def _require_bot(deps: Mapping[str, object]) -> hikari.GatewayBot:
        value = deps.get("bot")
        if not isinstance(value, hikari.GatewayBot):
            raise TypeError("Dashboard editor requires GatewayBot")
        return value

    @staticmethod
    def _require_manager(deps: Mapping[str, object]) -> App_Manager:
        value = deps.get("manager")
        if not isinstance(value, App_Manager):
            raise TypeError("Dashboard editor requires App_Manager")
        return value

    @staticmethod
    def _require_names_cache(deps: Mapping[str, object]) -> Name_Cache:
        value = deps.get("names_cache")
        if not isinstance(value, Name_Cache):
            raise TypeError("Dashboard editor requires Name_Cache")
        return value

    @staticmethod
    def _require_stats(deps: Mapping[str, object]) -> Stats_System:
        value = deps.get("stats")
        if not isinstance(value, Stats_System):
            raise TypeError("Dashboard editor requires Stats_System")
        return value

    @staticmethod
    def _require_tracker(deps: Mapping[str, object]) -> Online_Tracker:
        value = deps.get("tracker")
        if not isinstance(value, Online_Tracker):
            raise TypeError("Dashboard editor requires Online_Tracker")
        return value


class CMD_Dashboard(
    lightbulb.SlashCommand,
    name="dashboard",
    description="Open the bot dashboard",
):
    public = lightbulb.boolean("public", "Send the dashboard as a normal message", default=False)  # type: ignore[reportAssignmentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        bot: hikari.GatewayBot,
        dashboard_editor: DashboardEditorService,
        manager: App_Manager,
        names_cache: Name_Cache,
        stats: Stats_System,
        tracker: Online_Tracker,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        await dashboard_editor.open_editor(
            ctx=ctx,
            acl=acl,
            bot=bot,
            is_public=self.public,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            tracker=tracker,
        )


# AiviA APasz
