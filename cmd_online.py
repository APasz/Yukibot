from __future__ import annotations

import enum
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

import hikari
import lightbulb
from hikari_ui import (
    Editor,
    EditorButton,
    EditorLayout,
    EditorPageState,
    EditorRequest,
    EditorResponse,
    EditorSelectOption,
    ModalKit,
    ModalRequest,
    ModalSchema,
    ModalTextField,
    PagedActionCodec,
)

import config
from _editor_session import startup_editor_prefix
from _file import File_Utils
from _security import Access_Control
from config import Name_Cache
from online import (
    ACTIVITY_TYPES,
    NICKNAME_MODES,
    NICKNAME_PLATFORMS,
    STATUS_TYPES,
    Online_Tracker,
    WatchRule,
)

log = logging.getLogger(__name__)

_ONLINE_EDITOR_PREFIX = "online-editor:"
_ONLINE_WATCH_GAME_MODAL_PREFIX = "online-watch-game:"
_ONLINE_DRINK_GAME_MODAL_PREFIX = "online-drink-game:"
_ONLINE_NICK_MODAL_PREFIX = "online-nick:"
_ONLINE_STEAM_MODAL_PREFIX = "online-steam:"

_GAME_MODAL_FIELD_ID = "game"
_NICK_MODAL_FIELD_ID = "nick"
_STEAM_MODAL_FIELD_ID = "steam_id"

_PAGE_SIZE = 25

ValueT = TypeVar("ValueT")


class OnlineActionKind(enum.StrEnum):
    ADD_DRINK_GAME = "dg"
    ADD_WATCH_GAME = "wg"
    CANCEL_PENDING_NICKNAME = "cn"
    CLOSE = "cl"
    CREATE_WATCH = "cw"
    CYCLE_WATCH_GAMES_MODE = "cm"
    EXPORT_CONFIG = "ex"
    OPEN_WATCH = "ow"
    PAGE = "pg"
    REFRESH = "rf"
    REMOVE_DRINK_GAME = "rd"
    REMOVE_NICKNAME = "rn"
    REMOVE_WATCH_GAME = "rg"
    SAVE_PENDING_NICKNAME = "sn"
    SET_DRINK_MODE = "dm"
    SET_PENDING_NICK_MODE = "nm"
    SET_PENDING_NICK_PLATFORM = "np"
    SHOW_ACCOUNT = "ac"
    SHOW_DRINK = "dr"
    SHOW_NICKNAMES = "ni"
    SHOW_OVERVIEW = "ov"
    SHOW_WATCHES = "wa"
    START_ADD_NICKNAME = "an"
    START_ADD_WATCH_GAME = "ag"
    START_ADD_DRINK_GAME = "ad"
    START_SET_STEAM = "ss"
    STOP_WATCHING = "sw"
    TOGGLE_FILTER = "tf"
    TOGGLE_IGNORE_ME = "ig"
    TOGGLE_WATCH_SILENT = "ts"
    UPSERT_WATCH = "uw"
    CLEAR_STEAM = "cs"


class OnlineEditorSection(enum.StrEnum):
    OVERVIEW = "ov"
    WATCHES = "wa"
    DRINK = "dr"
    NICKNAMES = "ni"
    ACCOUNT = "ac"


class OnlineEditorView(enum.StrEnum):
    ROOT = "rt"
    NICKNAME_CONFIG = "nc"


@dataclass(frozen=True, slots=True)
class OnlineEditorState:
    section: OnlineEditorSection
    page: int
    selected_target_id: hikari.Snowflake | None = None
    view: OnlineEditorView = OnlineEditorView.ROOT

    @property
    def is_watch_detail(self) -> bool:
        return self.section is OnlineEditorSection.WATCHES and self.selected_target_id is not None


@dataclass(slots=True)
class PendingNicknameRule:
    nick: str
    mode: str | None = None
    platform: str | None = None


@dataclass(frozen=True, slots=True)
class PagedItems(Generic[ValueT]):
    visible: tuple[ValueT, ...]
    total_count: int
    page_state: EditorPageState


@dataclass(frozen=True, slots=True)
class WatchListEntry:
    user_id: hikari.Snowflake
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class LabelValueEntry:
    label: str
    value: str
    description: str


def _editor_flags(is_public: bool) -> hikari.MessageFlag | hikari.UndefinedType:
    if is_public:
        return hikari.UNDEFINED
    return hikari.MessageFlag.EPHEMERAL


def _component_text(value: str, /, *, limit: int = 100) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    if limit <= 3:
        return stripped[:limit]
    return stripped[: limit - 3].rstrip() + "..."


def _display_value(values: Sequence[str]) -> str:
    return "\n".join(values) if values else "None"


