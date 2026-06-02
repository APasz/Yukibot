from __future__ import annotations

import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, Protocol, TypeVar

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
    EditorSelectOption,
    InteractionDeferral,
    ModalKit,
    ModalRequest,
    ModalSchema,
    ModalTextField,
    PagedActionCodec,
)

import config
from _discord import cached_top_role_color
from _editor_session import startup_editor_prefix
from _manager import App_Manager
from _security import Access_Control, Power_Level
from _sys import Stats_Disk, Stats_System
from cmd_ops import available_restart_targets
from config import Name_Cache
from maintenance import MaintenanceService
from online import Online_Tracker
from restart_targets import RestartTarget

log = logging.getLogger(__name__)

_DASHBOARD_EDITOR_PREFIX = "dashboard:"
_DASHBOARD_MAINTENANCE_MODAL_PREFIX = "dashboard-maintenance:"
_DASHBOARD_MAINTENANCE_WARNING_MODAL_PREFIX = "dashboard-maintenance-warning:"
_DASHBOARD_STORAGE_LABEL_MODAL_PREFIX = "dashboard-storage-label:"
_DASHBOARD_VISITOR_MODAL_PREFIX = "dashboard-visitor:"
_DEFAULT_DASHBOARD_EMBED_COLOR = 0xB00F0F
_PAGE_SIZE = 25
_DISK_PAGE_SIZE = 24
_PRIVILEGE_PAGE_SIZE = 15
_MAINTENANCE_SCHEDULE_FIELD_IDS: dict[RestartTarget, str] = {
    RestartTarget.BOT: "bot",
    RestartTarget.VOICE: "voice",
    RestartTarget.SYSTEM: "system",
}
_MAINTENANCE_WARNING_FIELD_ID = "warning"
_PRIMARY_DISK_DEFAULT_VALUE = "__default__"
_STORAGE_LABELS_FIELD_ID = "labels"
_VISITOR_DISPLAY_NAME_FIELD_ID = "display"
_VISITOR_USER_ID_FIELD_ID = "user_id"
_EMBED_SPACER = "᲼"
_EMBED_SUBTEXT = "-# "

ValueT = TypeVar("ValueT")


class DashboardActionKind(enum.StrEnum):
    CLOSE = "cl"
    CONFIRM_DEMOTE_UNKNOWN = "xu"
    DEMOTE = "dm"
    DEMOTE_UNKNOWN = "du"
    OPEN_MAINTENANCE_MODAL = "mm"
    OPEN_MAINTENANCE_WARNING_MODAL = "mw"
    OPEN_LABEL_MODAL = "lm"
    OPEN_VISITOR_MODAL = "vm"
    PAGE = "pg"
    PROMOTE = "pm"
    REFRESH = "rf"
    SELECT_ACTIVITY_DISKS = "ad"
    SELECT_PRIMARY_DISK = "pd"
    SELECT_TARGET = "st"
    SHOW_HOME = "hm"
    SHOW_MAINTENANCE = "mt"
    SHOW_PRIVILEGES = "pv"
    SHOW_STORAGE = "ds"


class DashboardSection(enum.StrEnum):
    HOME = "hm"
    MAINTENANCE = "mt"
    PRIVILEGES = "pv"
    STORAGE = "ds"


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


class PrivilegedUserEntry(Protocol):
    @property
    def user_id(self) -> hikari.Snowflake: ...

    @property
    def label(self) -> str: ...

    @property
    def level(self) -> Power_Level: ...


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
    for level in (Power_Level.visitor, Power_Level.user, Power_Level.admin, Power_Level.sudo, Power_Level.root):
        entries = payload.get(level.name, [])
        count = len(entries) if isinstance(entries, list) else 0
        lines.append(f"{level.name}: {count}")
    return lines


def _level_letter(level: Power_Level) -> str:
    return level.name[:1].upper()


def _format_user_block(entries: Sequence[PrivilegedUserEntry]) -> str:
    if not entries:
        return "None"

    lines = []
    for entry in entries:
        safe_label = _truncate_text(entry.label.replace("`", "'"), limit=64)
        lines.append(
            "\n".join(
                [
                    f"`{_level_letter(entry.level)}` {safe_label}",
                    f"{_EMBED_SUBTEXT}{_EMBED_SPACER * 2}{entry.user_id}",
                ]
            )
        )
    return "\n".join(lines)


def _format_disk_line(
    disk: Stats_Disk,
    *,
    is_activity_disk: bool,
    is_primary_disk: bool,
) -> str:
    disk_total = _format_bytes(int(disk.usage.total))
    disk_free = _format_bytes(int(disk.usage.free))
    flags: list[str] = []
    if is_primary_disk:
        flags.append("primary")
    if is_activity_disk:
        flags.append("activity")
    flag_text = f" [{' / '.join(flags)}]" if flags else ""
    label = disk.label or "(unlabelled)"
    return (
        f"{label} | {disk.mountpoint_text}{flag_text}\n"
        f"{_EMBED_SUBTEXT}{disk.filesystem} | {disk.percent}% | {disk_free}/{disk_total}"
    )


def _disk_select_option(disk: Stats_Disk, *, is_default: bool) -> EditorSelectOption:
    return EditorSelectOption(
        label=_truncate_text(f"{disk.display_name} | {disk.mountpoint_text}", limit=100),
        value=disk.mountpoint_text,
        description=_truncate_text(
            f"{disk.device} | {disk.filesystem} | {disk.percent}% used",
            limit=100,
        ),
        is_default=is_default,
    )


def _storage_label_modal_value(stats: Stats_System) -> str:
    return "\n".join(f"{disk.mountpoint_text} = {disk.label or ''}".rstrip() for disk in stats.disks)


