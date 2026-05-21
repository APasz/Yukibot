import enum
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

from _manager import App_Manager
from _security import Access_Control
from config import Name_Cache, UserNames

log = logging.getLogger(__name__)

_ALIAS_EDITOR_PREFIX = "alias-editor:"
_ALIAS_MODAL_PREFIX = "alias-modal:"
_ALIAS_MODAL_FIELD_ID = "alias"
_ALIAS_PAGE_SIZE = 25

ValueT = TypeVar("ValueT")


class AliasActionKind(enum.StrEnum):
    ADD_GENERAL = enum.auto()
    CLOSE = enum.auto()
    PAGE = enum.auto()
    REFRESH = enum.auto()
    REMOVE_APP = enum.auto()
    REMOVE_GENERAL = enum.auto()
    SET_APP = enum.auto()
    SHOW_APP_ALIASES = enum.auto()
    SHOW_APP_SCOPES = enum.auto()
    SHOW_GENERAL = enum.auto()


class AliasEditorSection(enum.StrEnum):
    GENERAL = enum.auto()
    APP_ALIASES = enum.auto()
    APP_SCOPES = enum.auto()


@dataclass(frozen=True, slots=True)
class AliasEditorState:
    section: AliasEditorSection
    page: int


@dataclass(frozen=True, slots=True)
class PagedItems(Generic[ValueT]):
    visible: tuple[ValueT, ...]
    total_count: int
    page_state: EditorPageState


@dataclass(frozen=True, slots=True)
class AppAliasEntry:
    scope: str
    alias: str


@dataclass(frozen=True, slots=True)
class AliasEditorView:
    account_name: str | None
    section: AliasEditorSection
    known_names: tuple[str, ...]
    general_aliases: PagedItems[str]
    app_aliases: PagedItems[AppAliasEntry]
    app_scopes: PagedItems[str]

    @property
    def current_page_state(self) -> EditorPageState:
        if self.section is AliasEditorSection.GENERAL:
            return self.general_aliases.page_state
        if self.section is AliasEditorSection.APP_ALIASES:
            return self.app_aliases.page_state
        return self.app_scopes.page_state


async def ac_all_ids(ctx: lightbulb.AutocompleteContext, names_cache: Name_Cache) -> None:
    accounts = sorted({entry.account for entry in names_cache.by_id.values() if entry.account})
    await ctx.respond(accounts)