def _page_count(count: int) -> int:
    return max(1, (count + _PAGE_SIZE - 1) // _PAGE_SIZE)


def _clamp_page(page: int, total_pages: int) -> int:
    if page < 0:
        return 0
    if page >= total_pages:
        return total_pages - 1
    return page


def _page_slice(values: Sequence[ValueT], page: int) -> Sequence[ValueT]:
    start = page * _PAGE_SIZE
    end = start + _PAGE_SIZE
    return values[start:end]


def _paginate(values: Sequence[ValueT], page: int, *, is_subpage: bool = False) -> PagedItems[ValueT]:
    total_pages = _page_count(len(values))
    current_page = _clamp_page(page, total_pages)
    return PagedItems(
        visible=tuple(_page_slice(values, current_page)),
        total_count=len(values),
        page_state=EditorPageState(page=current_page, total_pages=total_pages, is_subpage=is_subpage),
    )


def _page_for_value(values: Sequence[ValueT], needle: ValueT) -> int:
    try:
        index = values.index(needle)
    except ValueError:
        return 0
    return index // _PAGE_SIZE


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


def _user_label(names_cache: Name_Cache, user_id: hikari.Snowflake) -> str:
    return names_cache.cached_display_name(int(user_id), f"User {user_id}")


def _watch_counts(rule: WatchRule) -> str:
    game_count = len(rule.games) if rule.games_mode != "all" else 0
    return (
        f"{len(rule.types)} types, "
        f"{len(rule.activities)} activities, "
        f"{rule.games_mode} games ({game_count}), "
        f"default silent={rule.silent}, "
        f"selectors={len(rule.silent_rules)}"
    )


def _watch_rule_lines(
    tracker: Online_Tracker,
    *,
    target_id: hikari.Snowflake,
    rule: WatchRule,
) -> list[str]:
    games = (
        "all"
        if rule.games_mode == "all"
        else ", ".join(tracker.display_game(target_id, game) for game in sorted(rule.games, key=str.casefold)) or "(none)"
    )
    return [
        f"types: {', '.join(sorted(rule.types)) or '(none)'}",
        f"activities: {', '.join(sorted(rule.activities)) or '(none)'}",
        f"games mode: {rule.games_mode}",
        f"games: {games}",
        f"default silent: {rule.silent}",
        f"silent selectors: {len(rule.silent_rules)}",
    ]


def _drink_lines(tracker: Online_Tracker, user_id: hikari.Snowflake) -> list[str]:
    rule = tracker.get_drink_rule(user_id)
    if rule is None:
        return ["mode: include", "games: (none)"]
    games = ", ".join(tracker.display_game(user_id, game) for game in sorted(rule.games, key=str.casefold)) or "(none)"
    return [f"mode: {rule.mode}", f"games: {games}"]


def _nickname_lines(tracker: Online_Tracker, user_id: hikari.Snowflake) -> list[str]:
    entries = tracker.list_nickname_entries(user_id)
    if not entries:
        return ["nickname rules: (none)"]
    return [f"{mode}/{platform} -> {nick}" for mode, platform, nick in entries]


def _platform_lines(names_cache: Name_Cache, user_id: hikari.Snowflake) -> list[str]:
    rows = names_cache.list_platform_ids(int(user_id))
    if not rows:
        return ["platform ids: (none)"]
    return [f"{platform}: {platform_id}" for platform, platform_id in rows.items()]


def _section_label(section: OnlineEditorSection) -> str:
    if section is OnlineEditorSection.OVERVIEW:
        return "Overview"
    if section is OnlineEditorSection.WATCHES:
        return "Watches"
    if section is OnlineEditorSection.DRINK:
        return "Drink"
    if section is OnlineEditorSection.NICKNAMES:
        return "Nicknames"
    return "Accounts"


class OnlineEditorService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(OnlineActionKind)
        self._pending_nickname_rules: dict[hikari.Snowflake, PendingNicknameRule] = {}
        self._editor = Editor(
            prefix=startup_editor_prefix(_ONLINE_EDITOR_PREFIX),
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
        )
        self._watch_game_modal = ModalKit(
            prefix=startup_editor_prefix(_ONLINE_WATCH_GAME_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_GAME_MODAL_FIELD_ID,
                        label="Game",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                    )
                ]
            ),
        )
        self._drink_game_modal = ModalKit(
            prefix=startup_editor_prefix(_ONLINE_DRINK_GAME_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_GAME_MODAL_FIELD_ID,
                        label="Game",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                    )
                ]
            ),
        )
        self._nick_modal = ModalKit(
            prefix=startup_editor_prefix(_ONLINE_NICK_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_NICK_MODAL_FIELD_ID,
                        label="Nickname",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=32,
                    ),
                ]
            ),
        )
        self._steam_modal = ModalKit(
            prefix=startup_editor_prefix(_ONLINE_STEAM_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_STEAM_MODAL_FIELD_ID,
                        label="Steam ID",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                    )
                ]
            ),
        )

    async def open_editor(
        self,
        *,
        ctx: lightbulb.Context,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        focus_target_id: hikari.Snowflake | None = None,
        is_public: bool = False,
        status: str = "Manage online tracking below.",
    ) -> None:
        locale = self._editor.resolve_locale(ctx.interaction)
        state = self._initial_state(
            tracker=tracker,
            names_cache=names_cache,
            watcher_id=ctx.user.id,
            focus_target_id=focus_target_id,
        )
        embed, components = self._render_editor(
            watcher_id=ctx.user.id,
            locale=locale,
            tracker=tracker,
            names_cache=names_cache,
            state=state,
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
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        bot: hikari.GatewayBot,
    ) -> bool:
        return await self._editor.route(
            interaction,
            acl=acl,
            tracker=tracker,
            names_cache=names_cache,
            bot=bot,
        )

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        acl: Access_Control,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        bot: hikari.GatewayBot,
    ) -> bool:
        for kit, handler in (
            (self._watch_game_modal, self._on_watch_game_modal_submit),
            (self._drink_game_modal, self._on_drink_game_modal_submit),
            (self._nick_modal, self._on_nick_modal_submit),
            (self._steam_modal, self._on_steam_modal_submit),
        ):
            handled = await kit.route(
                interaction,
                on_submit=handler,
                authoriser=self._authorise_modal_submit,
                unauthorised_message="You are not authorised to use this online editor.",
                invalid_message="Submitted values were invalid.",
                acl=acl,
                tracker=tracker,
                names_cache=names_cache,
                bot=bot,
            )
            if handled:
                return True
        return False

    async def _authorise_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, deps)

    async def _authorise_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, deps)

    async def _authorise_request_user(
        self,
        actor_user_id: hikari.Snowflakeish,
        deps: Mapping[str, object],
    ) -> bool:
        acl = self._require_acl(deps)
        try:
            await acl.perm_check(actor_user_id, acl.LvL.guest)
        except Exception:
            return False
        return True

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        tracker = self._require_tracker(deps)
        names_cache = self._require_names_cache(deps)
        bot = self._require_bot(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown online editor action.")

        watcher_id = hikari.Snowflake(req.scope_id)
        actor_user_id = hikari.Snowflake(req.user_id)
        state, extra = self._state_and_extra_from_action(action)

        if action.kind is OnlineActionKind.CLOSE:
            return EditorResponse.close("Online editor closed.")

        if action.kind in {
            OnlineActionKind.ADD_DRINK_GAME,
            OnlineActionKind.ADD_WATCH_GAME,
            OnlineActionKind.CANCEL_PENDING_NICKNAME,
            OnlineActionKind.PAGE,
            OnlineActionKind.REFRESH,
            OnlineActionKind.EXPORT_CONFIG,
            OnlineActionKind.TOGGLE_IGNORE_ME,
            OnlineActionKind.CLEAR_STEAM,
            OnlineActionKind.CYCLE_WATCH_GAMES_MODE,
            OnlineActionKind.SET_DRINK_MODE,
            OnlineActionKind.SAVE_PENDING_NICKNAME,
            OnlineActionKind.SET_PENDING_NICK_MODE,
            OnlineActionKind.SET_PENDING_NICK_PLATFORM,
            OnlineActionKind.REMOVE_DRINK_GAME,
            OnlineActionKind.REMOVE_NICKNAME,
            OnlineActionKind.OPEN_WATCH,
            OnlineActionKind.CREATE_WATCH,
            OnlineActionKind.STOP_WATCHING,
            OnlineActionKind.TOGGLE_FILTER,
            OnlineActionKind.TOGGLE_WATCH_SILENT,
            OnlineActionKind.REMOVE_WATCH_GAME,
            OnlineActionKind.START_ADD_NICKNAME,
            OnlineActionKind.START_ADD_WATCH_GAME,
            OnlineActionKind.START_ADD_DRINK_GAME,
            OnlineActionKind.START_SET_STEAM,
            OnlineActionKind.UPSERT_WATCH,
        } and state is None:
            return EditorResponse.ephemeral("Online editor state is invalid.")

        if action.kind is OnlineActionKind.PAGE:
            assert state is not None
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status="Page updated.",
            )

        if action.kind is OnlineActionKind.REFRESH:
            assert state is not None
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status="Online editor refreshed.",
            )

        if action.kind is OnlineActionKind.SHOW_OVERVIEW:
            return self._show_section(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                section=OnlineEditorSection.OVERVIEW,
                status="Showing overview.",
            )

        if action.kind is OnlineActionKind.SHOW_WATCHES:
            return self._show_section(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                section=OnlineEditorSection.WATCHES,
                status="Showing watches.",
            )

        if action.kind is OnlineActionKind.SHOW_DRINK:
            return self._show_section(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                section=OnlineEditorSection.DRINK,
                status="Showing drink reminders.",
            )

        if action.kind is OnlineActionKind.SHOW_NICKNAMES:
            return self._show_section(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                section=OnlineEditorSection.NICKNAMES,
                status="Showing nickname rules.",
            )

        if action.kind is OnlineActionKind.SHOW_ACCOUNT:
            return self._show_section(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                section=OnlineEditorSection.ACCOUNT,
                status="Showing platform IDs.",
            )

        if action.kind is OnlineActionKind.EXPORT_CONFIG:
            assert state is not None
            export_target_id = state.selected_target_id if state.is_watch_detail else None
            export_sent = await self._send_config_export_dm(
                bot=bot,
                tracker=tracker,
                names_cache=names_cache,
                watcher_id=watcher_id,
                export_target_id=export_target_id,
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status="Export sent by DM." if export_sent else "Could not send the export DM. Check your DM settings.",
            )

        if action.kind is OnlineActionKind.TOGGLE_IGNORE_ME:
            assert state is not None
            now_ignored = tracker.toggle_ignored_user(watcher_id)
            status = (
                "You are now ignored by online tracking."
                if now_ignored
                else "You are no longer ignored by online tracking."
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=status,
            )

        if action.kind is OnlineActionKind.UPSERT_WATCH:
            assert state is not None
            if not req.values:
                return EditorResponse.ephemeral("Choose a user to watch first.")
            target_id = _extract_user_id(req.values[0])
            if target_id is None:
                return EditorResponse.ephemeral("Invalid target user.")
            if tracker.is_ignored_user(target_id):
                return EditorResponse.ephemeral("That user is ignored.")
            tracker.ensure_rule(watcher_id, target_id)
            watch_entries = self._watch_entries(tracker=tracker, names_cache=names_cache, watcher_id=watcher_id)
            page = _page_for_value(watch_entries, self._watch_entry_for_target(watch_entries, target_id))
            next_state = OnlineEditorState(
                section=OnlineEditorSection.WATCHES,
                page=page,
                selected_target_id=target_id,
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=next_state,
                status=f"Watching {_user_label(names_cache, target_id)}.",
            )

        if action.kind is OnlineActionKind.OPEN_WATCH:
            assert state is not None
            if not req.values:
                return EditorResponse.ephemeral("Choose a watched user first.")
            target_id = _extract_user_id(req.values[0])
            if target_id is None:
                return EditorResponse.ephemeral("Invalid watched user.")
            next_state = OnlineEditorState(
                section=OnlineEditorSection.WATCHES,
                page=state.page,
                selected_target_id=target_id,
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=next_state,
                status=f"Editing {_user_label(names_cache, target_id)}.",
            )

        if action.kind is OnlineActionKind.CREATE_WATCH:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("Choose a user to watch first.")
            if tracker.is_ignored_user(state.selected_target_id):
                return EditorResponse.ephemeral("That user is ignored.")
            tracker.ensure_rule(watcher_id, state.selected_target_id)
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=f"Watching {_user_label(names_cache, state.selected_target_id)}.",
            )

        if action.kind is OnlineActionKind.STOP_WATCHING:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("No watched user selected.")
            label = _user_label(names_cache, state.selected_target_id)
            removed = tracker.remove_rule(watcher_id, state.selected_target_id)
            list_state = OnlineEditorState(section=OnlineEditorSection.WATCHES, page=state.page)
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=list_state,
                status=f"Stopped watching {label}." if removed else f"No watch config found for {label}.",
            )

        if action.kind is OnlineActionKind.TOGGLE_FILTER:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("No watched user selected.")
            if not req.values:
                return EditorResponse.ephemeral("Choose a filter to toggle first.")
            tracker.ensure_rule(watcher_id, state.selected_target_id)
            selector = req.values[0].strip()
            changed_status = self._toggle_watch_filter(
                tracker=tracker,
                watcher_id=watcher_id,
                target_id=state.selected_target_id,
                selector=selector,
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=changed_status,
            )

        if action.kind is OnlineActionKind.TOGGLE_WATCH_SILENT:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("No watched user selected.")
            rule, _ = tracker.ensure_rule(watcher_id, state.selected_target_id)
            changed = tracker.set_rule_silent(watcher_id, state.selected_target_id, not rule.silent)
            current_rule = tracker.get_rule(watcher_id, state.selected_target_id)
            next_silent = current_rule.silent if current_rule is not None else (not rule.silent)
            status = (
                f"Default silent set to `{next_silent}`."
                if changed
                else f"Default silent is already `{rule.silent}`."
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=status,
            )

        if action.kind is OnlineActionKind.CYCLE_WATCH_GAMES_MODE:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("No watched user selected.")
            rule, _ = tracker.ensure_rule(watcher_id, state.selected_target_id)
            next_mode = (
                "include"
                if rule.games_mode == "all"
                else "exclude"
                if rule.games_mode == "include"
                else "all"
            )
            tracker.set_rule_games_mode(watcher_id, state.selected_target_id, next_mode)
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=f"Watch games mode set to `{next_mode}`.",
            )

        if action.kind is OnlineActionKind.START_ADD_WATCH_GAME:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("No watched user selected.")
            rule = tracker.get_rule(watcher_id, state.selected_target_id)
            if rule is None:
                return EditorResponse.ephemeral("Create the watch first.")
            if rule.games_mode == "all":
                return EditorResponse.ephemeral("Pick `include` or `exclude` games mode first.")
            await req.interaction.create_modal_response(
                "Add Watch Game",
                self._watch_game_modal.build_id(
                    self._build_state_action(OnlineActionKind.START_ADD_WATCH_GAME, state),
                    scope_id=watcher_id,
                    user_id=actor_user_id,
                ),
                components=self._watch_game_modal.rows(),
            )
            return None

        if action.kind is OnlineActionKind.ADD_WATCH_GAME:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("No watched user selected.")
            if not req.values:
                return EditorResponse.ephemeral("Choose a seen game to add first.")
            rule = tracker.get_rule(watcher_id, state.selected_target_id)
            if rule is None:
                return EditorResponse.ephemeral("Create the watch first.")
            if rule.games_mode == "all":
                return EditorResponse.ephemeral("Pick `include` or `exclude` games mode first.")
            game = req.values[0]
            result = (
                tracker.add_game(watcher_id, state.selected_target_id, game)
                if rule.games_mode == "include"
                else tracker.remove_game(watcher_id, state.selected_target_id, game)
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=result if result != "no change" else f"`{tracker.display_game(state.selected_target_id, game)}` is already configured.",
            )

        if action.kind is OnlineActionKind.REMOVE_WATCH_GAME:
            assert state is not None
            if state.selected_target_id is None:
                return EditorResponse.ephemeral("No watched user selected.")
            if not req.values:
                return EditorResponse.ephemeral("Choose a game filter to remove first.")
            rule = tracker.get_rule(watcher_id, state.selected_target_id)
            if rule is None or rule.games_mode == "all":
                return EditorResponse.ephemeral("This watch has no game filter list.")
            game = req.values[0]
            result = (
                tracker.remove_game(watcher_id, state.selected_target_id, game)
                if rule.games_mode == "include"
                else tracker.add_game(watcher_id, state.selected_target_id, game)
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=result if result != "no change" else "No matching game filter was set.",
            )

        if action.kind is OnlineActionKind.SET_DRINK_MODE:
            assert state is not None
            if extra is None:
                return EditorResponse.ephemeral("Drink mode is invalid.")
            changed = tracker.set_drink_mode(watcher_id, extra)
            status = f"Drink mode set to `{extra}`." if changed else f"Drink mode is already `{extra}`."
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=status,
            )

        if action.kind is OnlineActionKind.START_ADD_DRINK_GAME:
            assert state is not None
            await req.interaction.create_modal_response(
                "Add Drink Reminder Game",
                self._drink_game_modal.build_id(
                    self._build_state_action(OnlineActionKind.START_ADD_DRINK_GAME, state),
                    scope_id=watcher_id,
                    user_id=actor_user_id,
                ),
                components=self._drink_game_modal.rows(),
            )
            return None

        if action.kind is OnlineActionKind.ADD_DRINK_GAME:
            assert state is not None
            if not req.values:
                return EditorResponse.ephemeral("Choose a seen game to add first.")
            game = req.values[0]
            changed = tracker.add_drink_game(watcher_id, game)
            display = tracker.display_game(watcher_id, game)
            status = f"Added drink reminder game `{display}`." if changed else f"`{display}` is already configured."
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=status,
            )

        if action.kind is OnlineActionKind.REMOVE_DRINK_GAME:
            assert state is not None
            if not req.values:
                return EditorResponse.ephemeral("Choose a drink reminder game to remove first.")
            game = req.values[0]
            removed = tracker.remove_drink_game(watcher_id, game)
            display = tracker.display_game(watcher_id, game)
            status = f"Removed drink reminder game `{display}`." if removed else f"`{display}` was not configured."
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=status,
            )

        if action.kind is OnlineActionKind.START_ADD_NICKNAME:
            assert state is not None
            await req.interaction.create_modal_response(
                "Add Nickname Rule",
                self._nick_modal.build_id(
                    self._build_state_action(OnlineActionKind.START_ADD_NICKNAME, state),
                    scope_id=watcher_id,
                    user_id=actor_user_id,
                ),
                components=self._nick_modal.rows(),
            )
            return None

        if action.kind is OnlineActionKind.SET_PENDING_NICK_MODE:
            assert state is not None
            if not req.values:
                return EditorResponse.ephemeral("Choose a nickname mode first.")
            pending = self._pending_nickname_rules.get(watcher_id)
            if pending is None:
                return EditorResponse.ephemeral("Nickname setup expired. Start again.")
            mode = req.values[0].strip().lower()
            if mode not in NICKNAME_MODES:
                return EditorResponse.ephemeral(f"Unknown nickname mode `{mode}`.")
            pending.mode = mode
            if mode == "offline":
                pending.platform = "all"
            elif pending.platform not in NICKNAME_PLATFORMS:
                pending.platform = None
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=f"Nickname mode set to `{mode}`.",
            )

        if action.kind is OnlineActionKind.SET_PENDING_NICK_PLATFORM:
            assert state is not None
            if not req.values:
                return EditorResponse.ephemeral("Choose a nickname platform first.")
            pending = self._pending_nickname_rules.get(watcher_id)
            if pending is None:
                return EditorResponse.ephemeral("Nickname setup expired. Start again.")
            platform = req.values[0].strip().lower()
            allowed_platforms = self._nickname_platform_options(pending.mode)
            if platform not in allowed_platforms:
                return EditorResponse.ephemeral(f"Platform `{platform}` is not valid for the selected mode.")
            pending.platform = platform
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=f"Nickname platform set to `{platform}`.",
            )

        if action.kind is OnlineActionKind.SAVE_PENDING_NICKNAME:
            assert state is not None
            pending = self._pending_nickname_rules.get(watcher_id)
            if pending is None:
                return EditorResponse.ephemeral("Nickname setup expired. Start again.")
            if pending.mode is None or pending.platform is None:
                return EditorResponse.ephemeral("Choose both a mode and a platform first.")
            changed = tracker.set_nick_rule(watcher_id, pending.nick, pending.mode, pending.platform)
            await tracker.refresh_nickname(watcher_id, bot)
            self._pending_nickname_rules.pop(watcher_id, None)
            root_state = OnlineEditorState(section=OnlineEditorSection.NICKNAMES, page=0)
            status = (
                f"Saved nickname rule `{pending.mode}/{pending.platform}` -> `{pending.nick}`."
                if changed
                else (
                    f"No change: `{pending.mode}/{pending.platform}` already points to `{pending.nick}`."
                )
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=root_state,
                status=status,
            )

        if action.kind is OnlineActionKind.CANCEL_PENDING_NICKNAME:
            assert state is not None
            self._pending_nickname_rules.pop(watcher_id, None)
            root_state = OnlineEditorState(section=OnlineEditorSection.NICKNAMES, page=0)
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=root_state,
                status="Nickname setup cancelled.",
            )

        if action.kind is OnlineActionKind.REMOVE_NICKNAME:
            assert state is not None
            if not req.values:
                return EditorResponse.ephemeral("Choose a nickname rule to remove first.")
            token = req.values[0]
            label = tracker.describe_nick_clear_token(token)
            removed = tracker.clear_nick_by_token(watcher_id, token)
            await tracker.refresh_nickname(watcher_id, bot, force_clear=not tracker.nick_rules.get(watcher_id))
            status = (
                f"Removed `{label}`."
                if removed > 0
                else f"No matching nickname rule found for `{label}`."
            )
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=status,
            )

        if action.kind is OnlineActionKind.START_SET_STEAM:
            assert state is not None
            current = names_cache.get_platform_id(int(watcher_id), "steam") or ""
            await req.interaction.create_modal_response(
                "Set Steam ID",
                self._steam_modal.build_id(
                    self._build_state_action(OnlineActionKind.START_SET_STEAM, state),
                    scope_id=watcher_id,
                    user_id=actor_user_id,
                ),
                components=self._steam_modal.rows({_STEAM_MODAL_FIELD_ID: current}),
            )
            return None

        if action.kind is OnlineActionKind.CLEAR_STEAM:
            assert state is not None
            changed = names_cache.set_platform_id(int(watcher_id), "steam", None)
            status = "Cleared Steam ID." if changed else "No Steam ID was set."
            return self._build_editor_response(
                watcher_id=watcher_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
                status=status,
            )

        return EditorResponse.ephemeral("Unsupported online editor action.")

    async def _on_watch_game_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        tracker = self._require_tracker(deps)
        names_cache = self._require_names_cache(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown watch game modal action.")
        state, _ = self._state_and_extra_from_action(action)
        if state is None or state.selected_target_id is None:
            return EditorResponse.ephemeral("Watch game editor state is invalid.")

        rule = tracker.get_rule(req.scope_id, state.selected_target_id)
        if rule is None:
            return EditorResponse.ephemeral("Create the watch first.")
        if rule.games_mode == "all":
            return EditorResponse.ephemeral("Pick `include` or `exclude` games mode first.")

        game = req.values.get(_GAME_MODAL_FIELD_ID, "").strip()
        if not game:
            return EditorResponse.ephemeral("Game must not be empty.")

        result = (
            tracker.add_game(req.scope_id, state.selected_target_id, game)
            if rule.games_mode == "include"
            else tracker.remove_game(req.scope_id, state.selected_target_id, game)
        )
        return self._build_editor_response(
            watcher_id=req.scope_id,
            actor_user_id=req.user_id,
            locale=self._editor.resolve_locale(req.interaction),
            tracker=tracker,
            names_cache=names_cache,
            state=state,
            status=result if result != "no change" else f"`{game}` is already configured.",
        )

    async def _on_drink_game_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        tracker = self._require_tracker(deps)
        names_cache = self._require_names_cache(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown drink modal action.")
        state, _ = self._state_and_extra_from_action(action)
        if state is None:
            return EditorResponse.ephemeral("Drink editor state is invalid.")

        game = req.values.get(_GAME_MODAL_FIELD_ID, "").strip()
        if not game:
            return EditorResponse.ephemeral("Game must not be empty.")

        changed = tracker.add_drink_game(req.scope_id, game)
        display = tracker.display_game(req.scope_id, game)
        status = f"Added drink reminder game `{display}`." if changed else f"`{display}` is already configured."
        return self._build_editor_response(
            watcher_id=req.scope_id,
            actor_user_id=req.user_id,
            locale=self._editor.resolve_locale(req.interaction),
            tracker=tracker,
            names_cache=names_cache,
            state=state,
            status=status,
        )

    async def _on_nick_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        tracker = self._require_tracker(deps)
        names_cache = self._require_names_cache(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown nickname modal action.")
        state, _ = self._state_and_extra_from_action(action)
        if state is None:
            return EditorResponse.ephemeral("Nickname editor state is invalid.")

        nick = req.values.get(_NICK_MODAL_FIELD_ID, "").strip()
        if not nick:
            return EditorResponse.ephemeral("Nickname must not be empty.")
        self._pending_nickname_rules[req.scope_id] = PendingNicknameRule(nick=nick)
        next_state = OnlineEditorState(
            section=OnlineEditorSection.NICKNAMES,
            page=0,
            view=OnlineEditorView.NICKNAME_CONFIG,
        )
        return self._build_editor_response(
            watcher_id=req.scope_id,
            actor_user_id=req.user_id,
            locale=self._editor.resolve_locale(req.interaction),
            tracker=tracker,
            names_cache=names_cache,
            state=next_state,
            status=f"Choose mode and platform for `{nick}`.",
        )

    async def _on_steam_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        tracker = self._require_tracker(deps)
        names_cache = self._require_names_cache(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown platform modal action.")
        state, _ = self._state_and_extra_from_action(action)
        if state is None:
            return EditorResponse.ephemeral("Platform editor state is invalid.")

        steam_id = req.values.get(_STEAM_MODAL_FIELD_ID, "").strip()
        if not steam_id:
            return EditorResponse.ephemeral("Steam ID must not be empty.")
        changed = names_cache.set_platform_id(int(req.scope_id), "steam", steam_id)
        current = names_cache.get_platform_id(int(req.scope_id), "steam")
        status = (
            f"Set Steam ID to `{current}`."
            if changed and current
            else f"Steam ID is already `{current}`."
        )
        return self._build_editor_response(
            watcher_id=req.scope_id,
            actor_user_id=req.user_id,
            locale=self._editor.resolve_locale(req.interaction),
            tracker=tracker,
            names_cache=names_cache,
            state=state,
            status=status,
        )

    def _show_section(
        self,
        *,
        watcher_id: hikari.Snowflake,
        actor_user_id: hikari.Snowflake,
        locale: hikari.Locale,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        section: OnlineEditorSection,
        status: str,
    ) -> EditorResponse:
        return self._build_editor_response(
            watcher_id=watcher_id,
            actor_user_id=actor_user_id,
            locale=locale,
            tracker=tracker,
            names_cache=names_cache,
            state=OnlineEditorState(section=section, page=0),
            status=status,
        )

    def _build_editor_response(
        self,
        *,
        watcher_id: hikari.Snowflake,
        actor_user_id: hikari.Snowflake,
        locale: hikari.Locale,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        state: OnlineEditorState,
        status: str,
    ) -> EditorResponse:
        embed, components = self._render_editor(
            watcher_id=watcher_id,
            locale=locale,
            tracker=tracker,
            names_cache=names_cache,
            state=state,
        )
        return EditorResponse.update(status, components=components, embeds=[embed])

    def _render_editor(
        self,
        *,
        watcher_id: hikari.Snowflake,
        locale: hikari.Locale,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        state: OnlineEditorState,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        drink_rule = tracker.get_drink_rule(watcher_id)
        drink_game_count = len(drink_rule.games) if drink_rule is not None else 0
        embed = hikari.Embed(
            title="Your Online Config",
            description=(
                f"Section: {_section_label(state.section)}\n"
                f"Watches: {len(tracker.list_rules(watcher_id))} | "
                f"Drink games: {drink_game_count} | "
                f"Nickname rules: {len(tracker.list_nickname_entries(watcher_id))}"
            ),
            color=0x2D8C7F,
        )
        layout = EditorLayout(
            self._editor.context(
                scope_id=watcher_id,
                user_id=watcher_id,
                locale=locale,
            )
        )
        self._add_section_buttons(layout=layout, state=state)
        layout.next_row()

        if state.section is OnlineEditorSection.OVERVIEW:
            self._render_overview_section(
                embed=embed,
                layout=layout,
                watcher_id=watcher_id,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
            )
        elif state.section is OnlineEditorSection.WATCHES:
            self._render_watches_section(
                embed=embed,
                layout=layout,
                watcher_id=watcher_id,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
            )
        elif state.section is OnlineEditorSection.DRINK:
            self._render_drink_section(
                embed=embed,
                layout=layout,
                watcher_id=watcher_id,
                tracker=tracker,
                state=state,
            )
        elif state.section is OnlineEditorSection.NICKNAMES:
            if state.view is OnlineEditorView.NICKNAME_CONFIG:
                self._render_nickname_config_section(
                    embed=embed,
                    layout=layout,
                    watcher_id=watcher_id,
                    tracker=tracker,
                    state=state,
                )
            else:
                self._render_nicknames_section(
                    embed=embed,
                    layout=layout,
                    watcher_id=watcher_id,
                    tracker=tracker,
                    state=state,
                )
        else:
            self._render_account_section(
                embed=embed,
                layout=layout,
                watcher_id=watcher_id,
                names_cache=names_cache,
                state=state,
            )

        return embed, layout.build()

    def _render_overview_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        watcher_id: hikari.Snowflake,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        state: OnlineEditorState,
    ) -> None:
        watch_entries = self._watch_entries(tracker=tracker, names_cache=names_cache, watcher_id=watcher_id)
        embed.add_field(
            name=f"Watched Users ({len(watch_entries)})",
            value=_display_value([entry.label for entry in watch_entries[:10]]),
            inline=False,
        )
        embed.add_field(name="Drink", value=_display_value(_drink_lines(tracker, watcher_id)), inline=False)
        embed.add_field(name="Nicknames", value=_display_value(_nickname_lines(tracker, watcher_id)), inline=False)
        embed.add_field(name="Platform IDs", value=_display_value(_platform_lines(names_cache, watcher_id)), inline=False)
        embed.add_field(
            name="Notes",
            value=(
                "Use export to edit advanced watch JSON such as silent selectors.\n"
                "JSON imports still go through `/online file:<attachment>`."
            ),
            inline=False,
        )

        layout.add_buttons(
            EditorButton(
                self._build_state_action(OnlineActionKind.EXPORT_CONFIG, state),
                "Export JSON",
                style=hikari.ButtonStyle.PRIMARY,
            ),
            EditorButton(
                self._build_state_action(OnlineActionKind.TOGGLE_IGNORE_ME, state),
                "Toggle Ignore Me",
            ),
        )
        layout.page_footer(
            self._action_codec.build(OnlineActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1),
            extra_buttons=(
                EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),
            ),
        )

    def _render_watches_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        watcher_id: hikari.Snowflake,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        state: OnlineEditorState,
    ) -> None:
        if state.selected_target_id is not None:
            self._render_watch_detail(
                embed=embed,
                layout=layout,
                watcher_id=watcher_id,
                tracker=tracker,
                names_cache=names_cache,
                state=state,
            )
            return

        entries = self._watch_entries(tracker=tracker, names_cache=names_cache, watcher_id=watcher_id)
        page = _paginate(entries, state.page)
        embed.add_field(
            name=f"Watched Users ({page.total_count})",
            value=_display_value([f"{entry.label}: {entry.description}" for entry in page.visible]),
            inline=False,
        )
        layout.add_user_select(
            self._build_state_action(OnlineActionKind.UPSERT_WATCH, state),
            placeholder="Add or reopen a watch by user",
        )

        prev_action = None
        next_action = None
        if page.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                OnlineActionKind.PAGE,
                OnlineEditorState(section=OnlineEditorSection.WATCHES, page=max(0, page.page_state.page - 1)),
            )
            next_action = self._build_state_action(
                OnlineActionKind.PAGE,
                OnlineEditorState(
                    section=OnlineEditorSection.WATCHES,
                    page=min(page.page_state.total_pages - 1, page.page_state.page + 1),
                ),
            )
        layout.page_footer(
            self._action_codec.build(OnlineActionKind.CLOSE, page=page.page_state.page),
            page_state=page.page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),
            ),
        )

    def _render_watch_detail(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        watcher_id: hikari.Snowflake,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        state: OnlineEditorState,
    ) -> None:
        assert state.selected_target_id is not None
        target_id = state.selected_target_id
        target_label = _user_label(names_cache, target_id)
        rule = tracker.get_rule(watcher_id, target_id)
        embed.title = f"Watch: {target_label}"
        if rule is None:
            embed.description = "No watch config exists for this user yet."
            layout.add_button(
                self._build_state_action(OnlineActionKind.CREATE_WATCH, state),
                "Start Watching",
                style=hikari.ButtonStyle.PRIMARY,
            )
            layout.page_footer(
                self._action_codec.build(OnlineActionKind.CLOSE, page=state.page),
                page_state=EditorPageState(page=state.page, total_pages=max(1, _page_count(len(self._watch_entries(tracker=tracker, names_cache=names_cache, watcher_id=watcher_id)))), is_subpage=True),
                back_action=self._build_state_action(
                    OnlineActionKind.PAGE,
                    OnlineEditorState(section=OnlineEditorSection.WATCHES, page=state.page),
                ),
                extra_buttons=(EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),),
            )
            return

        embed.description = _watch_counts(rule)
        embed.add_field(name="Rule", value=_display_value(_watch_rule_lines(tracker, target_id=target_id, rule=rule)), inline=False)
        filter_options = [
            EditorSelectOption(
                label=_component_text(f"{'[x]' if status_type in rule.types else '[ ]'} type:{status_type}"),
                value=f"type:{status_type}",
                description="Toggle status filter",
            )
            for status_type in STATUS_TYPES
        ]
        filter_options.extend(
            EditorSelectOption(
                label=_component_text(f"{'[x]' if activity in rule.activities else '[ ]'} activity:{activity}"),
                value=f"activity:{activity}",
                description="Toggle activity filter",
            )
            for activity in ACTIVITY_TYPES
        )

        layout.add_buttons(
            EditorButton(
                self._build_state_action(OnlineActionKind.CYCLE_WATCH_GAMES_MODE, state),
                f"Games: {rule.games_mode.title()}",
                style=hikari.ButtonStyle.PRIMARY,
            ),
            EditorButton(
                self._build_state_action(OnlineActionKind.TOGGLE_WATCH_SILENT, state),
                f"Silent: {rule.silent}",
            ),
        )
        if rule.games_mode != "all":
            layout.add_button(
                self._build_state_action(OnlineActionKind.START_ADD_WATCH_GAME, state),
                "Add Game",
                style=hikari.ButtonStyle.PRIMARY,
            )
        layout.next_row()
        layout.add_text_select(
            self._build_state_action(OnlineActionKind.TOGGLE_FILTER, state),
            options=filter_options[:25],
            placeholder="Toggle type/activity filters",
        )
        seen_games = tracker.list_games_for_user(target_id)
        visible_seen_games = seen_games[:25]
        if rule.games_mode != "all" and visible_seen_games:
            layout.add_text_select(
                self._build_state_action(OnlineActionKind.ADD_WATCH_GAME, state),
                options=[
                    EditorSelectOption(
                        label=_component_text(tracker.display_game(target_id, game)),
                        value=game,
                        description=(
                            f"Add to {rule.games_mode} games"
                            if game.casefold() not in rule.games
                            else f"Already in {rule.games_mode} games"
                        ),
                    )
                    for game in visible_seen_games
                ],
                placeholder=f"Add seen game to {rule.games_mode} list",
            )
        visible_games = sorted(rule.games, key=str.casefold)[:25]
        if visible_games:
            layout.add_text_select(
                self._build_state_action(OnlineActionKind.REMOVE_WATCH_GAME, state),
                options=[
                    EditorSelectOption(
                        label=_component_text(tracker.display_game(target_id, game)),
                        value=game,
                        description=f"Remove from {rule.games_mode} games",
                    )
                    for game in visible_games
                ],
                placeholder=f"Remove {rule.games_mode} game filter",
            )
        layout.page_footer(
            self._action_codec.build(OnlineActionKind.CLOSE, page=state.page),
            page_state=EditorPageState(
                page=state.page,
                total_pages=max(1, _page_count(len(self._watch_entries(tracker=tracker, names_cache=names_cache, watcher_id=watcher_id)))),
                is_subpage=True,
            ),
            back_action=self._build_state_action(
                OnlineActionKind.PAGE,
                OnlineEditorState(section=OnlineEditorSection.WATCHES, page=state.page),
            ),
            extra_buttons=(
                EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),
                EditorButton(
                    self._build_state_action(OnlineActionKind.STOP_WATCHING, state),
                    "Stop Watching",
                    style=hikari.ButtonStyle.DANGER,
                ),
            ),
        )

    def _render_drink_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        watcher_id: hikari.Snowflake,
        tracker: Online_Tracker,
        state: OnlineEditorState,
    ) -> None:
        rule = tracker.get_drink_rule(watcher_id)
        mode = rule.mode if rule is not None else "include"
        configured_games = rule.games if rule is not None else set()
        games = tuple(
            LabelValueEntry(
                label=tracker.display_game(watcher_id, game),
                value=game,
                description="Remove from drink reminders",
            )
            for game in sorted(configured_games, key=str.casefold)
        )
        seen_games = tuple(
            LabelValueEntry(
                label=tracker.display_game(watcher_id, game),
                value=game,
                description=(
                    "Already in drink reminders"
                    if game.casefold() in configured_games
                    else "Add to drink reminders"
                ),
            )
            for game in tracker.list_games()
        )
        total_pages = max(_page_count(len(games)), _page_count(len(seen_games)))
        current_page = _clamp_page(state.page, total_pages)
        tracked_page = PagedItems(
            visible=tuple(_page_slice(games, current_page)),
            total_count=len(games),
            page_state=EditorPageState(page=current_page, total_pages=total_pages),
        )
        seen_page = PagedItems(
            visible=tuple(_page_slice(seen_games, current_page)),
            total_count=len(seen_games),
            page_state=EditorPageState(page=current_page, total_pages=total_pages),
        )
        embed.add_field(name="Drink Rule", value=_display_value(_drink_lines(tracker, watcher_id)), inline=False)
        embed.add_field(
            name=f"Tracked Games ({tracked_page.total_count})",
            value=_display_value([entry.label for entry in tracked_page.visible]),
            inline=False,
        )
        layout.add_buttons(
            EditorButton(
                self._build_state_action_with_value(OnlineActionKind.SET_DRINK_MODE, state, "include"),
                "Mode: Include",
                style=hikari.ButtonStyle.PRIMARY if mode == "include" else hikari.ButtonStyle.SECONDARY,
                is_disabled=mode == "include",
            ),
            EditorButton(
                self._build_state_action_with_value(OnlineActionKind.SET_DRINK_MODE, state, "exclude"),
                "Mode: Exclude",
                style=hikari.ButtonStyle.PRIMARY if mode == "exclude" else hikari.ButtonStyle.SECONDARY,
                is_disabled=mode == "exclude",
            ),
            EditorButton(
                self._build_state_action(OnlineActionKind.START_ADD_DRINK_GAME, state),
                "Add Game",
                style=hikari.ButtonStyle.PRIMARY,
            ),
        )
        if tracked_page.visible:
            layout.next_row()
            layout.add_text_select(
                self._build_state_action(OnlineActionKind.REMOVE_DRINK_GAME, state),
                options=[
                    EditorSelectOption(
                        label=_component_text(entry.label),
                        value=entry.value,
                        description=entry.description,
                    )
                    for entry in tracked_page.visible
                ],
                placeholder="Remove drink reminder game",
            )
        if seen_page.visible:
            layout.add_text_select(
                self._build_state_action(OnlineActionKind.ADD_DRINK_GAME, state),
                options=[
                    EditorSelectOption(
                        label=_component_text(entry.label),
                        value=entry.value,
                        description=entry.description,
                    )
                    for entry in seen_page.visible
                ],
                placeholder="Add seen game",
            )
        prev_action = None
        next_action = None
        if tracked_page.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                OnlineActionKind.PAGE,
                OnlineEditorState(
                    section=OnlineEditorSection.DRINK,
                    page=max(0, tracked_page.page_state.page - 1),
                ),
            )
            next_action = self._build_state_action(
                OnlineActionKind.PAGE,
                OnlineEditorState(
                    section=OnlineEditorSection.DRINK,
                    page=min(tracked_page.page_state.total_pages - 1, tracked_page.page_state.page + 1),
                ),
            )
        layout.page_footer(
            self._action_codec.build(OnlineActionKind.CLOSE, page=tracked_page.page_state.page),
            page_state=tracked_page.page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),),
        )

    def _render_nicknames_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        watcher_id: hikari.Snowflake,
        tracker: Online_Tracker,
        state: OnlineEditorState,
    ) -> None:
        options = tuple(
            LabelValueEntry(label=label, value=token, description="Remove nickname rule")
            for label, token in tracker.list_nickname_clear_options(watcher_id)
        )
        page = _paginate(options, state.page)
        embed.add_field(name="Nickname Rules", value=_display_value(_nickname_lines(tracker, watcher_id)), inline=False)
        layout.add_button(
            self._build_state_action(OnlineActionKind.START_ADD_NICKNAME, state),
            "Add Rule",
            style=hikari.ButtonStyle.PRIMARY,
        )
        if page.visible:
            layout.next_row()
            layout.add_text_select(
                self._build_state_action(OnlineActionKind.REMOVE_NICKNAME, state),
                options=[
                    EditorSelectOption(
                        label=_component_text(entry.label),
                        value=entry.value,
                        description=entry.description,
                    )
                    for entry in page.visible
                ],
                placeholder="Remove nickname rule",
            )
        prev_action = None
        next_action = None
        if page.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                OnlineActionKind.PAGE,
                OnlineEditorState(section=OnlineEditorSection.NICKNAMES, page=max(0, page.page_state.page - 1)),
            )
            next_action = self._build_state_action(
                OnlineActionKind.PAGE,
                OnlineEditorState(
                    section=OnlineEditorSection.NICKNAMES,
                    page=min(page.page_state.total_pages - 1, page.page_state.page + 1),
                ),
            )
        layout.page_footer(
            self._action_codec.build(OnlineActionKind.CLOSE, page=page.page_state.page),
            page_state=page.page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),),
        )

    def _render_nickname_config_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        watcher_id: hikari.Snowflake,
        tracker: Online_Tracker,
        state: OnlineEditorState,
    ) -> None:
        pending = self._pending_nickname_rules.get(watcher_id)
        if pending is None:
            self._render_nicknames_section(
                embed=embed,
                layout=layout,
                watcher_id=watcher_id,
                tracker=tracker,
                state=OnlineEditorState(section=OnlineEditorSection.NICKNAMES, page=0),
            )
            return

        embed.title = "Add Nickname Rule"
        embed.description = "Choose a mode and platform for the pending nickname."
        embed.add_field(name="Nickname", value=f"`{pending.nick}`", inline=False)
        embed.add_field(name="Mode", value=pending.mode or "(not selected)", inline=True)
        embed.add_field(name="Platform", value=pending.platform or "(not selected)", inline=True)
        embed.add_field(name="Existing Rules", value=_display_value(_nickname_lines(tracker, watcher_id)), inline=False)

        layout.add_text_select(
            self._build_state_action(OnlineActionKind.SET_PENDING_NICK_MODE, state),
            options=[
                EditorSelectOption(
                    label=_component_text(mode),
                    value=mode,
                    description="Use this presence mode",
                    is_default=mode == pending.mode,
                )
                for mode in NICKNAME_MODES
            ],
            placeholder="Choose nickname mode",
        )
        layout.add_text_select(
            self._build_state_action(OnlineActionKind.SET_PENDING_NICK_PLATFORM, state),
            options=[
                EditorSelectOption(
                    label=_component_text(platform),
                    value=platform,
                    description="Use this platform",
                    is_default=platform == pending.platform,
                )
                for platform in self._nickname_platform_options(pending.mode)
            ],
            placeholder="Choose nickname platform",
        )
        layout.next_row()
        layout.add_button(
            self._build_state_action(OnlineActionKind.SAVE_PENDING_NICKNAME, state),
            "Save Rule",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=pending.mode is None or pending.platform is None,
        )
        layout.page_footer(
            self._action_codec.build(OnlineActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
            back_action=self._build_state_action(OnlineActionKind.CANCEL_PENDING_NICKNAME, state),
            extra_buttons=(EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),),
        )

    def _render_account_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        watcher_id: hikari.Snowflake,
        names_cache: Name_Cache,
        state: OnlineEditorState,
    ) -> None:
        embed.add_field(name="Platform IDs", value=_display_value(_platform_lines(names_cache, watcher_id)), inline=False)
        steam_id = names_cache.get_platform_id(int(watcher_id), "steam")
        if steam_id:
            embed.add_field(name="Steam", value=f"`{steam_id}`", inline=False)
        layout.add_buttons(
            EditorButton(
                self._build_state_action(OnlineActionKind.START_SET_STEAM, state),
                "Set Steam ID",
                style=hikari.ButtonStyle.PRIMARY,
            ),
            EditorButton(
                self._build_state_action(OnlineActionKind.CLEAR_STEAM, state),
                "Clear Steam ID",
                style=hikari.ButtonStyle.DANGER,
                is_disabled=steam_id is None,
            ),
        )
        layout.page_footer(
            self._action_codec.build(OnlineActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1),
            extra_buttons=(EditorButton(self._build_state_action(OnlineActionKind.REFRESH, state), "Refresh"),),
        )

    def _add_section_buttons(self, *, layout: EditorLayout, state: OnlineEditorState) -> None:
        layout.add_buttons(
            EditorButton(
                self._action_codec.build(OnlineActionKind.SHOW_OVERVIEW, page=0),
                "Overview",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if state.section is OnlineEditorSection.OVERVIEW
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=state.section is OnlineEditorSection.OVERVIEW,
            ),
            EditorButton(
                self._action_codec.build(OnlineActionKind.SHOW_WATCHES, page=0),
                "Watches",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if state.section is OnlineEditorSection.WATCHES
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=state.section is OnlineEditorSection.WATCHES and state.selected_target_id is None,
            ),
            EditorButton(
                self._action_codec.build(OnlineActionKind.SHOW_DRINK, page=0),
                "Drink",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if state.section is OnlineEditorSection.DRINK
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=state.section is OnlineEditorSection.DRINK,
            ),
            EditorButton(
                self._action_codec.build(OnlineActionKind.SHOW_NICKNAMES, page=0),
                "Nicknames",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if state.section is OnlineEditorSection.NICKNAMES
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=state.section is OnlineEditorSection.NICKNAMES,
            ),
            EditorButton(
                self._action_codec.build(OnlineActionKind.SHOW_ACCOUNT, page=0),
                "Accounts",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if state.section is OnlineEditorSection.ACCOUNT
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=state.section is OnlineEditorSection.ACCOUNT,
            ),
        )

    def _watch_entries(
        self,
        *,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        watcher_id: hikari.Snowflake,
    ) -> list[WatchListEntry]:
        return [
            WatchListEntry(
                user_id=target_id,
                label=_user_label(names_cache, target_id),
                description=_watch_counts(rule),
            )
            for target_id, rule in sorted(tracker.list_rules(watcher_id).items(), key=lambda item: int(item[0]))
        ]

    @staticmethod
    def _watch_entry_for_target(entries: Sequence[WatchListEntry], target_id: hikari.Snowflake) -> WatchListEntry:
        for entry in entries:
            if entry.user_id == target_id:
                return entry
        raise ValueError(f"Target {target_id} is not watched")

    @staticmethod
    def _nickname_platform_options(mode: str | None) -> tuple[str, ...]:
        if mode == "offline":
            return ("all",)
        return NICKNAME_PLATFORMS

    def _initial_state(
        self,
        *,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        watcher_id: hikari.Snowflake,
        focus_target_id: hikari.Snowflake | None,
    ) -> OnlineEditorState:
        if focus_target_id is None:
            return OnlineEditorState(section=OnlineEditorSection.OVERVIEW, page=0)
        entries = self._watch_entries(tracker=tracker, names_cache=names_cache, watcher_id=watcher_id)
        for entry in entries:
            if entry.user_id == focus_target_id:
                return OnlineEditorState(
                    section=OnlineEditorSection.WATCHES,
                    page=_page_for_value(entries, entry),
                    selected_target_id=focus_target_id,
                )
        return OnlineEditorState(section=OnlineEditorSection.WATCHES, page=0, selected_target_id=focus_target_id)

    async def _send_config_export_dm(
        self,
        *,
        bot: hikari.GatewayBot,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
        watcher_id: hikari.Snowflake,
        export_target_id: hikari.Snowflake | None,
    ) -> bool:
        payload = tracker.export_user_config(watcher_id, target_id=export_target_id)
        if export_target_id is None:
            filename = "online_config.json"
            content = "Online config export. Import with `/online file:<attachment>`."
        else:
            filename = f"online_config_{int(export_target_id)}.json"
            content = f"Online config export for {_user_label(names_cache, export_target_id)}."
        try:
            dm = await bot.rest.create_dm_channel(watcher_id)
            await bot.rest.create_message(
                dm.id,
                content=content,
                attachment=hikari.Bytes(
                    json.dumps(payload, indent=4, sort_keys=False).encode(config.STR_ENCODE),
                    filename,
                ),
            )
        except hikari.ForbiddenError:
            return False
        return True

    def _toggle_watch_filter(
        self,
        *,
        tracker: Online_Tracker,
        watcher_id: hikari.Snowflake,
        target_id: hikari.Snowflake,
        selector: str,
    ) -> str:
        kind, _, raw_value = selector.partition(":")
        value = raw_value.strip()
        if kind == "type":
            rule = tracker.get_rule(watcher_id, target_id)
            enabled = bool(rule and value in rule.types)
            if enabled:
                tracker.remove_type(watcher_id, target_id, value)
                return f"Disabled type filter `{value}`."
            tracker.add_type(watcher_id, target_id, value)
            return f"Enabled type filter `{value}`."
        if kind == "activity":
            rule = tracker.get_rule(watcher_id, target_id)
            enabled = bool(rule and value in rule.activities)
            if enabled:
                tracker.remove_activity(watcher_id, target_id, value)
                return f"Disabled activity filter `{value}`."
            tracker.add_activity(watcher_id, target_id, value)
            return f"Enabled activity filter `{value}`."
        raise ValueError(f"Unknown watch filter selector: {selector}")

    def _build_state_action(self, kind: OnlineActionKind, state: OnlineEditorState) -> str:
        return self._action_codec.build(kind, page=state.page, value=self._pack_state(state))

    def _build_state_action_with_value(self, kind: OnlineActionKind, state: OnlineEditorState, extra: str) -> str:
        return self._action_codec.build(kind, page=state.page, value=f"{self._pack_state(state)},{extra}")

    @staticmethod
    def _pack_state(state: OnlineEditorState) -> str:
        target_id = int(state.selected_target_id) if state.selected_target_id is not None else 0
        return f"{state.section.value},{target_id},{state.view.value}"

    def _state_and_extra_from_action(self, action: object) -> tuple[OnlineEditorState | None, str | None]:
        page = getattr(action, "page", None)
        raw_value = getattr(action, "value", None)
        if not isinstance(page, int):
            return None, None
        if raw_value is None:
            return None, None
        if not isinstance(raw_value, str):
            return None, None
        parts = raw_value.split(",", 3)
        if len(parts) < 3 or not parts[1].isdigit():
            return None, None
        try:
            section = OnlineEditorSection(parts[0])
            view = OnlineEditorView(parts[2])
        except ValueError:
            return None, None
        target_id = hikari.Snowflake(parts[1]) if parts[1] != "0" else None
        extra = parts[3] if len(parts) > 3 else None
        return OnlineEditorState(section=section, page=page, selected_target_id=target_id, view=view), extra

    @staticmethod
    def _require_acl(deps: Mapping[str, object]) -> Access_Control:
        value = deps.get("acl")
        if not isinstance(value, Access_Control):
            raise TypeError("Online editor requires Access_Control")
        return value

    @staticmethod
    def _require_tracker(deps: Mapping[str, object]) -> Online_Tracker:
        value = deps.get("tracker")
        if not isinstance(value, Online_Tracker):
            raise TypeError("Online editor requires Online_Tracker")
        return value

    @staticmethod
    def _require_names_cache(deps: Mapping[str, object]) -> Name_Cache:
        value = deps.get("names_cache")
        if not isinstance(value, Name_Cache):
            raise TypeError("Online editor requires Name_Cache")
        return value

    @staticmethod
    def _require_bot(deps: Mapping[str, object]) -> hikari.GatewayBot:
        value = deps.get("bot")
        if not isinstance(value, hikari.GatewayBot):
            raise TypeError("Online editor requires GatewayBot")
        return value