def _directory_file_lines(path: Path, *, max_entries: int = 6) -> list[str]:
    if not path.exists():
        return ["Missing"]
    if not path.is_dir():
        return ["Not a directory"]

    try:
        files = [entry for entry in path.iterdir() if entry.is_file() or entry.is_symlink()]
    except OSError as xcp:
        return [f"Error: {type(xcp).__name__}"]

    if not files:
        return ["Empty"]

    sortable_files: list[tuple[int, Path]] = []
    for entry in files:
        try:
            sortable_files.append((entry.stat().st_mtime_ns, entry))
        except OSError:
            continue
    if not sortable_files:
        return ["Unreadable"]
    sortable_files.sort(key=lambda item: item[0], reverse=True)
    lines: list[str] = []
    for _, entry in sortable_files[:max_entries]:
        try:
            stat = entry.stat()
        except OSError:
            continue
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        lines.append(
            f"{_truncate_text(entry.name, limit=34)} | {_format_bytes(stat.st_size)} | {_format_duration(age.total_seconds())}"
        )
    if len(sortable_files) > max_entries:
        lines.append(f"...and {len(sortable_files) - max_entries} more")
    return lines


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
    return cached_top_role_color(bot, guild_ids=_dashboard_embed_guild_ids(current_guild_id)) or (
        _DEFAULT_DASHBOARD_EMBED_COLOR
    )


