import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar
from urllib.parse import urlparse

import aiohttp
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
from _manager import App_Manager
from _security import Access_Control
from config import Name_Cache, UserNames

log = logging.getLogger(__name__)

_ALIAS_EDITOR_PREFIX = "alias-editor:"
_ALIAS_MODAL_PREFIX = "alias-modal:"
_DISPLAY_OVERRIDE_MODAL_PREFIX = "display-override-modal:"
_STEAM_MODAL_PREFIX = "steam-modal:"
_MINECRAFT_PROFILE_MODAL_PREFIX = "mc-prof:"
_ALIAS_MODAL_FIELD_ID = "alias"
_DISPLAY_OVERRIDE_MODAL_FIELD_ID = "display_name"
_STEAM_MODAL_FIELD_ID = "steam_id"
_MINECRAFT_PROFILE_NAME_FIELD_ID = "profile_name"
_MINECRAFT_PROFILE_UUID_FIELD_ID = "profile_uuid"
_ALIAS_PAGE_SIZE = 25
_MINECRAFT_SCOPE = "minecraft"

ValueT = TypeVar("ValueT")


class AliasActionKind(enum.StrEnum):
    ADD_GENERAL = enum.auto()
    CHOOSE_MINECRAFT_LOOKUP = enum.auto()
    CLEAR_DISPLAY_OVERRIDE = enum.auto()
    CLEAR_MINECRAFT_PROFILE = enum.auto()
    CLEAR_STEAM = enum.auto()
    CLOSE = enum.auto()
    PAGE = enum.auto()
    REFRESH = enum.auto()
    REMOVE_APP = enum.auto()
    REMOVE_GENERAL = enum.auto()
    SET_DISPLAY_OVERRIDE = enum.auto()
    SET_APP = enum.auto()
    SET_MINECRAFT_PROFILE = enum.auto()
    SET_STEAM = enum.auto()
    SHOW_ALIASES = enum.auto()
    SHOW_DISPLAY_OVERRIDES = enum.auto()
    SHOW_APP_SCOPES = enum.auto()
    SHOW_LINKED_ACCOUNTS = enum.auto()
    SHOW_OVERVIEW = enum.auto()


class AliasEditorSection(enum.StrEnum):
    OVERVIEW = enum.auto()
    DISPLAY_OVERRIDES = enum.auto()
    ALIASES = enum.auto()
    APP_SCOPES = enum.auto()
    LINKED_ACCOUNTS = enum.auto()


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
class DisplayOverrideEntry:
    category: config.DisplayNameCategory
    value: str | None


@dataclass(frozen=True, slots=True)
class LinkedAccountEntry:
    platform: str
    value: str


@dataclass(frozen=True, slots=True)
class MinecraftProfileEntry:
    alias: str | None
    uuid: str | None


@dataclass(frozen=True, slots=True)
class MinecraftLookupCandidate:
    name: str
    uuid: str


@dataclass(frozen=True, slots=True)
class AliasEditorView:
    account_name: str | None
    section: AliasEditorSection
    known_names: tuple[str, ...]
    display_overrides: PagedItems[DisplayOverrideEntry]
    general_aliases: PagedItems[str]
    app_aliases: PagedItems[AppAliasEntry]
    app_scopes: PagedItems[str]
    linked_accounts: PagedItems[LinkedAccountEntry]
    minecraft_profile: MinecraftProfileEntry

    @property
    def current_page_state(self) -> EditorPageState:
        if self.section is AliasEditorSection.OVERVIEW:
            return EditorPageState(page=0, total_pages=1)
        if self.section is AliasEditorSection.DISPLAY_OVERRIDES:
            return self.display_overrides.page_state
        if self.section is AliasEditorSection.ALIASES:
            total_pages = max(self.general_aliases.page_state.total_pages, self.app_aliases.page_state.total_pages)
            return EditorPageState(page=min(self.general_aliases.page_state.page, total_pages - 1), total_pages=total_pages)
        if self.section is AliasEditorSection.LINKED_ACCOUNTS:
            return self.linked_accounts.page_state
        return self.app_scopes.page_state


async def ac_all_ids(ctx: lightbulb.AutocompleteContext[str], names_cache: Name_Cache) -> None:
    accounts = sorted({entry.account for entry in names_cache.by_id.values() if entry.account})
    await ctx.respond(accounts)


def _all_app_scopes(manager: App_Manager) -> tuple[str, ...]:
    return tuple(sorted({app.scope.lower() for app in manager.apps.values()}))


def _editable_app_scopes(manager: App_Manager) -> tuple[str, ...]:
    return tuple(scope for scope in _all_app_scopes(manager) if scope != _MINECRAFT_SCOPE)


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


def _page_slice(values: Sequence[ValueT], page: int) -> Sequence[ValueT]:
    start = page * _ALIAS_PAGE_SIZE
    end = start + _ALIAS_PAGE_SIZE
    return values[start:end]