class CMD_Online(
    lightbulb.SlashCommand,
    name="online",
    description="Open the online tracking editor",
):
    file = lightbulb.attachment(
        "file",
        "Optional JSON config exported by this command; uploads replace your config",
        default=None,
    )
    public = lightbulb.boolean("public", "Send the editor as a normal message", default=False)  # type: ignore[reportAssignmentType]
    user = lightbulb.user("user", "Optional watched user to focus", default=None)  # type: ignore[reportAssignmentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        bot: hikari.GatewayBot,
        online_editor: OnlineEditorService,
        tracker: Online_Tracker,
        names_cache: Name_Cache,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        focus_target_id = _extract_user_id(self.user) if self.user else None
        if self.user and focus_target_id is None:
            raise ValueError("Invalid target user")
        if focus_target_id is not None and tracker.is_ignored_user(focus_target_id):
            raise ValueError("That user is ignored")

        if self.file is not None:
            if focus_target_id is not None:
                raise ValueError("`user` can't be used with `file` import")
            await ctx.defer(ephemeral=not self.public and ctx.guild_id is not None)
            path = await File_Utils.download_temp(self.file)
            try:
                payload = json.loads(path.read_text(config.STR_ENCODE))
            except json.JSONDecodeError as xcp:
                raise ValueError(f"Invalid JSON file: {xcp}") from xcp
            if not isinstance(payload, dict):
                raise ValueError("Invalid JSON file: top-level object expected")
            result = tracker.apply_user_config(ctx.user.id, payload)
            await tracker.refresh_nickname(ctx.user.id, bot, force_clear=not tracker.nick_rules.get(ctx.user.id))
            await ctx.respond(
                "Online config updated from file\n"
                f"- watches: {result['watches']}\n"
                f"- drink games: {result['drink_games']}\n"
                f"- nicknames: {result['nicknames']}\n"
                f"- skipped ignored users: {result['skipped_ignored_users']}",
            )
            return

        await online_editor.open_editor(
            ctx=ctx,
            tracker=tracker,
            names_cache=names_cache,
            focus_target_id=focus_target_id,
            is_public=self.public,
        )


# AiviA APasz