class DashboardEditorService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(DashboardActionKind)
        self._pending_unknown_demotions: dict[hikari.Snowflake, tuple[UnknownPrivilegeEntry, ...]] = {}
        self._maintenance_modal = ModalKit(
            prefix=startup_editor_prefix(_DASHBOARD_MAINTENANCE_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_MAINTENANCE_SCHEDULE_FIELD_IDS[RestartTarget.BOT],
                        label="Bot Schedule",
                        style=hikari.TextInputStyle.SHORT,
                        required=False,
                        max_length=16,
                    ),
                    ModalTextField(
                        id=_MAINTENANCE_SCHEDULE_FIELD_IDS[RestartTarget.VOICE],
                        label="Voice Schedule",
                        style=hikari.TextInputStyle.SHORT,
                        required=False,
                        max_length=16,
                    ),
                    ModalTextField(
                        id=_MAINTENANCE_SCHEDULE_FIELD_IDS[RestartTarget.SYSTEM],
                        label="System Schedule",
                        style=hikari.TextInputStyle.SHORT,
                        required=False,
                        max_length=16,
                    ),
                ]
            ),
        )
        self._maintenance_warning_modal = ModalKit(
            prefix=startup_editor_prefix(_DASHBOARD_MAINTENANCE_WARNING_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_MAINTENANCE_WARNING_FIELD_ID,
                        label="Warning Minutes (0 or 5-180)",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=3,
                    )
                ]
            ),
        )
        self._storage_label_modal = ModalKit(
            prefix=startup_editor_prefix(_DASHBOARD_STORAGE_LABEL_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_STORAGE_LABELS_FIELD_ID,
                        label="Disk Labels",
                        style=hikari.TextInputStyle.PARAGRAPH,
                        required=False,
                        max_length=4000,
                    )
                ]
            ),
        )
        self._visitor_modal = ModalKit(
            prefix=startup_editor_prefix(_DASHBOARD_VISITOR_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VISITOR_USER_ID_FIELD_ID,
                        label="Discord User ID",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=20,
                    ),
                    ModalTextField(
                        id=_VISITOR_DISPLAY_NAME_FIELD_ID,
                        label="Display Name (optional)",
                        style=hikari.TextInputStyle.SHORT,
                        required=False,
                        max_length=100,
                    ),
                ]
            ),
        )
        self._editor = Editor(
            prefix=startup_editor_prefix(_DASHBOARD_EDITOR_PREFIX),
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
        maintenance: MaintenanceService,
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
            maintenance=maintenance,
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
        maintenance: MaintenanceService,
        manager: App_Manager,
        names_cache: Name_Cache,
        stats: Stats_System,
        tracker: Online_Tracker,
    ) -> bool:
        return await self._editor.route(
            interaction,
            acl=acl,
            bot=bot,
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            tracker=tracker,
        )

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        acl: Access_Control,
        bot: hikari.GatewayBot,
        maintenance: MaintenanceService,
        manager: App_Manager,
        names_cache: Name_Cache,
        stats: Stats_System,
        tracker: Online_Tracker,
    ) -> bool:
        if await self._maintenance_modal.route(
            interaction,
            on_submit=self._on_maintenance_modal_submit,
            authoriser=self._authorise_modal_action,
            unauthorised_message="You are not authorised to use this dashboard modal.",
            invalid_message="Maintenance input is invalid.",
            acl=acl,
            bot=bot,
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            tracker=tracker,
        ):
            return True
        if await self._maintenance_warning_modal.route(
            interaction,
            on_submit=self._on_maintenance_warning_modal_submit,
            authoriser=self._authorise_modal_action,
            unauthorised_message="You are not authorised to use this dashboard modal.",
            invalid_message="Maintenance input is invalid.",
            acl=acl,
            bot=bot,
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            tracker=tracker,
        ):
            return True
        if await self._visitor_modal.route(
            interaction,
            on_submit=self._on_visitor_modal_submit,
            authoriser=self._authorise_modal_action,
            unauthorised_message="You are not authorised to use this dashboard modal.",
            invalid_message="Visitor input is invalid.",
            acl=acl,
            bot=bot,
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            tracker=tracker,
        ):
            return True
        return await self._storage_label_modal.route(
            interaction,
            on_submit=self._on_storage_label_modal_submit,
            authoriser=self._authorise_modal_action,
            unauthorised_message="You are not authorised to use this dashboard modal.",
            invalid_message="Storage label input is invalid.",
            acl=acl,
            bot=bot,
            maintenance=maintenance,
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

    async def _authorise_modal_action(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
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
            DashboardActionKind.SELECT_ACTIVITY_DISKS,
            DashboardActionKind.SELECT_PRIMARY_DISK,
            DashboardActionKind.SHOW_MAINTENANCE,
            DashboardActionKind.SHOW_PRIVILEGES,
            DashboardActionKind.SHOW_STORAGE,
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
        maintenance = self._require_maintenance(deps)
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
                DashboardActionKind.OPEN_MAINTENANCE_MODAL,
                DashboardActionKind.OPEN_MAINTENANCE_WARNING_MODAL,
                DashboardActionKind.OPEN_LABEL_MODAL,
                DashboardActionKind.OPEN_VISITOR_MODAL,
                DashboardActionKind.PAGE,
                DashboardActionKind.PROMOTE,
                DashboardActionKind.REFRESH,
                DashboardActionKind.SELECT_ACTIVITY_DISKS,
                DashboardActionKind.SELECT_PRIMARY_DISK,
                DashboardActionKind.SELECT_TARGET,
                DashboardActionKind.SHOW_HOME,
                DashboardActionKind.SHOW_MAINTENANCE,
                DashboardActionKind.SHOW_PRIVILEGES,
                DashboardActionKind.SHOW_STORAGE,
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

        if (
            state.section is DashboardSection.MAINTENANCE and action.kind is not DashboardActionKind.SHOW_HOME
        ) or action.kind is DashboardActionKind.SHOW_MAINTENANCE:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.sudo)
            except Exception:
                return EditorResponse.ephemeral("Sudo access is required for the maintenance page.")

        if (
            state.section is DashboardSection.STORAGE and action.kind is not DashboardActionKind.SHOW_HOME
        ) or action.kind is DashboardActionKind.SHOW_STORAGE:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.sudo)
            except Exception:
                return EditorResponse.ephemeral("Sudo access is required for the storage page.")

        if action.kind is DashboardActionKind.REFRESH:
            if state.section is DashboardSection.PRIVILEGES:
                self._pending_unknown_demotions.pop(hikari.Snowflake(actor_user_id), None)
                reloaded = acl.reload()
                status = (
                    "Privileges reloaded from authority."
                    if reloaded
                    else "Privileges reload failed; showing cached data."
                )
            elif state.section is DashboardSection.STORAGE:
                reloaded = stats.reload_disk_preferences()
                stats.refresh_disk_inventory()
                status = (
                    "Storage settings reloaded from configuration."
                    if reloaded
                    else "Storage settings reload failed; showing cached data."
                )
            elif state.section is DashboardSection.MAINTENANCE:
                reloaded = maintenance.reload()
                status = (
                    "Maintenance schedules reloaded from configuration."
                    if reloaded
                    else "Maintenance schedule reload failed; showing cached data."
                )
            else:
                status = "Dashboard refreshed."
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                maintenance=maintenance,
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
                maintenance=maintenance,
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
                maintenance=maintenance,
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
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=DashboardEditorState(
                    section=DashboardSection.PRIVILEGES, page=0, selected_target_id=state.selected_target_id
                ),
                status="Showing privileges." if reloaded else "Showing privileges with cached authority data.",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.SHOW_MAINTENANCE:
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=DashboardEditorState(
                    section=DashboardSection.MAINTENANCE,
                    page=0,
                    selected_target_id=state.selected_target_id,
                ),
                status="Showing maintenance.",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.SHOW_STORAGE:
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=DashboardEditorState(
                    section=DashboardSection.STORAGE,
                    page=0,
                    selected_target_id=state.selected_target_id,
                ),
                status="Showing storage.",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.OPEN_MAINTENANCE_MODAL:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.sudo)
            except Exception:
                return EditorResponse.ephemeral("Sudo access is required to edit maintenance schedules.")

            await req.interaction.create_modal_response(
                title=f"Edit Maintenance Schedules ({self._local_timezone_label()})",
                custom_id=self._maintenance_modal.build_id(
                    self._build_state_action(DashboardActionKind.SHOW_MAINTENANCE, state),
                    scope_id=actor_user_id,
                    user_id=actor_user_id,
                ),
                components=self._maintenance_modal.rows(self._maintenance_modal_values(maintenance)),
            )
            return None

        if action.kind is DashboardActionKind.OPEN_MAINTENANCE_WARNING_MODAL:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.sudo)
            except Exception:
                return EditorResponse.ephemeral("Sudo access is required to edit maintenance warnings.")

            await req.interaction.create_modal_response(
                title="Edit Maintenance Warning",
                custom_id=self._maintenance_warning_modal.build_id(
                    self._build_state_action(DashboardActionKind.SHOW_MAINTENANCE, state),
                    scope_id=actor_user_id,
                    user_id=actor_user_id,
                ),
                components=self._maintenance_warning_modal.rows(self._maintenance_warning_modal_values(maintenance)),
            )
            return None

        if action.kind is DashboardActionKind.OPEN_LABEL_MODAL:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.sudo)
            except Exception:
                return EditorResponse.ephemeral("Sudo access is required to edit disk labels.")

            modal_value = _storage_label_modal_value(stats)
            if len(modal_value) > 4000:
                return EditorResponse.ephemeral(
                    "Disk labels exceed the modal size limit. Reduce the discovered disk set first."
                )

            await req.interaction.create_modal_response(
                title="Edit Disk Labels",
                custom_id=self._storage_label_modal.build_id(
                    self._build_state_action(DashboardActionKind.SHOW_STORAGE, state),
                    scope_id=actor_user_id,
                    user_id=actor_user_id,
                ),
                components=self._storage_label_modal.rows({_STORAGE_LABELS_FIELD_ID: modal_value}),
            )
            return None

        if action.kind is DashboardActionKind.OPEN_VISITOR_MODAL:
            try:
                await acl.perm_check(actor_user_id, acl.LvL.admin)
            except Exception:
                return EditorResponse.ephemeral("Admin access is required to add visitors.")

            await req.interaction.create_modal_response(
                title="Add Visitor",
                custom_id=self._visitor_modal.build_id(
                    self._build_state_action(DashboardActionKind.SHOW_PRIVILEGES, state),
                    scope_id=actor_user_id,
                    user_id=actor_user_id,
                ),
                components=self._visitor_modal.rows({}),
            )
            return None

        if action.kind is DashboardActionKind.SELECT_ACTIVITY_DISKS:
            page = _paginate(stats.disks, state.page, page_size=_DISK_PAGE_SIZE)
            visible_mountpoints = {disk.mountpoint_text for disk in page.visible}
            current_mountpoints = {disk.mountpoint_text for disk in stats.activity_disks}
            selected_mountpoints = {
                config.normalise_absolute_path_text(str(value), source="dashboard storage activity selection")
                for value in req.values
            }
            next_mountpoints = sorted((current_mountpoints - visible_mountpoints) | selected_mountpoints)
            stats.set_activity_mounts(next_mountpoints)
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=f"Updated activity disks ({len(stats.activity_disks)} selected).",
                tracker=tracker,
            )

        if action.kind is DashboardActionKind.SELECT_PRIMARY_DISK:
            if not req.values:
                return EditorResponse.ephemeral("Choose a primary disk option first.")

            selected_value = str(req.values[0])
            if selected_value == _PRIMARY_DISK_DEFAULT_VALUE:
                stats.set_primary_mount_override(None)
                status = "Primary disk reset to the bot disk."
            else:
                disk = stats.set_primary_mount_override(selected_value)
                if disk is None:
                    return EditorResponse.ephemeral("No primary disk is available.")
                status = f"Primary disk override set to {disk.mountpoint_text}."
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=req.locale,
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=status,
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
                maintenance=maintenance,
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
                maintenance=maintenance,
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
                maintenance=maintenance,
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
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=status,
                tracker=tracker,
            )

        return EditorResponse.ephemeral("Unsupported dashboard action.")

    async def _on_visitor_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        acl = self._require_acl(deps)
        bot = self._require_bot(deps)
        maintenance = self._require_maintenance(deps)
        manager = self._require_manager(deps)
        names_cache = self._require_names_cache(deps)
        stats = self._require_stats(deps)
        tracker = self._require_tracker(deps)

        actor_user_id = int(req.user_id)
        try:
            await acl.perm_check(actor_user_id, acl.LvL.admin)
        except Exception:
            return EditorResponse.ephemeral("Admin access is required to add visitors.")

        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown dashboard modal action.")

        state = self._state_from_action(action)
        if state is None or state.section is not DashboardSection.PRIVILEGES:
            return EditorResponse.ephemeral("Dashboard state is invalid.")

        try:
            target_user_id = self._parse_discord_user_id(
                req.values.get(_VISITOR_USER_ID_FIELD_ID, ""),
                source="visitor user id",
            )
            display_name = self._normalise_optional_single_line_text(
                req.values.get(_VISITOR_DISPLAY_NAME_FIELD_ID, ""),
                source="visitor display name",
            )
            acl.grant_visitor(actor_user_id, int(target_user_id))
            names_cache.upsert_manual_user(
                int(target_user_id),
                display_name=display_name,
            )
        except Exception as xcp:
            return self._build_editor_response(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=self._editor.resolve_locale(req.interaction),
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=f"Error: visitor add failed: {xcp}",
                tracker=tracker,
            )

        label = _user_label(
            bot=bot,
            names_cache=names_cache,
            user_id=target_user_id,
            guild_id=req.interaction.guild_id,
        )
        next_state = DashboardEditorState(
            section=DashboardSection.PRIVILEGES,
            page=state.page,
            selected_target_id=target_user_id,
        )
        return self._build_editor_response(
            acl=acl,
            actor_user_id=actor_user_id,
            bot=bot,
            guild_id=req.interaction.guild_id,
            locale=self._editor.resolve_locale(req.interaction),
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            state=next_state,
            status=f"Added visitor access for {label}.",
            tracker=tracker,
        )

    async def _on_storage_label_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        acl = self._require_acl(deps)
        bot = self._require_bot(deps)
        maintenance = self._require_maintenance(deps)
        manager = self._require_manager(deps)
        names_cache = self._require_names_cache(deps)
        stats = self._require_stats(deps)
        tracker = self._require_tracker(deps)

        try:
            await acl.perm_check(int(req.user_id), acl.LvL.sudo)
        except Exception:
            return EditorResponse.ephemeral("Sudo access is required to edit disk labels.")

        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown dashboard modal action.")

        state = self._state_from_action(action)
        if state is None or state.section is not DashboardSection.STORAGE:
            return EditorResponse.ephemeral("Dashboard state is invalid.")

        try:
            labels = self._parse_storage_label_lines(
                req.values.get(_STORAGE_LABELS_FIELD_ID, ""),
                stats=stats,
            )
            stats.replace_disk_labels(labels)
        except Exception as xcp:
            return self._build_editor_response(
                acl=acl,
                actor_user_id=int(req.user_id),
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=self._editor.resolve_locale(req.interaction),
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=f"Error: label update failed: {xcp}",
                tracker=tracker,
            )

        return self._build_editor_response(
            acl=acl,
            actor_user_id=int(req.user_id),
            bot=bot,
            guild_id=req.interaction.guild_id,
            locale=self._editor.resolve_locale(req.interaction),
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            state=state,
            status="Disk labels updated.",
            tracker=tracker,
        )

    async def _on_maintenance_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        acl = self._require_acl(deps)
        bot = self._require_bot(deps)
        maintenance = self._require_maintenance(deps)
        manager = self._require_manager(deps)
        names_cache = self._require_names_cache(deps)
        stats = self._require_stats(deps)
        tracker = self._require_tracker(deps)

        try:
            await acl.perm_check(int(req.user_id), acl.LvL.sudo)
        except Exception:
            return EditorResponse.ephemeral("Sudo access is required to edit maintenance schedules.")

        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown dashboard modal action.")

        state = self._state_from_action(action)
        if state is None or state.section is not DashboardSection.MAINTENANCE:
            return EditorResponse.ephemeral("Dashboard state is invalid.")

        try:
            available_targets = set(available_restart_targets(config.ACTIVE_BOT_PROFILE))
            updates: dict[RestartTarget, tuple[int, int] | None] = {}
            for target in RestartTarget:
                field_id = _MAINTENANCE_SCHEDULE_FIELD_IDS[target]
                raw_value = req.values.get(field_id, "")
                if target not in available_targets:
                    if raw_value.strip():
                        raise ValueError(f"{target.value.title()} restarts are unavailable for this bot profile.")
                    continue
                updates[target] = maintenance.parse_schedule_text(raw_value)
            maintenance.update_restart_schedules(updates)
        except Exception as xcp:
            return self._build_editor_response(
                acl=acl,
                actor_user_id=int(req.user_id),
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=self._editor.resolve_locale(req.interaction),
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=f"Error: maintenance update failed: {xcp}",
                tracker=tracker,
            )

        return self._build_editor_response(
            acl=acl,
            actor_user_id=int(req.user_id),
            bot=bot,
            guild_id=req.interaction.guild_id,
            locale=self._editor.resolve_locale(req.interaction),
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            state=state,
            status="Maintenance schedules updated.",
            tracker=tracker,
        )

    async def _on_maintenance_warning_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        acl = self._require_acl(deps)
        bot = self._require_bot(deps)
        maintenance = self._require_maintenance(deps)
        manager = self._require_manager(deps)
        names_cache = self._require_names_cache(deps)
        stats = self._require_stats(deps)
        tracker = self._require_tracker(deps)

        try:
            await acl.perm_check(int(req.user_id), acl.LvL.sudo)
        except Exception:
            return EditorResponse.ephemeral("Sudo access is required to edit maintenance warnings.")

        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown dashboard modal action.")

        state = self._state_from_action(action)
        if state is None or state.section is not DashboardSection.MAINTENANCE:
            return EditorResponse.ephemeral("Dashboard state is invalid.")

        try:
            lead_minutes = maintenance.parse_warning_minutes_text(req.values.get(_MAINTENANCE_WARNING_FIELD_ID, ""))
            maintenance.update_restart_warning_minutes(lead_minutes)
        except Exception as xcp:
            return self._build_editor_response(
                acl=acl,
                actor_user_id=int(req.user_id),
                bot=bot,
                guild_id=req.interaction.guild_id,
                locale=self._editor.resolve_locale(req.interaction),
                maintenance=maintenance,
                manager=manager,
                names_cache=names_cache,
                stats=stats,
                state=state,
                status=f"Error: maintenance warning update failed: {xcp}",
                tracker=tracker,
            )

        return self._build_editor_response(
            acl=acl,
            actor_user_id=int(req.user_id),
            bot=bot,
            guild_id=req.interaction.guild_id,
            locale=self._editor.resolve_locale(req.interaction),
            maintenance=maintenance,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            state=state,
            status="Maintenance warning updated.",
            tracker=tracker,
        )

    def _build_editor_response(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        bot: hikari.GatewayBot,
        guild_id: hikari.Snowflake | None,
        locale: hikari.Locale,
        maintenance: MaintenanceService,
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
            maintenance=maintenance,
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
        maintenance: MaintenanceService,
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
            url=config.PUBLIC_BASE_URL,
            color=_dashboard_embed_color(bot, guild_id),
        )
        if me is not None:
            embed.set_thumbnail(str(me.display_avatar_url))

        if state.section is DashboardSection.HOME:
            self._render_home_section(
                acl=acl,
                actor_user_id=actor_user_id,
                bot=bot,
                embed=embed,
                layout=layout,
                maintenance=maintenance,
                manager=manager,
                state=state,
                stats=stats,
            )
        elif state.section is DashboardSection.MAINTENANCE:
            self._render_maintenance_section(
                acl=acl,
                actor_user_id=actor_user_id,
                embed=embed,
                layout=layout,
                maintenance=maintenance,
                state=state,
            )
        elif state.section is DashboardSection.STORAGE:
            self._render_storage_section(
                acl=acl,
                actor_user_id=actor_user_id,
                embed=embed,
                layout=layout,
                state=state,
                stats=stats,
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
        maintenance: MaintenanceService,
        manager: App_Manager,
        state: DashboardEditorState,
        stats: Stats_System,
    ) -> None:
        del maintenance
        embed.description = "\n".join(
            [
                f"Viewer: {acl.level_of(actor_user_id).name.title()}",
                f"{_EMBED_SUBTEXT}Operational snapshot for the current bot instance.",
            ]
        )
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
            name=_EMBED_SPACER,
            value=_EMBED_SPACER,
            inline=True,
        )
        embed.add_field(
            name="Services",
            value=_display_value(self._service_lines(manager)),
            inline=False,
        )
        if config.ACTIVE_BOT_PROFILE.name is config.BotProfileName.YUKI:
            embed.add_field(
                name="OAuth",
                value=_display_value(self._oauth_lines(bot)),
                inline=False,
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
        if acl.can(actor_user_id, acl.LvL.sudo):
            extra_buttons.append(
                EditorButton(
                    self._build_state_action(DashboardActionKind.SHOW_MAINTENANCE, state),
                    "Maintenance",
                )
            )
            extra_buttons.append(
                EditorButton(
                    self._build_state_action(DashboardActionKind.SHOW_STORAGE, state),
                    "Storage",
                )
            )

        layout.page_footer(
            self._action_codec.build(DashboardActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1),
            extra_buttons=tuple(extra_buttons),
        )

    def _render_storage_section(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: DashboardEditorState,
        stats: Stats_System,
    ) -> None:
        embed.title = "Dashboard Storage"
        embed.description = "\n".join(
            [
                f"Lvl: {acl.level_of(actor_user_id).name.title()} | Disks: {len(stats.disks)}",
                f"{_EMBED_SUBTEXT}Sudo disk controls, labels, uploads, and tmp files.",
            ]
        )

        page = _paginate(stats.disks, state.page, page_size=_DISK_PAGE_SIZE)
        activity_mountpoints = {disk.mountpoint_text for disk in stats.activity_disks}
        primary_disk = stats.primary_disk
        bot_disk = stats.bot_disk

        embed.add_field(
            name="Primary",
            value=_display_value(self._primary_disk_lines(stats=stats)),
            inline=True,
        )
        embed.add_field(
            name="Activity",
            value=_display_value(self._activity_disk_lines(stats=stats)),
            inline=True,
        )
        embed.add_field(name=_EMBED_SPACER, value=_EMBED_SPACER, inline=True)
        embed.add_field(
            name="Uploads",
            value=_display_value(_directory_file_lines(config.DIR_UPLOAD)),
            inline=True,
        )
        embed.add_field(
            name="Tmp",
            value=_display_value(_directory_file_lines(config.DIR_TMP)),
            inline=True,
        )
        embed.add_field(name=_EMBED_SPACER, value=_EMBED_SPACER, inline=True)
        embed.add_field(
            name=f"Disks ({page.total_count})",
            value=_display_value(
                [
                    _format_disk_line(
                        disk,
                        is_activity_disk=disk.mountpoint_text in activity_mountpoints,
                        is_primary_disk=primary_disk is not None
                        and disk.mountpoint_text == primary_disk.mountpoint_text,
                    )
                    for disk in page.visible
                ]
            ),
            inline=False,
        )

        if page.visible:
            layout.add_text_select(
                self._build_state_action(DashboardActionKind.SELECT_ACTIVITY_DISKS, state),
                options=[
                    _disk_select_option(
                        disk,
                        is_default=disk.mountpoint_text in activity_mountpoints,
                    )
                    for disk in page.visible
                ],
                min_values=0,
                max_values=len(page.visible),
                placeholder="Choose activity disks for this page",
            )

            primary_options = [
                EditorSelectOption(
                    label="Bot Disk (default)",
                    value=_PRIMARY_DISK_DEFAULT_VALUE,
                    description=(
                        _truncate_text(
                            bot_disk.mountpoint_text if bot_disk is not None else "Use the bot disk when available",
                            limit=100,
                        )
                    ),
                    is_default=stats.configured_primary_mount is None,
                )
            ]
            primary_options.extend(
                [
                    _disk_select_option(
                        disk,
                        is_default=stats.configured_primary_mount == disk.mountpoint_text,
                    )
                    for disk in page.visible
                ]
            )
            layout.add_text_select(
                self._build_state_action(DashboardActionKind.SELECT_PRIMARY_DISK, state),
                options=primary_options,
                placeholder="Choose the effective primary disk",
            )
            layout.add_button(
                self._build_state_action(DashboardActionKind.OPEN_LABEL_MODAL, state),
                "Edit Labels",
                style=hikari.ButtonStyle.PRIMARY,
            )

        prev_action = None
        next_action = None
        if page.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                DashboardActionKind.PAGE,
                DashboardEditorState(
                    section=DashboardSection.STORAGE,
                    page=max(0, page.page_state.page - 1),
                    selected_target_id=state.selected_target_id,
                ),
            )
            next_action = self._build_state_action(
                DashboardActionKind.PAGE,
                DashboardEditorState(
                    section=DashboardSection.STORAGE,
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
                    section=DashboardSection.STORAGE,
                    page=0,
                    selected_target_id=state.selected_target_id,
                ),
            ),
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(EditorButton(self._build_state_action(DashboardActionKind.REFRESH, state), "Refresh"),),
        )

    def _render_maintenance_section(
        self,
        *,
        acl: Access_Control,
        actor_user_id: int,
        embed: hikari.Embed,
        layout: EditorLayout,
        maintenance: MaintenanceService,
        state: DashboardEditorState,
    ) -> None:
        embed.title = "Dashboard Maintenance"
        embed.description = "\n".join(
            [
                f"Lvl: {acl.level_of(actor_user_id).name.title()} | Timezone: {self._local_timezone_label()}",
                f"{_EMBED_SUBTEXT}Daily restart schedules and relay warnings for this bot instance.",
            ]
        )
        embed.add_field(
            name="Restart Schedules",
            value=_display_value(self._maintenance_schedule_lines(maintenance)),
            inline=False,
        )
        embed.add_field(
            name="Relay Warning",
            value=_display_value(self._maintenance_warning_lines(maintenance)),
            inline=False,
        )
        embed.add_field(
            name="Notes",
            value=_display_value(
                [
                    f"Schedules use `HH:MM` in `{self._local_timezone_label()}` or `off` to disable a target.",
                    "If multiple targets match the same minute, broader restarts supersede narrower ones.",
                ]
            ),
            inline=True,
        )
        layout.add_buttons(
            EditorButton(
                self._build_state_action(DashboardActionKind.OPEN_MAINTENANCE_MODAL, state),
                "Edit Schedules",
                style=hikari.ButtonStyle.PRIMARY,
            ),
            EditorButton(
                self._build_state_action(DashboardActionKind.OPEN_MAINTENANCE_WARNING_MODAL, state),
                "Edit Warning",
            ),
        )
        layout.page_footer(
            self._action_codec.build(DashboardActionKind.CLOSE, page=state.page),
            page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
            back_action=self._build_state_action(
                DashboardActionKind.SHOW_HOME,
                DashboardEditorState(
                    section=DashboardSection.MAINTENANCE,
                    page=0,
                    selected_target_id=state.selected_target_id,
                ),
            ),
            extra_buttons=(EditorButton(self._build_state_action(DashboardActionKind.REFRESH, state), "Refresh"),),
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
        embed.description = "\n".join(
            [
                f"Lvl: {acl.level_of(actor_user_id).name.title()} | Manage: {highest_label}",
                f"{_EMBED_SUBTEXT}Authority roles and unresolved configured users.",
            ]
        )

        entries = self._privilege_entries(acl=acl, bot=bot, guild_id=guild_id, names_cache=names_cache)
        page = _paginate(entries, state.page, page_size=_PRIVILEGE_PAGE_SIZE)
        pending_unknown = self._pending_unknown_demotions.get(hikari.Snowflake(actor_user_id), ())

        embed.add_field(name="Counts", value=_display_value(_role_count_lines(acl)), inline=True)
        embed.add_field(
            name="Selected",
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
            inline=True,
        )
        embed.add_field(name=_EMBED_SPACER, value=_EMBED_SPACER, inline=True)
        embed.add_field(
            name=f"Users ({page.total_count})",
            value=_format_user_block(page.visible),
            inline=False,
        )
        if pending_unknown:
            embed.add_field(
                name=f"Pending ({len(pending_unknown)})",
                value=_format_user_block(pending_unknown),
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
                value=(
                    "User selection is only available when `/dashboard` is opened in a guild.\n"
                    "Use Add Visitor to grant manual visitor access by Discord ID."
                ),
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
                self._build_state_action(DashboardActionKind.OPEN_VISITOR_MODAL, state),
                "Add Visitor",
            ),
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
    def _maintenance_modal_values(maintenance: MaintenanceService) -> dict[str, str]:
        return {
            field_id: maintenance.format_schedule_input(maintenance.schedule_for(target))
            for target, field_id in _MAINTENANCE_SCHEDULE_FIELD_IDS.items()
        }

    @staticmethod
    def _maintenance_warning_modal_values(maintenance: MaintenanceService) -> dict[str, str]:
        return {
            _MAINTENANCE_WARNING_FIELD_ID: maintenance.format_warning_minutes_input(
                maintenance.restart_warning_lead_minutes
            )
        }

    @staticmethod
    def _maintenance_schedule_lines(maintenance: MaintenanceService) -> list[str]:
        lines: list[str] = []
        available_targets = set(available_restart_targets(config.ACTIVE_BOT_PROFILE))
        for target in RestartTarget:
            if target not in available_targets:
                continue
            schedule = maintenance.schedule_for(target)
            lines.append(f"{target.value}: {maintenance.format_schedule_text(schedule)}")
        if not lines:
            return ["No restart targets are available."]
        return lines

    @staticmethod
    def _maintenance_warning_lines(maintenance: MaintenanceService) -> list[str]:
        available_targets = [
            target.value
            for target in available_restart_targets(config.ACTIVE_BOT_PROFILE)
            if target in {RestartTarget.BOT, RestartTarget.SYSTEM}
        ]
        applies_to = ", ".join(available_targets) if available_targets else "none"
        return [
            f"configurable: {maintenance.format_warning_minutes_display(maintenance.restart_warning_lead_minutes)}",
            "final: 1m",
            f"applies to: {applies_to}",
            "delivery: running apps with inbound relay",
        ]

    @staticmethod
    def _local_timezone_label() -> str:
        now = datetime.now().astimezone()
        tz_name = now.tzname() or "local"
        offset = now.utcoffset()
        if offset is None:
            return tz_name
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        return f"{tz_name} (UTC{sign}{hours:02d}:{minutes:02d})"

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
        lines = [
            f"cpu: {stats.cpu.r_total}% (hot core {hot_core}%)",
            f"ram: {stats.ram.percent}% ({ram_used} / {ram_total})",
            f"swap: {stats.ram.swap_percent}%",
        ]
        primary_disk = stats.primary_disk
        if primary_disk is not None:
            disk_total = _format_bytes(int(primary_disk.usage.total))
            disk_free = _format_bytes(int(primary_disk.usage.free))
            lines.append(f"primary: {primary_disk.percent}% ({disk_free} / {disk_total})")
        return lines

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
    def _oauth_lines(bot: hikari.GatewayBot) -> list[str]:
        try:
            bot_config = config.load_bot_configuration(Path("configuration.json"))
        except (OSError, ValueError) as xcp:
            return [f"Config error: {type(xcp).__name__}: {xcp}"]

        oauth_entries = DashboardEditorService._oauth_entries(bot, bot_config=bot_config)
        if not oauth_entries:
            return ["None configured"]

        log.info(
            "Dashboard OAuth entries: %s",
            [
                {
                    "bot_id": bot_id,
                    "label": label,
                    "install_types": [install_type.value for install_type in oauth_links.supported_install_types()],
                }
                for bot_id, label, oauth_links in oauth_entries
            ],
        )

        lines: list[str] = []
        for bot_id, label, oauth_links in oauth_entries:
            for install_type in oauth_links.supported_install_types():
                url = oauth_links.configured_url(install_type) or config.build_discord_oauth_url(
                    bot_id,
                    install_type=install_type,
                )
                lines.append(f"[{label} {install_type.value}]({url})")
        return lines

    @staticmethod
    def _oauth_entries(
        bot: hikari.GatewayBot,
        *,
        bot_config: config.BotConfiguration,
    ) -> list[tuple[str, str, config.PersistedOAuthLinks]]:
        entries: list[tuple[str, str, config.PersistedOAuthLinks]] = []
        seen_bot_ids: set[str] = set()
        me = bot.get_me()
        if me is not None:
            me_id = str(int(me.id))
            if bot_config.oauth.supported_install_types():
                entries.append(
                    (
                        me_id,
                        me.display_name or me.username,
                        bot_config.oauth,
                    )
                )
                seen_bot_ids.add(me_id)

        for bot_id, snapshot in sorted(bot_config.known_bots.items(), key=lambda item: int(item[0])):
            if bot_id in seen_bot_ids:
                continue
            oauth_links = snapshot.features.oauth or config.PersistedOAuthLinks()
            if not oauth_links.supported_install_types():
                continue
            entries.append(
                (
                    bot_id,
                    DashboardEditorService._oauth_bot_label(bot, snapshot=snapshot),
                    oauth_links,
                )
            )
            seen_bot_ids.add(bot_id)
        return entries

    @staticmethod
    def _oauth_bot_label(bot: hikari.GatewayBot, *, snapshot: config.BotMetadataSnapshot) -> str:
        bot_id = snapshot.profile.id
        if snapshot.profile.label is not None:
            return snapshot.profile.label

        me = bot.get_me()
        if me is not None and int(me.id) == int(bot_id):
            return me.display_name or me.username

        cached_user = bot.cache.get_user(hikari.Snowflake(bot_id))
        if cached_user is not None:
            return cached_user.display_name or cached_user.username

        return f"Bot {bot_id}"

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
            if names_cache.is_manual_user(user_id):
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
            return ["No user selected.", "Use the selector or Add Visitor to inspect a user."]

        label = _user_label(
            bot=bot,
            names_cache=names_cache,
            user_id=target_user_id,
            guild_id=guild_id,
        )
        current_level = acl.level_of(int(target_user_id))
        return [
            f"user: {label} (`{int(target_user_id)}`)",
            f"current: {current_level.name.title()}",
        ]

    @staticmethod
    def _primary_disk_lines(*, stats: Stats_System) -> list[str]:
        primary_disk = stats.primary_disk
        bot_disk = stats.bot_disk
        if primary_disk is None:
            return ["Unavailable"]

        source = {
            "override": "override",
            "bot_path": "bot",
            "fallback": "fallback",
        }[stats.primary_disk_source]
        lines = [
            f"now: {primary_disk.display_name}",
            f"src: {source}",
        ]
        if bot_disk is not None:
            lines.append(f"bot: {bot_disk.display_name}")
        if stats.configured_primary_mount is not None:
            lines.append(f"set: {stats.configured_primary_mount}")
        return lines

    @staticmethod
    def _activity_disk_lines(*, stats: Stats_System) -> list[str]:
        activity_disks = stats.activity_disks
        if stats.configured_activity_mounts is None:
            mode = "all"
        elif stats.configured_activity_mounts:
            mode = "custom"
        else:
            mode = "disabled"
        lines = [f"mode: {mode}", f"set: {len(activity_disks)}/{len(stats.disks)}"]
        lines.extend(f"{disk.display_name} | {disk.mountpoint_text}" for disk in activity_disks[:5])
        if len(activity_disks) > 5:
            lines.append(f"...and {len(activity_disks) - 5} more")
        return lines

    @staticmethod
    def _parse_discord_user_id(value: object, *, source: str) -> hikari.Snowflake:
        return hikari.Snowflake(config.normalise_discord_id_text(value, source=source))

    @staticmethod
    def _normalise_optional_single_line_text(value: object, *, source: str) -> str | None:
        text = str(value).strip()
        if not text:
            return None
        if "\n" in text or "\r" in text:
            raise ValueError(f"{source} must be a single line.")
        return text

    @staticmethod
    def _parse_storage_label_lines(
        raw_value: str,
        *,
        stats: Stats_System,
    ) -> dict[str, str]:
        labels: dict[str, str] = {}
        for index, raw_line in enumerate(raw_value.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue

            mountpoint_text, separator, label_text = stripped.partition("=")
            if not separator:
                raise ValueError(f"Line {index} must use `<mountpoint> = <label>`.")

            mountpoint = config.normalise_absolute_path_text(
                mountpoint_text.strip(),
                source=f"storage label line {index}",
            )
            if mountpoint not in {disk.mountpoint_text for disk in stats.disks}:
                raise ValueError(f"Line {index} uses an unknown mountpoint: {mountpoint}")

            labels[mountpoint] = label_text.strip()
        return labels

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
    def _require_maintenance(deps: Mapping[str, object]) -> MaintenanceService:
        value = deps.get("maintenance")
        if not isinstance(value, MaintenanceService):
            raise TypeError("Dashboard editor requires MaintenanceService")
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
        maintenance: MaintenanceService,
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
            maintenance=maintenance,
            is_public=self.public,
            manager=manager,
            names_cache=names_cache,
            stats=stats,
            tracker=tracker,
        )


# AiviA APasz