def _paginate(values: Sequence[ValueT], page: int) -> PagedItems[ValueT]:
    total_pages = _page_count(len(values))
    current_page = _clamp_page(page, total_pages)
    return PagedItems(
        visible=tuple(_page_slice(values, current_page)),
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
    if section is AliasEditorSection.OVERVIEW:
        return "Overview"
    if section is AliasEditorSection.DISPLAY_OVERRIDES:
        return "Display Names"
    if section is AliasEditorSection.ALIASES:
        return "Names & Aliases"
    if section is AliasEditorSection.LINKED_ACCOUNTS:
        return "Linked Accounts"
    return "Advanced Game Aliases"


def _app_scope_options(manager: App_Manager) -> tuple[str, ...]:
    return _editable_app_scopes(manager)


def _platform_label(platform: str) -> str:
    if platform == "steam":
        return "Steam"
    return platform.title()


def _display_override_label(category: config.DisplayNameCategory) -> str:
    if category is config.DisplayNameCategory.DISCORD:
        return "Discord"
    if category is config.DisplayNameCategory.WEB:
        return "Web App"
    raise ValueError(f"Unsupported display override category `{category}`.")


def _require_known_app_scope(manager: App_Manager, scope: str) -> str:
    normalised_scope = scope.strip().lower()
    if not normalised_scope:
        raise ValueError("App scope must not be empty.")
    if normalised_scope not in _app_scope_options(manager):
        raise KeyError(f"Unknown app scope `{normalised_scope}`.")
    return normalised_scope


def _require_display_override_category(category: object) -> config.DisplayNameCategory:
    return Name_Cache._normalised_display_name_category(category)


def _display_override_entries(names: UserNames) -> tuple[DisplayOverrideEntry, ...]:
    return tuple(
        DisplayOverrideEntry(category=category, value=names.display_overrides.get_for_category(category))
        for category in config.DisplayNameCategory
    )


def _linked_account_entries(names_cache: Name_Cache, user_id: int) -> tuple[LinkedAccountEntry, ...]:
    return tuple(
        LinkedAccountEntry(platform=platform, value=value)
        for platform, value in names_cache.list_platform_ids(user_id).items()
    )


def _minecraft_profile_entry(names: UserNames) -> MinecraftProfileEntry:
    alias, uuid = names.games.get(_MINECRAFT_SCOPE, (None, None))
    return MinecraftProfileEntry(alias=alias, uuid=uuid)


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def _parse_steamcommunity_identity(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    if host not in {"steamcommunity.com", "www.steamcommunity.com"}:
        raise ValueError("Steam profile URL must use steamcommunity.com.")
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        raise ValueError("Steam profile URL must include `/profiles/<steamid>` or `/id/<vanity>`.")
    scope, identifier = path_parts[0].casefold(), path_parts[1].strip()
    if not identifier:
        raise ValueError("Steam profile URL identifier must not be empty.")
    if scope == "profiles":
        return ("steam_id", identifier)
    if scope == "id":
        return ("vanity", identifier)
    raise ValueError("Steam profile URL must use `/profiles/<steamid>` or `/id/<vanity>`.")


def _exception_message(xcp: Exception) -> str:
    if isinstance(xcp, KeyError) and xcp.args:
        return str(xcp.args[0])
    return str(xcp)


async def _resolve_alias_target_user_id(
    *,
    actor_user_id: hikari.Snowflakeish,
    requested_user: str | None,
    target_display_name: str | None = None,
    acl: Access_Control,
    names_cache: Name_Cache,
) -> int:
    user_id = int(actor_user_id)
    manual_name = target_display_name.strip() if target_display_name is not None else None
    if manual_name == "":
        manual_name = None
    if requested_user is None:
        if manual_name is not None:
            raise ValueError("`manual_name` is only valid for a raw Discord user ID that is not already in the name cache.")
        return user_id

    await acl.perm_check(actor_user_id, acl.LvL.sudo)
    resolved_user_id = names_cache.resolve_to_id(requested_user, prefer_global_name=True)
    if resolved_user_id:
        if manual_name is not None:
            raise ValueError("`manual_name` is only valid for a raw Discord user ID that is not already in the name cache.")
        return resolved_user_id

    requested = requested_user.strip()
    if requested.isdigit():
        target_user_id = int(requested)
        names_cache.upsert_manual_user(target_user_id, display_name=manual_name)
        return target_user_id

    raise KeyError("User Not Found")


def _user_names_for_editor(names_cache: Name_Cache, user_id: int) -> UserNames:
    return names_cache.by_id.setdefault(user_id, UserNames())


class AliasEditorService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(AliasActionKind)
        self._pending_minecraft_lookups: dict[tuple[int, int], tuple[MinecraftLookupCandidate, ...]] = {}
        self._editor = Editor(
            prefix=startup_editor_prefix(_ALIAS_EDITOR_PREFIX),
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
        )
        self._alias_modal = ModalKit(
            prefix=startup_editor_prefix(_ALIAS_MODAL_PREFIX),
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
        self._display_override_modal = ModalKit(
            prefix=startup_editor_prefix(_DISPLAY_OVERRIDE_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_DISPLAY_OVERRIDE_MODAL_FIELD_ID,
                        label="Display Name",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                    )
                ]
            ),
        )
        self._steam_modal = ModalKit(
            prefix=startup_editor_prefix(_STEAM_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_STEAM_MODAL_FIELD_ID,
                        label="Steam ID or URL",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                    )
                ]
            ),
        )
        self._minecraft_profile_modal = ModalKit(
            prefix=startup_editor_prefix(_MINECRAFT_PROFILE_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_MINECRAFT_PROFILE_NAME_FIELD_ID,
                        label="Minecraft Username",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=16,
                    ),
                    ModalTextField(
                        id=_MINECRAFT_PROFILE_UUID_FIELD_ID,
                        label="Minecraft UUID",
                        style=hikari.TextInputStyle.SHORT,
                        required=False,
                        max_length=36,
                    ),
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
        status: str = "Manage your identity below.",
    ) -> None:
        locale = self._editor.resolve_locale(ctx.interaction)
        embed, components = self._render_editor(
            target_user_id=target_user_id,
            actor_user_id=int(ctx.user.id),
            locale=locale,
            names_cache=names_cache,
            manager=manager,
            state=AliasEditorState(section=AliasEditorSection.OVERVIEW, page=0),
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
        if await self._alias_modal.route(
            interaction,
            on_submit=self._on_alias_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this alias editor.",
            invalid_message="Alias must not be empty.",
            acl=acl,
            names_cache=names_cache,
            manager=manager,
        ):
            return True
        if await self._display_override_modal.route(
            interaction,
            on_submit=self._on_display_override_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this alias editor.",
            invalid_message="Display name must not be empty.",
            acl=acl,
            names_cache=names_cache,
            manager=manager,
        ):
            return True
        if await self._steam_modal.route(
            interaction,
            on_submit=self._on_steam_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this alias editor.",
            invalid_message="Steam ID must not be empty.",
            acl=acl,
            names_cache=names_cache,
            manager=manager,
        ):
            return True
        return await self._minecraft_profile_modal.route(
            interaction,
            on_submit=self._on_minecraft_profile_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this alias editor.",
            invalid_message="Minecraft profile name must not be empty.",
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

    @staticmethod
    def _pending_minecraft_lookup_key(
        actor_user_id: hikari.Snowflakeish,
        target_user_id: hikari.Snowflakeish,
    ) -> tuple[int, int]:
        return (int(actor_user_id), int(target_user_id))

    def _pending_minecraft_lookup_candidates(
        self,
        actor_user_id: hikari.Snowflakeish,
        target_user_id: hikari.Snowflakeish,
    ) -> tuple[MinecraftLookupCandidate, ...]:
        return self._pending_minecraft_lookups.get(self._pending_minecraft_lookup_key(actor_user_id, target_user_id), ())

    def _set_pending_minecraft_lookup_candidates(
        self,
        actor_user_id: hikari.Snowflakeish,
        target_user_id: hikari.Snowflakeish,
        candidates: Sequence[MinecraftLookupCandidate],
    ) -> None:
        key = self._pending_minecraft_lookup_key(actor_user_id, target_user_id)
        if candidates:
            self._pending_minecraft_lookups[key] = tuple(candidates)
            return
        self._pending_minecraft_lookups.pop(key, None)

    def _clear_pending_minecraft_lookup_candidates(
        self,
        actor_user_id: hikari.Snowflakeish,
        target_user_id: hikari.Snowflakeish,
    ) -> None:
        self._pending_minecraft_lookups.pop(self._pending_minecraft_lookup_key(actor_user_id, target_user_id), None)

    async def _resolve_steam_input(self, raw_value: str) -> str:
        value = raw_value.strip()
        try:
            steam_id = Name_Cache._norm_steam_id(value)
        except ValueError:
            steam_id = None
        if steam_id is not None:
            return steam_id

        if _looks_like_url(value):
            input_kind, identifier = _parse_steamcommunity_identity(value)
            if input_kind == "steam_id":
                steam_id = Name_Cache._norm_steam_id(identifier)
                if steam_id is None:
                    raise ValueError("Steam profile URL did not contain a valid Steam ID.")
                return steam_id
            return await self._resolve_steam_vanity(identifier)

        return await self._resolve_steam_vanity(value)

    async def _resolve_steam_vanity(self, vanity: str) -> str:
        steam_api_key = (config.env_opt("STEAM_WEB_API_KEY") or "").strip()
        if not steam_api_key:
            raise ValueError("Steam vanity lookups require STEAM_WEB_API_KEY to be configured.")

        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
                params={"key": steam_api_key, "vanityurl": vanity, "url_type": 1},
            ) as response:
                response.raise_for_status()
                payload = await response.json()

        response_payload = payload.get("response")
        if not isinstance(response_payload, dict):
            raise ValueError("Steam vanity lookup returned an invalid response.")
        if int(response_payload.get("success", 0)) != 1:
            raise ValueError(f"Steam profile `{vanity}` was not found.")
        steam_id = Name_Cache._norm_steam_id(response_payload.get("steamid"))
        if steam_id is None:
            raise ValueError("Steam vanity lookup did not return a valid Steam ID.")
        return steam_id

    async def _lookup_minecraft_profiles(self, raw_name: str) -> tuple[MinecraftLookupCandidate, ...]:
        query = raw_name.strip()
        if not query:
            raise ValueError("Minecraft profile name must not be empty.")

        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.mojang.com/profiles/minecraft",
                json=[query],
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                payload = await response.json()

        if not isinstance(payload, list):
            raise ValueError("Minecraft profile lookup returned an invalid response.")

        candidates: list[MinecraftLookupCandidate] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            candidate_name = str(item.get("name", "")).strip()
            try:
                candidate_uuid = Name_Cache._normalised_game_uuid(_MINECRAFT_SCOPE, item.get("id"))
            except ValueError:
                continue
            if candidate_name and candidate_uuid is not None:
                candidates.append(MinecraftLookupCandidate(name=candidate_name, uuid=candidate_uuid))
        return tuple(candidates)

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

        if action.kind is AliasActionKind.SHOW_OVERVIEW:
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.OVERVIEW,
                status="Showing identity overview.",
                page=0,
            )

        if action.kind is AliasActionKind.SHOW_DISPLAY_OVERRIDES:
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.DISPLAY_OVERRIDES,
                status="Showing display overrides.",
                page=0,
            )

        if action.kind is AliasActionKind.SHOW_ALIASES:
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.ALIASES,
                status="Showing names and aliases.",
                page=0,
            )

        if action.kind is AliasActionKind.SHOW_LINKED_ACCOUNTS:
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.LINKED_ACCOUNTS,
                status="Showing linked accounts.",
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

        if action.kind is AliasActionKind.CHOOSE_MINECRAFT_LOOKUP:
            if not req.values:
                return EditorResponse.ephemeral("Choose a Minecraft profile first.")
            selected_uuid = req.values[0]
            candidates = self._pending_minecraft_lookup_candidates(actor_user_id, target_user_id)
            candidate = next((item for item in candidates if item.uuid == selected_uuid), None)
            if candidate is None:
                return EditorResponse.ephemeral("That Minecraft lookup result is no longer available.")
            try:
                changed = names_cache.set_game_profile(target_user_id, _MINECRAFT_SCOPE, candidate.name, candidate.uuid)
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            self._clear_pending_minecraft_lookup_candidates(actor_user_id, target_user_id)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.LINKED_ACCOUNTS,
                status=(
                    f"Selected Minecraft profile `{candidate.name}` with UUID `{candidate.uuid}`."
                    if changed
                    else f"Minecraft profile is already `{candidate.name}` with UUID `{candidate.uuid}`."
                ),
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

        if action.kind is AliasActionKind.SET_STEAM:
            current_steam_id = names_cache.get_platform_id(target_user_id, "steam") or ""
            await req.interaction.create_modal_response(
                "Set Steam ID",
                self._steam_modal.build_id(
                    self._action_codec.build(AliasActionKind.SET_STEAM, page=0),
                    scope_id=target_user_id,
                    user_id=actor_user_id,
                ),
                components=self._steam_modal.rows({_STEAM_MODAL_FIELD_ID: current_steam_id}),
            )
            return None

        if action.kind is AliasActionKind.SET_MINECRAFT_PROFILE:
            profile = _minecraft_profile_entry(_user_names_for_editor(names_cache, target_user_id))
            await req.interaction.create_modal_response(
                "Set Minecraft Profile",
                self._minecraft_profile_modal.build_id(
                    self._action_codec.build(AliasActionKind.SET_MINECRAFT_PROFILE, page=0),
                    scope_id=target_user_id,
                    user_id=actor_user_id,
                ),
                components=self._minecraft_profile_modal.rows(
                    {
                        _MINECRAFT_PROFILE_NAME_FIELD_ID: profile.alias or "",
                        _MINECRAFT_PROFILE_UUID_FIELD_ID: profile.uuid or "",
                    }
                ),
            )
            return None

        if action.kind is AliasActionKind.SET_DISPLAY_OVERRIDE:
            if not req.values:
                return EditorResponse.ephemeral("Choose a display override to edit first.")
            try:
                category = _require_display_override_category(req.values[0])
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            names = _user_names_for_editor(names_cache, target_user_id)
            current_value = names.display_overrides.get_for_category(category) or ""
            await req.interaction.create_modal_response(
                f"Set {_display_override_label(category)} Display Name",
                self._display_override_modal.build_id(
                    self._action_codec.build(AliasActionKind.SET_DISPLAY_OVERRIDE, page=action.page, value=category.value),
                    scope_id=target_user_id,
                    user_id=actor_user_id,
                ),
                components=self._display_override_modal.rows({_DISPLAY_OVERRIDE_MODAL_FIELD_ID: current_value}),
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
                section=AliasEditorSection.ALIASES,
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
                section=AliasEditorSection.ALIASES,
                status=f"Removed `{scope.title()}` alias.",
                page=0,
            )

        if action.kind is AliasActionKind.CLEAR_STEAM:
            changed = names_cache.set_platform_id(target_user_id, "steam", None)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.LINKED_ACCOUNTS,
                status="Cleared Steam ID." if changed else "No Steam ID was set.",
                page=0,
            )

        if action.kind is AliasActionKind.CLEAR_MINECRAFT_PROFILE:
            names = _user_names_for_editor(names_cache, target_user_id)
            profile = _minecraft_profile_entry(names)
            if profile.alias is None and profile.uuid is None:
                status = "No Minecraft profile was set."
            else:
                names_cache.remove_game_alias(target_user_id, _MINECRAFT_SCOPE)
                status = "Cleared Minecraft profile."
            self._clear_pending_minecraft_lookup_candidates(actor_user_id, target_user_id)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.LINKED_ACCOUNTS,
                status=status,
                page=0,
            )

        if action.kind is AliasActionKind.CLEAR_DISPLAY_OVERRIDE:
            if not req.values:
                return EditorResponse.ephemeral("Choose a display override to clear.")
            try:
                category = _require_display_override_category(req.values[0])
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            names_cache.set_display_override(target_user_id, category, None)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=req.locale,
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.DISPLAY_OVERRIDES,
                status=f"Cleared {_display_override_label(category)} display override.",
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
        pending_minecraft_candidates = self._pending_minecraft_lookup_candidates(actor_user_id, target_user_id)
        names = _user_names_for_editor(names_cache, target_user_id)
        title_name = "Your" if target_user_id == actor_user_id else (view.account_name or f"User {target_user_id}")
        section_label = _section_label(view.section)

        embed = hikari.Embed(
            title=f"{title_name} Identity Settings",
            description=(
                "Known names are read-only.\n"
                f"Section: {section_label}\n"
                f"Linked accounts: {view.linked_accounts.total_count} | "
                f"Aliases: {view.general_aliases.total_count + view.app_aliases.total_count} | "
                f"Display names: {sum(1 for entry in _display_override_entries(names) if entry.value)} custom"
            ),
            color=0xB00F0F,
        )
        if view.section is AliasEditorSection.OVERVIEW:
            embed.add_field(
                name="Linked Accounts",
                value=_display_value(
                    [
                        f"Steam: {names_cache.get_platform_id(target_user_id, 'steam') or 'Not linked'}",
                        f"Minecraft: {view.minecraft_profile.alias or 'Not linked'}",
                    ]
                ),
                inline=False,
            )
            embed.add_field(
                name="Aliases",
                value=_display_value(
                    [
                        f"General aliases: {view.general_aliases.total_count}",
                        f"Game aliases: {view.app_aliases.total_count}",
                    ]
                ),
                inline=False,
            )
            embed.add_field(
                name="Display Names",
                value=_display_value(
                    [f"{_display_override_label(entry.category)}: {entry.value or 'Default'}" for entry in view.display_overrides.visible]
                ),
                inline=False,
            )
            embed.add_field(name="Known Names", value=_display_value(view.known_names), inline=False)
        elif view.section is AliasEditorSection.DISPLAY_OVERRIDES:
            embed.add_field(
                name="Display Names",
                value=_display_value(
                    [f"{_display_override_label(entry.category)}: {entry.value or 'Default'}" for entry in view.display_overrides.visible]
                ),
                inline=False,
            )
        elif view.section is AliasEditorSection.ALIASES:
            embed.add_field(
                name=f"General Aliases ({view.general_aliases.total_count})",
                value=_display_value(view.general_aliases.visible),
                inline=False,
            )
            embed.add_field(
                name=f"Game Aliases ({view.app_aliases.total_count})",
                value=_display_value([f"{entry.scope.title()}: {entry.alias}" for entry in view.app_aliases.visible]),
                inline=False,
            )
        elif view.section is AliasEditorSection.LINKED_ACCOUNTS:
            embed.add_field(
                name=f"Linked Accounts ({view.linked_accounts.total_count})",
                value=_display_value(
                    [f"{_platform_label(entry.platform)}: {entry.value}" for entry in view.linked_accounts.visible]
                ),
                inline=False,
            )
            embed.add_field(
                name="Minecraft Profile",
                value=_display_value(
                    [
                        f"Alias: {view.minecraft_profile.alias or 'None'}",
                        f"UUID: {view.minecraft_profile.uuid or 'None'}",
                    ]
                ),
                inline=False,
            )
            if pending_minecraft_candidates:
                embed.add_field(
                    name="Minecraft Lookup Choices",
                    value=_display_value(
                        [f"{candidate.name}: {candidate.uuid}" for candidate in pending_minecraft_candidates]
                    ),
                    inline=False,
                )
        else:
            embed.add_field(
                name=f"Game-Specific Alias Targets ({view.app_scopes.total_count})",
                value=_display_value([scope.title() for scope in view.app_scopes.visible]),
                inline=False,
            )
            embed.add_field(
                name="Use This Page For",
                value="Choose a game to set or update its specific alias.",
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
                self._action_codec.build(AliasActionKind.SHOW_OVERVIEW, page=0),
                "Overview",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if view.section is AliasEditorSection.OVERVIEW
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=view.section is AliasEditorSection.OVERVIEW,
            ),
            EditorButton(
                self._action_codec.build(AliasActionKind.SHOW_LINKED_ACCOUNTS, page=0),
                "Accounts",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if view.section is AliasEditorSection.LINKED_ACCOUNTS
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=view.section is AliasEditorSection.LINKED_ACCOUNTS,
            ),
            EditorButton(
                self._action_codec.build(AliasActionKind.SHOW_ALIASES, page=0),
                "Aliases",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if view.section is AliasEditorSection.ALIASES
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=view.section is AliasEditorSection.ALIASES,
            ),
            EditorButton(
                self._action_codec.build(AliasActionKind.SHOW_DISPLAY_OVERRIDES, page=0),
                "Display",
                style=(
                    hikari.ButtonStyle.PRIMARY
                    if view.section is AliasEditorSection.DISPLAY_OVERRIDES
                    else hikari.ButtonStyle.SECONDARY
                ),
                is_disabled=view.section is AliasEditorSection.DISPLAY_OVERRIDES,
            ),
        )
        layout.next_row()

        if view.section is AliasEditorSection.OVERVIEW:
            layout.add_button(
                self._action_codec.build(AliasActionKind.SET_STEAM, page=0),
                "Link Steam",
                style=hikari.ButtonStyle.PRIMARY,
            )
            layout.add_button(
                self._action_codec.build(AliasActionKind.SET_MINECRAFT_PROFILE, page=0),
                "Link Minecraft",
                style=hikari.ButtonStyle.PRIMARY,
            )
            layout.add_button(
                self._action_codec.build(AliasActionKind.ADD_GENERAL, page=0),
                "Add Alias",
                style=hikari.ButtonStyle.SECONDARY,
            )
        if view.section is AliasEditorSection.DISPLAY_OVERRIDES:
            layout.add_text_select(
                self._action_codec.build(
                    AliasActionKind.SET_DISPLAY_OVERRIDE,
                    page=view.display_overrides.page_state.page,
                ),
                options=[
                    EditorSelectOption(
                        label=_display_override_label(entry.category),
                        value=entry.category.value,
                        description=_component_text(entry.value or "Set or update display override"),
                    )
                    for entry in view.display_overrides.visible
                ],
                placeholder="Set display override",
            )
            active_display_overrides = [entry for entry in view.display_overrides.visible if entry.value]
            if active_display_overrides:
                layout.add_text_select(
                    self._action_codec.build(
                        AliasActionKind.CLEAR_DISPLAY_OVERRIDE,
                        page=view.display_overrides.page_state.page,
                    ),
                    options=[
                        EditorSelectOption(
                            label=_display_override_label(entry.category),
                            value=entry.category.value,
                            description=_component_text(entry.value or "Clear display override"),
                        )
                        for entry in active_display_overrides
                    ],
                    placeholder="Clear display override",
                )
        if view.section is AliasEditorSection.ALIASES:
            layout.add_button(
                self._action_codec.build(AliasActionKind.ADD_GENERAL, page=view.general_aliases.page_state.page),
                "Add General Alias",
                style=hikari.ButtonStyle.PRIMARY,
            )
            layout.add_button(
                self._build_section_state_action(
                    AliasActionKind.SHOW_APP_SCOPES,
                    AliasEditorState(section=AliasEditorSection.APP_SCOPES, page=0),
                ),
                "Advanced Game Aliases",
                style=hikari.ButtonStyle.SECONDARY,
            )
        if view.section is AliasEditorSection.LINKED_ACCOUNTS:
            layout.add_button(
                self._action_codec.build(AliasActionKind.SET_STEAM, page=0),
                "Link Steam",
                style=hikari.ButtonStyle.PRIMARY,
            )
            if names_cache.get_platform_id(target_user_id, "steam") is not None:
                layout.add_button(
                    self._build_section_state_action(
                        AliasActionKind.CLEAR_STEAM,
                        AliasEditorState(section=AliasEditorSection.LINKED_ACCOUNTS, page=0),
                    ),
                    "Clear Steam ID",
                    style=hikari.ButtonStyle.SECONDARY,
                )
            layout.add_button(
                self._action_codec.build(AliasActionKind.SET_MINECRAFT_PROFILE, page=0),
                "Link Minecraft",
                style=hikari.ButtonStyle.PRIMARY,
            )
            if view.minecraft_profile.alias is not None or view.minecraft_profile.uuid is not None:
                layout.add_button(
                    self._build_section_state_action(
                        AliasActionKind.CLEAR_MINECRAFT_PROFILE,
                        AliasEditorState(section=AliasEditorSection.LINKED_ACCOUNTS, page=0),
                    ),
                    "Clear Minecraft Profile",
                    style=hikari.ButtonStyle.SECONDARY,
                )
            if pending_minecraft_candidates:
                layout.add_text_select(
                    self._action_codec.build(AliasActionKind.CHOOSE_MINECRAFT_LOOKUP, page=0),
                    options=[
                        EditorSelectOption(
                            label=_component_text(candidate.name),
                            value=candidate.uuid,
                            description=_component_text(candidate.uuid),
                        )
                        for candidate in pending_minecraft_candidates[:25]
                    ],
                    placeholder="Choose Minecraft lookup result",
                )
        if view.section is AliasEditorSection.APP_SCOPES and view.app_scopes.visible:
            layout.add_text_select(
                self._action_codec.build(AliasActionKind.SET_APP, page=view.app_scopes.page_state.page),
                options=[
                    EditorSelectOption(
                        label=_component_text(scope.title()),
                        value=scope,
                        description=_component_text(
                            names.games.get(scope, ("Set or update alias", None))[0] or "Set or update alias"
                        ),
                    )
                    for scope in view.app_scopes.visible
                ],
                placeholder="Choose a game-specific alias to edit",
            )
        if view.section is AliasEditorSection.APP_SCOPES:
            layout.add_button(
                self._build_section_state_action(
                    AliasActionKind.SHOW_ALIASES,
                    AliasEditorState(section=AliasEditorSection.APP_SCOPES, page=0),
                ),
                "Back To Aliases",
                style=hikari.ButtonStyle.SECONDARY,
            )
        if view.section is AliasEditorSection.ALIASES and view.general_aliases.visible:
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
        if view.section is AliasEditorSection.ALIASES and view.app_aliases.visible:
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
                placeholder="Remove game alias",
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

    async def _on_alias_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
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
            try:
                names_cache.add_name(target_user_id, alias, False)
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            nicknames = tuple(sorted(_user_names_for_editor(names_cache, target_user_id).nicknames))
            page = _page_for_sorted_value(nicknames, alias)
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=self._editor.resolve_locale(req.interaction),
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.ALIASES,
                status=f"Added general alias `{alias}`.",
                page=page,
            )

        if action.kind is AliasActionKind.SET_APP and action.value is not None:
            try:
                scope = _require_known_app_scope(manager, action.value)
            except (KeyError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            try:
                names_cache.set_game_alias(target_user_id, scope, alias)
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            return self._build_editor_response(
                target_user_id=target_user_id,
                actor_user_id=actor_user_id,
                locale=self._editor.resolve_locale(req.interaction),
                names_cache=names_cache,
                manager=manager,
                section=AliasEditorSection.ALIASES,
                status=f"Set `{scope.title()}` alias to `{alias}`.",
                page=0,
            )

        return EditorResponse.ephemeral("Unknown alias modal action.")

    async def _on_display_override_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        names_cache = self._require_names_cache(deps)
        manager = self._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown display override modal action.")
        if action.kind is not AliasActionKind.SET_DISPLAY_OVERRIDE or action.value is None:
            return EditorResponse.ephemeral("Unsupported display override modal action.")

        try:
            category = _require_display_override_category(action.value)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))

        actor_user_id = int(req.user_id)
        target_user_id = int(req.scope_id)
        display_name = req.values.get(_DISPLAY_OVERRIDE_MODAL_FIELD_ID, "").strip()
        if not display_name:
            return EditorResponse.ephemeral("Display name must not be empty.")

        names_cache.set_display_override(target_user_id, category, display_name)
        return self._build_editor_response(
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            locale=self._editor.resolve_locale(req.interaction),
            names_cache=names_cache,
            manager=manager,
            section=AliasEditorSection.DISPLAY_OVERRIDES,
            status=f"Set {_display_override_label(category)} display override to `{display_name}`.",
            page=action.page,
        )

    async def _on_steam_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        names_cache = self._require_names_cache(deps)
        manager = self._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown Steam modal action.")
        if action.kind is not AliasActionKind.SET_STEAM:
            return EditorResponse.ephemeral("Unsupported Steam modal action.")

        actor_user_id = int(req.user_id)
        target_user_id = int(req.scope_id)
        try:
            steam_id = await self._resolve_steam_input(req.values.get(_STEAM_MODAL_FIELD_ID, ""))
            changed = names_cache.set_platform_id(target_user_id, "steam", steam_id)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        except aiohttp.ClientError as xcp:
            log.warning("Steam lookup request failed for alias editor: %s", xcp)
            return EditorResponse.ephemeral("Steam lookup failed. Try again later or enter a numeric Steam ID.")
        current = names_cache.get_platform_id(target_user_id, "steam")
        status = (
            f"Set Steam ID to `{current}`."
            if changed and current is not None
            else f"Steam ID is already `{current}`."
        )
        return self._build_editor_response(
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            locale=self._editor.resolve_locale(req.interaction),
            names_cache=names_cache,
            manager=manager,
            section=AliasEditorSection.LINKED_ACCOUNTS,
            status=status,
            page=0,
        )

    async def _on_minecraft_profile_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        names_cache = self._require_names_cache(deps)
        manager = self._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown Minecraft profile modal action.")
        if action.kind is not AliasActionKind.SET_MINECRAFT_PROFILE:
            return EditorResponse.ephemeral("Unsupported Minecraft profile modal action.")

        actor_user_id = int(req.user_id)
        target_user_id = int(req.scope_id)
        profile_name = req.values.get(_MINECRAFT_PROFILE_NAME_FIELD_ID, "").strip()
        if not profile_name:
            return EditorResponse.ephemeral("Minecraft profile name must not be empty.")
        profile_uuid = req.values.get(_MINECRAFT_PROFILE_UUID_FIELD_ID, "").strip() or None
        resolved_profile_name = profile_name
        resolved_profile_uuid = profile_uuid
        if resolved_profile_uuid is None:
            try:
                candidates = await self._lookup_minecraft_profiles(profile_name)
            except aiohttp.ClientError as xcp:
                log.warning("Minecraft lookup request failed for alias editor: %s", xcp)
                return EditorResponse.ephemeral("Minecraft lookup failed. Try again later or enter the UUID manually.")
            if not candidates:
                return EditorResponse.ephemeral("Minecraft profile was not found. Enter the UUID manually to save it anyway.")
            if len(candidates) > 1:
                self._set_pending_minecraft_lookup_candidates(actor_user_id, target_user_id, candidates)
                return self._build_editor_response(
                    target_user_id=target_user_id,
                    actor_user_id=actor_user_id,
                    locale=self._editor.resolve_locale(req.interaction),
                    names_cache=names_cache,
                    manager=manager,
                    section=AliasEditorSection.LINKED_ACCOUNTS,
                    status="Multiple Minecraft profiles matched. Choose the correct profile below.",
                    page=0,
                )
            candidate = next(iter(candidates), None)
            if candidate is None:
                raise RuntimeError("Minecraft profile lookup returned no candidates after a non-empty validation.")
            resolved_profile_name = candidate.name
            resolved_profile_uuid = candidate.uuid
        try:
            changed = names_cache.set_game_profile(
                target_user_id,
                _MINECRAFT_SCOPE,
                resolved_profile_name,
                resolved_profile_uuid,
            )
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        self._clear_pending_minecraft_lookup_candidates(actor_user_id, target_user_id)
        profile = _minecraft_profile_entry(_user_names_for_editor(names_cache, target_user_id))
        status = (
            f"Set Minecraft profile to `{profile.alias}`"
            f"{f' with UUID `{profile.uuid}`' if profile.uuid else ''}."
            if changed and profile.alias is not None
            else f"Minecraft profile is already `{profile.alias}`"
            f"{f' with UUID `{profile.uuid}`' if profile.uuid else ''}."
        )
        return self._build_editor_response(
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            locale=self._editor.resolve_locale(req.interaction),
            names_cache=names_cache,
            manager=manager,
            section=AliasEditorSection.LINKED_ACCOUNTS,
            status=status,
            page=0,
        )

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
            AppAliasEntry(scope=scope, alias=data[0])
            for scope, data in sorted(names.games.items())
            if scope != _MINECRAFT_SCOPE and data[0]
        )
        return AliasEditorView(
            account_name=names.account,
            section=state.section,
            known_names=tuple(sorted(names.names)),
            display_overrides=_paginate(
                _display_override_entries(names),
                state.page if state.section is AliasEditorSection.DISPLAY_OVERRIDES else 0,
            ),
            general_aliases=_paginate(nicknames, state.page if state.section is AliasEditorSection.ALIASES else 0),
            app_aliases=_paginate(
                app_aliases,
                state.page if state.section is AliasEditorSection.ALIASES else 0,
            ),
            app_scopes=_paginate(all_scopes, state.page if state.section is AliasEditorSection.APP_SCOPES else 0),
            linked_accounts=_paginate(
                _linked_account_entries(names_cache, target_user_id),
                state.page if state.section is AliasEditorSection.LINKED_ACCOUNTS else 0,
            ),
            minecraft_profile=_minecraft_profile_entry(names),
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
    description="Open the identity, alias, and account linking editor",
):
    publc = lightbulb.boolean("publc", "Send the editor as a normal message", default=False)  # type: ignore[reportAssignmentType]
    user = lightbulb.string("user", "Other user", autocomplete=ac_all_ids, default=None)  # pyright: ignore[reportAssignmentType, reportArgumentType]
    manual_name = lightbulb.string("manual_name", "Manual display name for a raw uncached Discord user ID", default=None)  # pyright: ignore[reportAssignmentType, reportArgumentType]

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
        try:
            target_user_id = await _resolve_alias_target_user_id(
                actor_user_id=ctx.user.id,
                requested_user=self.user,
                target_display_name=self.manual_name,
                acl=acl,
                names_cache=names_cache,
            )
        except (KeyError, ValueError) as xcp:
            await ctx.respond(_exception_message(xcp), flags=hikari.MessageFlag.EPHEMERAL)
            return
        log.info(f"Alias.Open: {ctx.user.display_name} > {self.user}")
        await alias_editor.open_editor(
            ctx=ctx,
            names_cache=names_cache,
            manager=manager,
            target_user_id=target_user_id,
            is_public=self.publc,
        )


# AiviA APasz