def _all_app_scopes(manager: App_Manager) -> tuple[str, ...]:
    return tuple(sorted({app.scope.lower() for app in manager.apps.values()}))


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
    return max(1, (count + _ALIAS_PAGE_SIZE - 1) // _ALIAS_PAGE_SIZE)


def _clamp_page(page: int, total_pages: int) -> int:
    if page < 0:
        return 0
    if page >= total_pages:
        return total_pages - 1
    return page


def _page_slice_str(values: Sequence[str], page: int) -> Sequence[str]:
    start = page * _ALIAS_PAGE_SIZE
    end = start + _ALIAS_PAGE_SIZE
    return values[start:end]


def _page_slice_app_alias(values: Sequence[AppAliasEntry], page: int) -> Sequence[AppAliasEntry]:
    start = page * _ALIAS_PAGE_SIZE
    end = start + _ALIAS_PAGE_SIZE
    return values[start:end]


def _paginate_str(values: Sequence[str], page: int) -> PagedItems[str]:
    total_pages = _page_count(len(values))
    current_page = _clamp_page(page, total_pages)
    return PagedItems(
        visible=tuple(_page_slice_str(values, current_page)),
        total_count=len(values),
        page_state=EditorPageState(page=current_page, total_pages=total_pages),
    )


def _paginate_app_alias(values: Sequence[AppAliasEntry], page: int) -> PagedItems[AppAliasEntry]:
    total_pages = _page_count(len(values))
    current_page = _clamp_page(page, total_pages)
    return PagedItems(
        visible=tuple(_page_slice_app_alias(values, current_page)),
        total_count=len(values),
        page_state=EditorPageState(page=current_page, total_pages=total_pages),
    )


def _page_for_sorted_value(values: Sequence[str], needle: str) -> int:
    try:
        index = values.index(needle)
    except ValueError:
        return 0
    return index // _ALIAS_PAGE_SIZE


def _editor_flags(is_public: bool) -> hikari.MessageFlag | hikari.UndefinedType:
    if is_public:
        return hikari.UNDEFINED
    return hikari.MessageFlag.EPHEMERAL


def _section_label(section: AliasEditorSection) -> str:
    if section is AliasEditorSection.GENERAL:
        return "General Aliases"
    if section is AliasEditorSection.APP_ALIASES:
        return "App Aliases"
    return "Editable App Scopes"


def _app_scope_options(manager: App_Manager) -> tuple[str, ...]:
    return _all_app_scopes(manager)


def _require_known_app_scope(manager: App_Manager, scope: str) -> str:
    normalised_scope = scope.strip().lower()
    if not normalised_scope:
        raise ValueError("App scope must not be empty.")
    if normalised_scope not in _app_scope_options(manager):
        raise KeyError(f"Unknown app scope `{normalised_scope}`.")
    return normalised_scope


async def _resolve_alias_target_user_id(
    *,
    actor_user_id: hikari.Snowflakeish,
    requested_user: str | None,
    acl: Access_Control,
    names_cache: Name_Cache,
) -> int:
    user_id = int(actor_user_id)
    if requested_user is None:
        return user_id

    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    resolved_user_id = names_cache.resolve_to_id(requested_user, prefer_global_name=True)
    if not resolved_user_id:
        raise KeyError("User Not Found")
    return resolved_user_id


def _user_names_for_editor(names_cache: Name_Cache, user_id: int) -> UserNames:
    return names_cache.by_id.setdefault(user_id, UserNames())


class AliasEditorService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(AliasActionKind)
        self._editor = Editor(
            prefix=_ALIAS_EDITOR_PREFIX,
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
        )
        self._alias_modal = ModalKit(
            prefix=_ALIAS_MODAL_PREFIX,
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_ALIAS_MODAL_FIELD_ID,
                        label="Alias",
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
        names_cache: Name_Cache,
        manager: App_Manager,
        target_user_id: int,
        is_public: bool = False,
        status: str = "Manage aliases below.",
    ) -> None:
        locale = self._editor.resolve_locale(ctx.interaction)
        embed, components = self._render_editor(
            target_user_id=target_user_id,
            actor_user_id=int(ctx.user.id),
            locale=locale,
            names_cache=names_cache,
            manager=manager,
            state=AliasEditorState(section=AliasEditorSection.GENERAL, page=0),
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
        names_cache: Name_Cache,
        manager: App_Manager,
    ) -> bool:
        return await self._editor.route(
            interaction,
            acl=acl,
            names_cache=names_cache,
            manager=manager,
        )

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        acl: Access_Control,
        names_cache: Name_Cache,
        manager: App_Manager,
    ) -> bool:
        return await self._alias_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this alias editor.",
            invalid_message="Alias must not be empty.",
            acl=acl,
            names_cache=names_cache,
            manager=manager,
        )

    async def _authorise_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, req.scope_id, deps)

    async def _authorise_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, req.scope_id, deps)

    async def _authorise_request_user(
        self,
        actor_user_id: hikari.Snowflakeish,
        target_user_id: hikari.Snowflakeish,
        deps: Mapping[str, object],
    ) -> bool:
        acl = self._require_acl(deps)
        actor_id = int(actor_user_id)
        target_id = int(target_user_id)
        try:
            await acl.perm_check(actor_id, acl.LvL.guest)
            if target_id != actor_id:
                await acl.perm_check(actor_id, acl.LvL.sudo)
        except Exception:
            return False
        return True

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        names_cache = self._require_names_cache(deps)
        manager = self._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown alias editor action.")

        target_user_id = int(req.scope_id)
        actor_user_id = int(req.user_id)

        state = self._state_from_action(action)
        if state is None and action.kind in {
            AliasActionKind.PAGE,
            AliasActionKind.REFRESH,
        }:
            return EditorResponse.ephemeral("Alias editor state is invalid.")

        if action.kind is AliasActionKind.CLOSE:
            return EditorResponse.close("Alias editor closed.")

        if action.kind in {AliasActionKind.PAGE, AliasActionKind.REFRESH}:
            if state is None:
                return EditorResponse.ephemeral("Alias editor state is invalid.")
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=state.section,
                status="Alias editor refreshed." if action.kind is AliasActionKind.REFRESH else "Page updated.",
                page=state.page,
            )

        if action.kind is AliasActionKind.ADD_GENERAL:
            await req.interaction.create_modal_response(
                "Add General Alias",
                self._alias_modal.build_id(
                    self._action_codec.build(AliasActionKind.ADD_GENERAL, page=action.page),
                    scope_id=target_user_id,
                    user_id=actor_user_id,
                ),
                components=self._alias_modal.rows(),
            )
            return None

        if action.kind is AliasActionKind.SHOW_GENERAL:
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.GENERAL,
                status="Showing general aliases.",
                page=0,
            )

        if action.kind is AliasActionKind.SHOW_APP_ALIASES:
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.APP_ALIASES,
                status="Showing app aliases.",
                page=0,
            )

        if action.kind is AliasActionKind.SHOW_APP_SCOPES:
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.APP_SCOPES,
                status="Showing editable app scopes.",
                page=0,
            )

        if action.kind is AliasActionKind.SET_APP:
            if not req.values:
                return EditorResponse.ephemeral("Choose an app to edit first.")
            try:
                scope = _require_known_app_scope(manager, req.values[0])
            except (KeyError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            names = _user_names_for_editor(names_cache, target_user_id)
            current_alias = names.games.get(scope, ("", None))[0]
            await req.interaction.create_modal_response(
                f"Set {scope.title()} Alias",
                self._alias_modal.build_id(
                    self._action_codec.build(AliasActionKind.SET_APP, page=action.page, value=scope),
                    scope_id=target_user_id,
                    user_id=actor_user_id,
                ),
                components=self._alias_modal.rows({_ALIAS_MODAL_FIELD_ID: current_alias}),
            )
            return None

        if action.kind is AliasActionKind.REMOVE_GENERAL:
            if not req.values:
                return EditorResponse.ephemeral("Choose a general alias to remove.")
            alias = req.values[0]
            names_cache.remove_name(target_user_id, alias)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.GENERAL,
                status=f"Removed general alias `{alias}`.",
                page=action.page,
            )

        if action.kind is AliasActionKind.REMOVE_APP:
            if not req.values:
                return EditorResponse.ephemeral("Choose an app alias to remove.")
            try:
                scope = _require_known_app_scope(manager, req.values[0])
            except (KeyError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            names_cache.remove_game_alias(target_user_id, scope)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.APP_ALIASES,
                status=f"Removed `{scope.title()}` alias.",
                page=action.page,
            )

        return EditorResponse.ephemeral("Unsupported alias editor action.")

    def _build_editor_response(
        self,
        *,
        target_user_id: int,
        actor_user_id: int,
        locale: hikari.Locale,
        names_cache: Name_Cache,
        manager: App_Manager,
        section: AliasEditorSection,
        status: str,
        page: int,
    ) -> EditorResponse:
        embed, components = self._render_editor(
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            locale=locale,
            names_cache=names_cache,
            manager=manager,
            state=AliasEditorState(section=section, page=page),
        )
        return EditorResponse.update(status, components=components, embeds=[embed])

    def _render_editor(
        self,
        *,
        target_user_id: int,
        actor_user_id: int,
        locale: hikari.Locale,
        names_cache: Name_Cache,
        manager: App_Manager,
        state: AliasEditorState,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        view = self._build_view(
            names_cache=names_cache,
            manager=manager,
            target_user_id=target_user_id,
            state=state,
        )
        names = _user_names_for_editor(names_cache, target_user_id)
        title_name = "Your" if target_user_id == actor_user_id else (view.account_name or f"User {target_user_id}")
        section_label = _section_label(view.section)

        embed = hikari.Embed(
            title=f"{title_name} Aliases",
            description=(
                "Known names are read-only.\n"
                f"Section: {section_label}\n"
                f"General aliases: {view.general_aliases.total_count} | "
                f"App aliases: {view.app_aliases.total_count} | "
                f"App scopes: {view.app_scopes.total_count}"
            ),
            color=0xB00F0F,
        )
        embed.add_field(name="Known Names", value=_display_value(view.known_names), inline=False)
        if view.section is AliasEditorSection.GENERAL:
            embed.add_field(
                name=f"General Aliases ({view.general_aliases.total_count})",
                value=_display_value(view.general_aliases.visible),
                inline=False,
            )
        elif view.section is AliasEditorSection.APP_ALIASES:
            embed.add_field(
                name=f"App Aliases ({view.app_aliases.total_count})",
                value=_display_value([f"{entry.scope.title()}: {entry.alias}" for entry in view.app_aliases.visible]),
                inline=False,
            )
        else:
            embed.add_field(
                name=f"Editable App Scopes ({view.app_scopes.total_count})",
                value=_display_value([scope.title() for scope in view.app_scopes.visible]),
                inline=False,
            )
        if names.account and target_user_id != actor_user_id:
            embed.set_footer(text=f"Editing {names.account}")

        editor_ctx = self._editor.context(
            scope_id=target_user_id,
            user_id=actor_user_id,
            locale=locale,
        )
        layout = EditorLayout(editor_ctx)
        layout.add_buttons(
            EditorButton(
                self._action_codec.build(AliasActionKind.SHOW_GENERAL, page=0),
                "General",
                style=hikari.ButtonStyle.PRIMARY
                if view.section is AliasEditorSection.GENERAL
                else hikari.ButtonStyle.SECONDARY,
                is_disabled=view.section is AliasEditorSection.GENERAL,
            ),
            EditorButton(
                self._action_codec.build(AliasActionKind.SHOW_APP_ALIASES, page=0),
                "App Aliases",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if view.section is AliasEditorSection.APP_ALIASES
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=view.section is AliasEditorSection.APP_ALIASES,
            ),
            EditorButton(
                self._action_codec.build(AliasActionKind.SHOW_APP_SCOPES, page=0),
                "App Scopes",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if view.section is AliasEditorSection.APP_SCOPES
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=view.section is AliasEditorSection.APP_SCOPES,
            ),
        )
        layout.next_row()

        if view.section is AliasEditorSection.GENERAL:
            layout.add_button(
                self._action_codec.build(AliasActionKind.ADD_GENERAL, page=view.general_aliases.page_state.page),
                "Add General Alias",
                style=hikari.ButtonStyle.PRIMARY,
            )
        if view.section is AliasEditorSection.APP_SCOPES and view.app_scopes.visible:
            layout.add_text_select(
                self._action_codec.build(AliasActionKind.SET_APP, page=view.app_scopes.page_state.page),
                options=[
                    EditorSelectOption(
                        label=_component_text(scope.title()),
                        value=scope,
                        description=_component_text(names.games.get(scope, ("Set or update alias", None))[0]),
                    )
                    for scope in view.app_scopes.visible
                ],
                placeholder="Set app-specific alias",
            )
        if view.section is AliasEditorSection.GENERAL and view.general_aliases.visible:
            layout.add_text_select(
                self._action_codec.build(AliasActionKind.REMOVE_GENERAL, page=view.general_aliases.page_state.page),
                options=[
                    EditorSelectOption(
                        label=_component_text(alias),
                        value=alias,
                        description="Remove this general alias",
                    )
                    for alias in view.general_aliases.visible
                ],
                placeholder="Remove general alias",
            )
        if view.section is AliasEditorSection.APP_ALIASES and view.app_aliases.visible:
            layout.add_text_select(
                self._action_codec.build(AliasActionKind.REMOVE_APP, page=view.app_aliases.page_state.page),
                options=[
                    EditorSelectOption(
                        label=_component_text(entry.scope.title()),
                        value=entry.scope,
                        description=_component_text(entry.alias),
                    )
                    for entry in view.app_aliases.visible
                ],
                placeholder="Remove app alias",
            )

        prev_action = None
        next_action = None
        page_state = view.current_page_state
        if page_state.total_pages > 1:
            prev_action = self._build_section_state_action(
                AliasActionKind.PAGE,
                AliasEditorState(section=view.section, page=max(0, page_state.page - 1)),
            )
            next_action = self._build_section_state_action(
                AliasActionKind.PAGE,
                AliasEditorState(section=view.section, page=min(page_state.total_pages - 1, page_state.page + 1)),
            )
        layout.page_footer(
            self._action_codec.build(AliasActionKind.CLOSE, page=page_state.page),
            page_state=page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._build_section_state_action(
                        AliasActionKind.REFRESH,
                        AliasEditorState(section=view.section, page=page_state.page),
                    ),
                    "Refresh",
                ),
            ),
        )
        return embed, layout.build()

    async def _on_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        names_cache = self._require_names_cache(deps)
        manager = self._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown alias modal action.")

        actor_user_id = int(req.user_id)
        target_user_id = int(req.scope_id)
        alias = req.values.get(_ALIAS_MODAL_FIELD_ID, "").strip()
        if not alias:
            return EditorResponse.ephemeral("Alias must not be empty.")

        page = action.page
        if action.kind is AliasActionKind.ADD_GENERAL:
            names_cache.add_name(target_user_id, alias, False)
            nicknames = tuple(sorted(_user_names_for_editor(names_cache, target_user_id).nicknames))
            page = _page_for_sorted_value(nicknames, alias)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=self._editor.resolve_locale(req.interaction),
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.GENERAL,
                status=f"Added general alias `{alias}`.",
                page=page,
            )

        if action.kind is AliasActionKind.SET_APP and action.value is not None:
            try:
                scope = _require_known_app_scope(manager, action.value)
            except (KeyError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            valid_scopes = _app_scope_options(manager)
            names_cache.set_game_alias(target_user_id, scope, alias)
            page = _page_for_sorted_value(valid_scopes, scope)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=self._editor.resolve_locale(req.interaction),
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.APP_SCOPES,
                status=f"Set `{scope.title()}` alias to `{alias}`.",
                page=page,
            )

        return EditorResponse.ephemeral("Unknown alias modal action.")

    def _build_view(
        self,
        *,
        names_cache: Name_Cache,
        manager: App_Manager,
        target_user_id: int,
        state: AliasEditorState,
    ) -> AliasEditorView:
        names = _user_names_for_editor(names_cache, target_user_id)
        all_scopes = _app_scope_options(manager)
        nicknames = tuple(sorted(names.nicknames))
        app_aliases = tuple(
            AppAliasEntry(scope=scope, alias=data[0]) for scope, data in sorted(names.games.items()) if data[0]
        )
        return AliasEditorView(
            account_name=names.account,
            section=state.section,
            known_names=tuple(sorted(names.names)),
            general_aliases=_paginate_str(nicknames, state.page if state.section is AliasEditorSection.GENERAL else 0),
            app_aliases=_paginate_app_alias(
                app_aliases,
                state.page if state.section is AliasEditorSection.APP_ALIASES else 0,
            ),
            app_scopes=_paginate_str(all_scopes, state.page if state.section is AliasEditorSection.APP_SCOPES else 0),
        )

    def _build_section_state_action(self, kind: AliasActionKind, state: AliasEditorState) -> str:
        return self._action_codec.build(kind, page=state.page, value=state.section.value)

    def _state_from_action(self, action: object) -> AliasEditorState | None:
        page = getattr(action, "page", None)
        raw_section = getattr(action, "value", None)
        if not isinstance(page, int) or not isinstance(raw_section, str):
            return None
        try:
            return AliasEditorState(section=AliasEditorSection(raw_section), page=page)
        except ValueError:
            return None

    @staticmethod
    def _require_acl(deps: Mapping[str, object]) -> Access_Control:
        value = deps.get("acl")
        if not isinstance(value, Access_Control):
            raise TypeError("Alias editor requires Access_Control")
        return value

    @staticmethod
    def _require_names_cache(deps: Mapping[str, object]) -> Name_Cache:
        value = deps.get("names_cache")
        if not isinstance(value, Name_Cache):
            raise TypeError("Alias editor requires Name_Cache")
        return value

    @staticmethod
    def _require_manager(deps: Mapping[str, object]) -> App_Manager:
        value = deps.get("manager")
        if not isinstance(value, App_Manager):
            raise TypeError("Alias editor requires App_Manager")
        return value


class CMD_Alias(
    lightbulb.SlashCommand,
    name="alias",
    description="Open the alias editor",
):
    publc = lightbulb.boolean("publc", "Send the editor as a normal message", default=False)  # type: ignore[reportAssignmentType]
    user = lightbulb.string("user", "Other user", autocomplete=ac_all_ids, default=None)  # type: ignore[reportAssignmentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        alias_editor: AliasEditorService,
        manager: App_Manager,
        names_cache: Name_Cache,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.guest)
        target_user_id = await _resolve_alias_target_user_id(
            actor_user_id=ctx.user.id,
            requested_user=self.user,
            acl=acl,
            names_cache=names_cache,
        )
        log.info(f"Alias.Open: {ctx.user.display_name} > {self.user}")
        await alias_editor.open_editor(
            ctx=ctx,
            names_cache=names_cache,
            manager=manager,
            target_user_id=target_user_id,
            is_public=self.publc,
        )


# AiviA APasz
