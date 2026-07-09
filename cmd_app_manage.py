from __future__ import annotations

import enum
import importlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging import Logger
from typing import Any, Generic, Protocol, TypeVar, cast

import hikari
import lightbulb
from hikari.snowflakes import Snowflake
from hikari_ui import (
    Editor,
    EditorButton,
    EditorFileUpload,
    EditorLayout,
    EditorPageState,
    EditorRequest,
    EditorResponse,
    ModalKit,
    ModalRequest,
    ModalSchema,
    ModalTextField,
    PagedActionCodec,
)
from hikari_ui.layout import EditorContext

import _errors
import config
from _discord import DC_Relay
from _editor_session import startup_editor_prefix
from _manager import App_Manager, AppInstanceCreateRequest
from _mod_ops import (
    refresh_mod_index,
    remove_mods,
    require_app_stopped_for_mod_mutation,
    toggle_coremod,
    toggle_downloadable,
    toggle_mod,
)
from _security import Access_Control
from _utils import Utilities
from apps._app import App
from apps._config import RelayChannelSource
from apps._console import ConsoleAction, ConsoleActionParameter, ConsoleActionResult
from apps._mod import Mod
from apps._settings import Setting, Settings_Manager
from config import VoiceTargetConfig

log: Logger = logging.getLogger(__name__)

_APP_MANAGE_PREFIX = "app-manage:"
_APP_MANAGE_LOCK_PREFIX = "app-manage-lock:"
_APP_CONSOLE_PREFIX = "app-console:"
_APP_SETTING_MODAL_PREFIX = "app-setting:"
_APP_CONSOLE_MODAL_PREFIX = "app-console-value:"
_APP_CREATE_MODAL_PREFIX = "app-create:"
_APP_CREATE_SATISFACTORY_MODAL_PREFIX = "app-create-satisfactory:"
_APP_SETTING_VALUE_FIELD_ID = "value"
_APP_CONSOLE_VALUE_FIELD_ID = "value"
_APP_CREATE_INSTANCE_KEY_FIELD_ID = "instance_key"
_APP_CREATE_FRIENDLY_NAME_FIELD_ID = "friendly_name"
_APP_CREATE_SUBFOLDER_FIELD_ID = "subfolder"
_APP_CREATE_PORT_FIELD_ID = "port"
_APP_CREATE_SERVER_LOG_FILE_FIELD_ID = "server_log_file"
_APP_CREATE_ADMIN_PASSWORD_FIELD_ID = "admin_password"
_MOD_UPLOAD_TTL: timedelta = timedelta(minutes=10)
_INTERACTION_RESPONSE_TTL: timedelta = timedelta(minutes=15)
_PAGE_SIZE = 25
_RELAY_CHANNEL_TYPES: tuple[hikari.ChannelType, ...] = (
    hikari.ChannelType.GUILD_TEXT,
    hikari.ChannelType.GUILD_NEWS,
)
_RELAY_VOICE_CHANNEL_TYPES: tuple[hikari.ChannelType, ...] = (
    hikari.ChannelType.GUILD_VOICE,
    hikari.ChannelType.GUILD_STAGE,
)
_STATE_VALUE_SEPARATOR = "~"
_EMBED_SPACER = "᲼"
_EMBED_SUBTEXT = "-# "

ValueT = TypeVar("ValueT")


@dataclass(frozen=True, slots=True)
class EditorStatus:
    text: str
    is_error: bool = False


class RelayVoiceTargetService(Protocol):
    def voice_target(self, guild_id: hikari.Snowflakeish) -> config.VoiceTargetConfig | None: ...

    def voice_targets(self) -> Mapping[hikari.Snowflake, config.VoiceTargetConfig]: ...

    def set_voice_target_config(
        self,
        guild_id: hikari.Snowflakeish,
        *,
        voice_channel: hikari.Snowflakeish,
        primary_tts_channel: hikari.Snowflakeish,
        primary_tts_listen_enabled: bool | None = None,
        secondary_tts_channel: hikari.Snowflakeish | None = None,
        secondary_tts_listen_enabled: bool | None = None,
        relay_tts_enabled: bool | None = None,
    ) -> config.VoiceTargetConfig: ...


class _AppManageActionsModule(Protocol):
    async def handle_app_download_action(
        self,
        host: object,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> None: ...

    async def handle_mod_download_action(
        self,
        host: object,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
        selected_mod: Mod | None,
    ) -> None: ...

    async def handle_mod_web_action(
        self,
        host: object,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> None: ...

    async def handle_app_update_action(
        self,
        host: object,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> None: ...

    async def consume_mod_upload(
        self,
        host: object,
        *,
        upload: EditorFileUpload,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> None: ...

    async def install_uploaded_mod(
        self,
        *,
        app: App,
        attachment: hikari.Attachment,
    ) -> Mod: ...

    async def edit_upload_response(
        self,
        host: object,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
        meta: ModUploadRequestMeta,
        channel_id: hikari.Snowflakeish,
        status: EditorStatus | str,
        editor_message_id: hikari.Snowflake | None = None,
        actor_user_id: hikari.Snowflakeish | None = None,
        app: App | None = None,
        state: AppManageState | None = None,
    ) -> None: ...

    async def force_invalidate_lock(
        self,
        host: object,
        *,
        bot: hikari.GatewayBot,
        lock: AppManagementLock,
        actor_user_id: int,
    ) -> bool: ...


class _AppManageRenderModule(Protocol):
    def render_landing(
        self,
        host: object,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]: ...

    def render_create(
        self,
        host: object,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]: ...

    def render_home(
        self,
        host: object,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]: ...

    def render_relay(
        self,
        host: object,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]: ...

    def render_mods(
        self,
        host: object,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]: ...

    def render_settings(
        self,
        host: object,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]: ...

    def render_setting_choices(
        self,
        host: object,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]: ...

    def build_landing_view(self, *, manager: App_Manager, state: AppManageState) -> LandingView: ...

    def build_create_view(self, *, manager: App_Manager) -> CreateView: ...

    def build_mods_view(self, *, app: App, state: AppManageState) -> ModsView: ...

    def build_settings_view(
        self,
        *,
        app: App,
        state: AppManageState,
        acl: Access_Control,
        actor_user_id: int,
    ) -> SettingsView: ...

    def build_setting_choices_view(
        self,
        *,
        setting: Setting,
        page: int,
    ) -> SettingChoicesView: ...


class _CmdAppModule(Protocol):
    def current_node_app_url(self, app_name: str) -> str: ...


def _manage_actions_module() -> _AppManageActionsModule:
    return cast(_AppManageActionsModule, cast(object, importlib.import_module("cmd_app_manage_actions")))


def _manage_render_module() -> _AppManageRenderModule:
    return cast(_AppManageRenderModule, cast(object, importlib.import_module("cmd_app_manage_render")))


class AppManageActionKind(enum.StrEnum):
    BACK_LANDING = "bl"
    BACK_HOME = "bh"
    BACK_SETTINGS = "bs"
    CLEAR_RELAY_CHANNEL = "cr"
    CLOSE = "cl"
    CREATE_INSTANCE = "ci"
    DOWNLOAD_APP = "da"
    DOWNLOAD_MOD = "dm"
    OPEN_MOD_WEB = "mw"
    OPEN_CREATE = "ca"
    OPEN_CREATE_MODAL = "cm"
    OPEN_APP = "oa"
    OPEN_RELAY = "or"
    OPEN_MODS = "om"
    SELECT_CREATE_SCOPE = "cs"
    OPEN_SETTING_CHOICES = "oc"
    OPEN_SETTINGS = "os"
    PAGE = "pg"
    REFRESH = "rf"
    REUSE_SETTING = "rv"
    REMOVE_MOD = "rm"
    REQUEST_MOD_UPLOAD = "ru"
    SAVE_RELAY_VOICE_CHANNEL = "sv"
    SAVE_RELAY_CHANNEL = "sr"
    SAVE_SETTINGS = "sa"
    SELECT_MOD = "sm"
    SELECT_SETTING = "ss"
    TOGGLE_APP = "ta"
    TOGGLE_RELAY_ADVANCEMENTS = "ra"
    TOGGLE_COREMOD = "tc"
    TOGGLE_DOWNLOADABLE = "td"
    TOGGLE_MOD = "tm"
    UPDATE_APP = "ua"
    UPDATE_SETTING = "us"
    WRITE_SETTING = "ws"


class AppManageLockActionKind(enum.StrEnum):
    FORCE_INVALIDATE = "fi"


class AppConsoleActionKind(enum.StrEnum):
    CLOSE = "cl"
    EXECUTE_ACTION = "ea"
    OPEN_ACTION_MODAL = "om"
    PAGE = "pg"
    REFRESH = "rf"
    REUSE_ACTION = "ra"
    SELECT_ACTION = "sa"


class AppManageMode(enum.StrEnum):
    CREATE = "ct"
    HOME = "hm"
    LANDING = "ld"
    MODS = "md"
    RELAY = "rl"
    SETTING_CHOICES = "sc"
    SETTINGS = "st"


APP_CONSOLE_ACTION_LEVELS: dict[AppConsoleActionKind, Access_Control.LvL] = {
    AppConsoleActionKind.CLOSE: Access_Control.LvL.user,
    AppConsoleActionKind.EXECUTE_ACTION: Access_Control.LvL.user,
    AppConsoleActionKind.OPEN_ACTION_MODAL: Access_Control.LvL.user,
    AppConsoleActionKind.PAGE: Access_Control.LvL.user,
    AppConsoleActionKind.REFRESH: Access_Control.LvL.user,
    AppConsoleActionKind.REUSE_ACTION: Access_Control.LvL.user,
    AppConsoleActionKind.SELECT_ACTION: Access_Control.LvL.user,
}


class AppManageCapability(enum.StrEnum):
    CHAT = "Chat"
    DOWNLOAD = "Download"
    TOGGLE = "Toggle"
    UPDATE = "Update"


APP_MANAGE_CAPABILITY_PERMISSIONS: dict[AppManageCapability, Access_Control.LvL] = {
    AppManageCapability.CHAT: Access_Control.LvL.sudo,
    AppManageCapability.DOWNLOAD: Access_Control.LvL.user,
    AppManageCapability.TOGGLE: Access_Control.LvL.sudo,
    AppManageCapability.UPDATE: Access_Control.LvL.sudo,
}


def _build_create_modal_schema(
    *,
    require_admin_password: bool,
    include_server_log_file: bool,
) -> ModalSchema:
    fields: list[ModalTextField] = [
        ModalTextField(
            id=_APP_CREATE_INSTANCE_KEY_FIELD_ID,
            label="Instance Key",
            style=hikari.TextInputStyle.SHORT,
            required=True,
            max_length=80,
        ),
        ModalTextField(
            id=_APP_CREATE_FRIENDLY_NAME_FIELD_ID,
            label="Friendly Name",
            style=hikari.TextInputStyle.SHORT,
            required=True,
            max_length=100,
        ),
        ModalTextField(
            id=_APP_CREATE_SUBFOLDER_FIELD_ID,
            label="DIR_APP Subfolder",
            style=hikari.TextInputStyle.SHORT,
            required=True,
            max_length=200,
        ),
        ModalTextField(
            id=_APP_CREATE_PORT_FIELD_ID,
            label="Port",
            style=hikari.TextInputStyle.SHORT,
            required=False,
            max_length=20,
        ),
    ]
    if include_server_log_file:
        fields.append(
            ModalTextField(
                id=_APP_CREATE_SERVER_LOG_FILE_FIELD_ID,
                label="Server Log File",
                style=hikari.TextInputStyle.SHORT,
                required=False,
                max_length=200,
            )
        )
    if require_admin_password:
        fields.append(
            ModalTextField(
                id=_APP_CREATE_ADMIN_PASSWORD_FIELD_ID,
                label="Admin Password",
                style=hikari.TextInputStyle.SHORT,
                required=True,
                max_length=200,
            )
        )
    return ModalSchema(fields)


APP_MANAGE_ACTION_LEVELS: dict[AppManageActionKind, Access_Control.LvL] = {
    AppManageActionKind.BACK_LANDING: Access_Control.LvL.user,
    AppManageActionKind.BACK_HOME: Access_Control.LvL.user,
    AppManageActionKind.BACK_SETTINGS: Access_Control.LvL.user,
    AppManageActionKind.CLEAR_RELAY_CHANNEL: Access_Control.LvL.sudo,
    AppManageActionKind.CLOSE: Access_Control.LvL.user,
    AppManageActionKind.CREATE_INSTANCE: Access_Control.LvL.sudo,
    AppManageActionKind.DOWNLOAD_APP: Access_Control.LvL.user,
    AppManageActionKind.DOWNLOAD_MOD: Access_Control.LvL.user,
    AppManageActionKind.OPEN_MOD_WEB: Access_Control.LvL.user,
    AppManageActionKind.OPEN_CREATE: Access_Control.LvL.sudo,
    AppManageActionKind.OPEN_CREATE_MODAL: Access_Control.LvL.sudo,
    AppManageActionKind.OPEN_APP: Access_Control.LvL.user,
    AppManageActionKind.OPEN_RELAY: Access_Control.LvL.sudo,
    AppManageActionKind.OPEN_MODS: Access_Control.LvL.user,
    AppManageActionKind.SELECT_CREATE_SCOPE: Access_Control.LvL.sudo,
    AppManageActionKind.OPEN_SETTING_CHOICES: Access_Control.LvL.user,
    AppManageActionKind.OPEN_SETTINGS: Access_Control.LvL.user,
    AppManageActionKind.PAGE: Access_Control.LvL.user,
    AppManageActionKind.REFRESH: Access_Control.LvL.user,
    AppManageActionKind.REUSE_SETTING: Access_Control.LvL.user,
    AppManageActionKind.REMOVE_MOD: Access_Control.LvL.user,
    AppManageActionKind.REQUEST_MOD_UPLOAD: Access_Control.LvL.user,
    AppManageActionKind.SAVE_RELAY_VOICE_CHANNEL: Access_Control.LvL.sudo,
    AppManageActionKind.SAVE_RELAY_CHANNEL: Access_Control.LvL.sudo,
    AppManageActionKind.SAVE_SETTINGS: Access_Control.LvL.user,
    AppManageActionKind.SELECT_MOD: Access_Control.LvL.user,
    AppManageActionKind.SELECT_SETTING: Access_Control.LvL.user,
    AppManageActionKind.TOGGLE_APP: Access_Control.LvL.sudo,
    AppManageActionKind.TOGGLE_RELAY_ADVANCEMENTS: Access_Control.LvL.sudo,
    AppManageActionKind.TOGGLE_COREMOD: Access_Control.LvL.sudo,
    AppManageActionKind.TOGGLE_DOWNLOADABLE: Access_Control.LvL.sudo,
    AppManageActionKind.TOGGLE_MOD: Access_Control.LvL.user,
    AppManageActionKind.UPDATE_APP: Access_Control.LvL.sudo,
    AppManageActionKind.UPDATE_SETTING: Access_Control.LvL.user,
    AppManageActionKind.WRITE_SETTING: Access_Control.LvL.user,
}


@dataclass(frozen=True, slots=True)
class AppManageState:
    mode: AppManageMode
    page: int
    app_name: str | None = None
    selected_page_slot: int | None = None
    selected_setting_index: int | None = None

    @property
    def is_home(self) -> bool:
        return self.mode is AppManageMode.HOME

    @property
    def is_create(self) -> bool:
        return self.mode is AppManageMode.CREATE

    @property
    def is_mods(self) -> bool:
        return self.mode is AppManageMode.MODS

    @property
    def is_relay(self) -> bool:
        return self.mode is AppManageMode.RELAY

    @property
    def is_settings(self) -> bool:
        return self.mode is AppManageMode.SETTINGS

    @property
    def is_setting_choices(self) -> bool:
        return self.mode is AppManageMode.SETTING_CHOICES


@dataclass(frozen=True, slots=True)
class AppConsoleState:
    page: int
    app_name: str
    selected_action_index: int | None = None


@dataclass(frozen=True, slots=True)
class PagedItems(Generic[ValueT]):
    visible: tuple[ValueT, ...]
    total_count: int
    page_state: EditorPageState


@dataclass(frozen=True, slots=True)
class LandingView:
    apps: PagedItems[App]


@dataclass(frozen=True, slots=True)
class CreateView:
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModsView:
    mods: PagedItems[Mod]
    enabled_count: int
    disabled_count: int
    coremod_count: int
    downloadable_count: int
    selected_mod_slot: int | None
    selected_mod: Mod | None


@dataclass(frozen=True, slots=True)
class SettingsView:
    settings: PagedItems[Setting]
    editable_count: int
    restricted_count: int
    selected_setting_slot: int | None
    selected_setting: Setting | None


@dataclass(frozen=True, slots=True)
class SettingChoiceEntry:
    label: str
    raw_value: str


@dataclass(frozen=True, slots=True)
class SettingChoicesView:
    setting: Setting
    choices: PagedItems[SettingChoiceEntry]


@dataclass(frozen=True, slots=True)
class ConsoleActionView:
    actions: PagedItems[ConsoleAction]
    selected_action: ConsoleAction | None
    selected_action_slot: int | None


@dataclass(frozen=True, slots=True)
class ConsoleActionExecutionView:
    status: EditorStatus
    success: bool


@dataclass(frozen=True, slots=True)
class AppManagementLock:
    message_id: hikari.Snowflake
    user_id: hikari.Snowflake
    app_name: str
    channel_id: hikari.Snowflake | None = None
    guild_id: hikari.Snowflake | None = None
    application_id: hikari.Snowflake | None = None
    interaction_token: str | None = None
    response_expires_at: datetime | None = None

    def location_text(self) -> str:
        if self.channel_id is not None:
            return f"<#{int(self.channel_id)}>"
        if self.guild_id is not None:
            return f"guild `{int(self.guild_id)}`"
        return "unknown"

    def can_force_close(self, *, now: datetime) -> bool:
        return (
            self.application_id is not None
            and self.interaction_token is not None
            and self.response_expires_at is not None
            and self.response_expires_at > now
        )


@dataclass(frozen=True, slots=True)
class ModUploadRequestMeta:
    app_name: str
    page: int
    selected_mod_slot: int | None
    application_id: hikari.Snowflake
    interaction_token: str
    locale: hikari.Locale

    def to_mapping(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "page": self.page,
            "selected_mod_slot": self.selected_mod_slot,
            "application_id": int(self.application_id),
            "interaction_token": self.interaction_token,
            "locale": self.locale.value,
        }


def _page_count(count: int) -> int:
    return max(1, (count + _PAGE_SIZE - 1) // _PAGE_SIZE)


def _clamp_page(page: int, total_pages: int) -> int:
    if page < 0:
        return 0
    if page >= total_pages:
        return total_pages - 1
    return page


def _page_slice(values: Sequence[ValueT], page: int) -> Sequence[ValueT]:
    start: int = page * _PAGE_SIZE
    end: int = start + _PAGE_SIZE
    return values[start:end]


def _paginate(values: Sequence[ValueT], page: int) -> PagedItems[ValueT]:
    total_pages: int = _page_count(len(values))
    current_page: int = _clamp_page(page, total_pages)
    return PagedItems[ValueT](
        visible=tuple[ValueT, ...](_page_slice(values, current_page)),
        total_count=len(values),
        page_state=EditorPageState(page=current_page, total_pages=total_pages),
    )


def _all_apps(manager: App_Manager) -> tuple[App, ...]:
    return tuple(sorted(manager.apps.values(), key=lambda app: app.friendly.casefold()))


def _component_text(value: str, /, *, limit: int = 100) -> str:
    stripped: str = value.strip()
    if len(stripped) <= limit:
        return stripped
    if limit <= 3:
        return stripped[:limit]
    return stripped[: limit - 3].rstrip() + "..."


def _display_value(values: Sequence[str]) -> str:
    return "\n".join(values) if values else "None"


def _app_started_response_text(app: App) -> str:
    lines: list[str] = [f"{app.friendly} Started!"]
    if app.cfg.join_display_address is not None:
        lines.append(f"Join: `{app.cfg.join_display_address}`")
    lines.extend(app.lifecycle_relay_description_lines(started=True))
    return "\n".join(lines)


def _parse_optional_port(raw: str) -> int | None:
    value: str = raw.strip()
    if not value:
        return None
    if not value.isdecimal():
        raise ValueError("Port must be a whole number.")
    port: int = int(value)
    if port <= 0 or port > 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return port


def _required_level_for_capability(capability: AppManageCapability) -> Access_Control.LvL:
    return APP_MANAGE_CAPABILITY_PERMISSIONS[capability]


def _state_value(state: AppManageState) -> str:
    app_name: str = state.app_name or ""
    selected_page_slot: str = "" if state.selected_page_slot is None else str(state.selected_page_slot)
    selected_setting_index: str = "" if state.selected_setting_index is None else str(state.selected_setting_index)
    for value in (state.mode.value, app_name, selected_page_slot, selected_setting_index):
        if _STATE_VALUE_SEPARATOR in value:
            raise ValueError(f"state value must not contain '{_STATE_VALUE_SEPARATOR}'")
    return _STATE_VALUE_SEPARATOR.join((state.mode.value, app_name, selected_page_slot, selected_setting_index))


def _state_from_value(raw: str | None, page: int) -> AppManageState | None:
    if raw is None:
        return AppManageState(mode=AppManageMode.LANDING, page=page)

    parts: list[str] = raw.split(_STATE_VALUE_SEPARATOR)
    if len(parts) == 2:
        raw_mode, raw_app_name = parts
        raw_selected_page_slot = ""
        raw_selected_setting_index = ""
    elif len(parts) == 3:
        raw_mode, raw_app_name, raw_selected_page_slot = parts
        raw_selected_setting_index = ""
    elif len(parts) == 4:
        raw_mode, raw_app_name, raw_selected_page_slot, raw_selected_setting_index = parts
    elif len(parts) == 5:
        raw_mode, raw_app_name, raw_selected_page_slot, raw_selected_setting_index, _raw_legacy_relay_state = parts
    else:
        return None

    try:
        mode: AppManageMode = AppManageMode(raw_mode)
    except ValueError:
        return None

    app_name: str | None = raw_app_name or None
    selected_page_slot: int | None = None
    if raw_selected_page_slot:
        try:
            selected_page_slot = int(raw_selected_page_slot)
        except ValueError:
            return None
        if selected_page_slot < 0:
            return None

    selected_setting_index: int | None = None
    if raw_selected_setting_index:
        try:
            selected_setting_index = int(raw_selected_setting_index)
        except ValueError:
            return None
        if selected_setting_index < 0:
            return None

    state: AppManageState = AppManageState(
        mode=mode,
        app_name=app_name,
        page=page,
        selected_page_slot=selected_page_slot,
        selected_setting_index=selected_setting_index,
    )
    if (
        state.mode
        in {
            AppManageMode.HOME,
            AppManageMode.MODS,
            AppManageMode.SETTINGS,
            AppManageMode.SETTING_CHOICES,
        }
        and state.app_name is None
    ):
        return None
    if state.mode is AppManageMode.SETTING_CHOICES and state.selected_setting_index is None:
        return None
    return state


def _console_state_value(state: AppConsoleState) -> str:
    selected_action_index: str = "" if state.selected_action_index is None else str(state.selected_action_index)
    for value in (state.app_name, selected_action_index):
        if _STATE_VALUE_SEPARATOR in value:
            raise ValueError(f"state value must not contain '{_STATE_VALUE_SEPARATOR}'")
    return _STATE_VALUE_SEPARATOR.join((state.app_name, selected_action_index))


def _console_state_from_value(raw: str | None, page: int) -> AppConsoleState | None:
    if raw is None:
        return None
    parts: list[str] = raw.split(_STATE_VALUE_SEPARATOR)
    if len(parts) != 2:
        return None
    raw_app_name, raw_selected_action_index = parts
    if not raw_app_name:
        return None
    selected_action_index: int | None = None
    if raw_selected_action_index:
        try:
            selected_action_index = int(raw_selected_action_index)
        except ValueError:
            return None
        if selected_action_index < 0:
            return None
    return AppConsoleState(
        page=page,
        app_name=raw_app_name,
        selected_action_index=selected_action_index,
    )


def _app_capabilities(app: App) -> tuple[AppManageCapability, ...]:
    capabilities: list[AppManageCapability] = []
    if app.directory.exists():
        capabilities.append(AppManageCapability.DOWNLOAD)
    if app.supports_chat_relay:
        capabilities.append(AppManageCapability.CHAT)
    capabilities.append(AppManageCapability.TOGGLE)
    if app.updater is not None:
        capabilities.append(AppManageCapability.UPDATE)
    return tuple[AppManageCapability, ...](capabilities)


def _app_extra_capability_labels(app: App) -> tuple[str, ...]:
    labels: list[str] = []
    for capability in _app_capabilities(app):
        if capability is AppManageCapability.TOGGLE:
            continue
        if capability is AppManageCapability.CHAT:
            chat_label: str | None = app.chat_relay_support.capability_label
            if chat_label is not None:
                labels.append(chat_label)
            continue
        labels.append(capability.value)
    if app.mods is not None:
        labels.append("Mods")
    if app.settings is not None:
        labels.append("Settings")
    return tuple[str, ...](labels)


def _app_option_description(app: App) -> str:
    labels: tuple[str, ...] = _app_extra_capability_labels(app)
    if not labels:
        return "No extra actions"
    return ", ".join(labels)


def _app_status_lines(app: App) -> tuple[str, ...]:
    return app.manager_status_lines


def _app_summary_line(app: App) -> str:
    if app.check_running():
        return "Running"
    return app.cfg.enabled_txt


@dataclass(frozen=True, slots=True)
class GuildScopedChannelSummary:
    current: hikari.Snowflake | None
    others: tuple[hikari.Snowflake, ...]


def _channel_display(channel_id: hikari.Snowflake | None) -> str:
    if channel_id is None:
        return "unset"
    return f"<#{int(channel_id)}>"


def _channels_display(channel_ids: Sequence[hikari.Snowflake]) -> str:
    if not channel_ids:
        return "unset"
    return ", ".join(f"<#{int(channel_id)}>" for channel_id in channel_ids)


def _manager_default_chat_channels(manager: App_Manager) -> tuple[hikari.Snowflake, ...]:
    channel_ids: Any | tuple[()] = getattr(manager, "default_chat_channels", ())
    if isinstance(channel_ids, tuple | list | set | frozenset) and channel_ids:
        return tuple[Snowflake, ...](hikari.Snowflake(channel_id) for channel_id in channel_ids)
    channel_id = getattr(manager, "default_chat_channel", None)
    if channel_id is None:
        return ()
    return (hikari.Snowflake(channel_id),)


def _app_override_channels(app: App) -> tuple[hikari.Snowflake, ...]:
    override_channels = tuple(getattr(app, "chat_channel_overrides", ()))
    if override_channels:
        return override_channels
    if getattr(app, "chat_channel_source", RelayChannelSource.NONE) is RelayChannelSource.INSTANCE:
        return tuple(getattr(app, "chat_channels", ()))
    return ()


def _cached_channel_guild_id(
    manager: App_Manager,
    channel_id: hikari.Snowflakeish,
) -> hikari.Snowflake | None:
    bot = getattr(manager, "bot", None)
    if bot is None:
        return None
    cache = getattr(bot, "cache", None)
    if cache is None:
        return None

    resolved_channel_id = hikari.Snowflake(channel_id)
    get_guild_channel = getattr(cache, "get_guild_channel", None)
    get_thread = getattr(cache, "get_thread", None)
    channel = (get_guild_channel(resolved_channel_id) if callable(get_guild_channel) else None) or (
        get_thread(resolved_channel_id) if callable(get_thread) else None
    )
    if channel is None:
        channel = DC_Relay._channel_objects.get(resolved_channel_id)
    guild_id = getattr(channel, "guild_id", None)
    if guild_id is None or not isinstance(guild_id, int | str | hikari.Snowflake):
        return None
    return hikari.Snowflake(guild_id)


def _guild_scoped_channel_summary(
    channel_ids: Sequence[hikari.Snowflake],
    *,
    manager: App_Manager,
    current_guild_id: hikari.Snowflakeish | None,
) -> GuildScopedChannelSummary:
    target_guild_id: Snowflake | None = hikari.Snowflake(current_guild_id) if current_guild_id is not None else None
    current_channel_id: hikari.Snowflake | None = None
    other_channel_ids: list[hikari.Snowflake] = []
    seen_channel_ids: set[int] = set[int]()

    for channel_id in channel_ids:
        resolved_channel_id: Snowflake = hikari.Snowflake(channel_id)
        channel_key: int = int(resolved_channel_id)
        if channel_key in seen_channel_ids:
            continue
        seen_channel_ids.add(channel_key)

        if target_guild_id is not None and _cached_channel_guild_id(manager, resolved_channel_id) == target_guild_id:
            if current_channel_id is None:
                current_channel_id = resolved_channel_id
                continue
        other_channel_ids.append(resolved_channel_id)

    return GuildScopedChannelSummary(current=current_channel_id, others=tuple(other_channel_ids))


def _relay_summary_display(summary: GuildScopedChannelSummary) -> str:
    return f"{_channel_display(summary.current)} | {_channels_display(summary.others)}"


def _voice_target_map(
    voice_target_service: RelayVoiceTargetService | None,
) -> Mapping[hikari.Snowflake, config.VoiceTargetConfig]:
    if voice_target_service is None:
        return {}
    voice_targets: Mapping[Snowflake, VoiceTargetConfig] = voice_target_service.voice_targets()
    if not isinstance(voice_targets, Mapping):
        return {}
    return voice_targets


def _voice_channel_summary_for_text_channels(
    text_channel_ids: Sequence[hikari.Snowflake],
    *,
    manager: App_Manager,
    current_guild_id: hikari.Snowflakeish | None,
    voice_target_service: RelayVoiceTargetService | None,
) -> GuildScopedChannelSummary:
    voice_targets: Mapping[Snowflake, VoiceTargetConfig] = _voice_target_map(voice_target_service)
    target_guild_id: Snowflake | None = hikari.Snowflake(current_guild_id) if current_guild_id is not None else None
    current_voice_channel_id: hikari.Snowflake | None = None
    other_voice_channel_ids: list[hikari.Snowflake] = []
    seen_voice_channel_ids: set[int] = set[int]()

    for text_channel_id in text_channel_ids:
        guild_id: Snowflake | None = _cached_channel_guild_id(manager, text_channel_id)
        if guild_id is None:
            continue
        voice_target: VoiceTargetConfig | None = voice_targets.get(guild_id)
        if (
            voice_target is None
            or not voice_target.relay_tts_enabled
            or voice_target.primary_tts_channel != hikari.Snowflake(text_channel_id)
        ):
            continue
        voice_channel_id: Snowflake = hikari.Snowflake(voice_target.voice_channel)
        voice_channel_key: int = int(voice_channel_id)
        if voice_channel_key in seen_voice_channel_ids:
            continue
        seen_voice_channel_ids.add(voice_channel_key)
        if target_guild_id is not None and guild_id == target_guild_id:
            if current_voice_channel_id is None:
                current_voice_channel_id = voice_channel_id
                continue
        other_voice_channel_ids.append(voice_channel_id)

    return GuildScopedChannelSummary(current=current_voice_channel_id, others=tuple(other_voice_channel_ids))


def _default_relay_lines(
    manager: App_Manager,
    *,
    current_guild_id: hikari.Snowflakeish | None = None,
    voice_target_service: RelayVoiceTargetService | None = None,
) -> tuple[str, ...]:
    default_text_channels: tuple[Snowflake, ...] = _manager_default_chat_channels(manager)
    default_text: GuildScopedChannelSummary = _guild_scoped_channel_summary(
        default_text_channels,
        manager=manager,
        current_guild_id=current_guild_id,
    )
    default_voice: GuildScopedChannelSummary = _voice_channel_summary_for_text_channels(
        default_text_channels,
        manager=manager,
        current_guild_id=current_guild_id,
        voice_target_service=voice_target_service,
    )
    return (
        f"Text: {_relay_summary_display(default_text)}",
        f"Voice: {_relay_summary_display(default_voice)}",
    )


def _app_relay_lines(
    app: App,
    manager: App_Manager,
    *,
    current_guild_id: hikari.Snowflakeish | None = None,
    voice_target_service: RelayVoiceTargetService | None = None,
) -> tuple[str, ...]:
    override_text_channels: tuple[Snowflake, ...] = _app_override_channels(app)
    override_text: GuildScopedChannelSummary = _guild_scoped_channel_summary(
        override_text_channels,
        manager=manager,
        current_guild_id=current_guild_id,
    )
    override_voice: GuildScopedChannelSummary = _voice_channel_summary_for_text_channels(
        override_text_channels,
        manager=manager,
        current_guild_id=current_guild_id,
        voice_target_service=voice_target_service,
    )
    default_text_channels: tuple[Snowflake, ...] = _manager_default_chat_channels(manager)
    default_text: GuildScopedChannelSummary = _guild_scoped_channel_summary(
        default_text_channels,
        manager=manager,
        current_guild_id=current_guild_id,
    )
    default_voice: GuildScopedChannelSummary = _voice_channel_summary_for_text_channels(
        default_text_channels,
        manager=manager,
        current_guild_id=current_guild_id,
        voice_target_service=voice_target_service,
    )
    lines: list[str] = [
        f"Support: {app.chat_relay_support.display_value}",
        f"Text: {_relay_summary_display(override_text)}",
        f"Voice: {_relay_summary_display(override_voice)}",
        f"Default: {_channel_display(default_text.current)} | {_channel_display(default_voice.current)}",
    ]
    if app.relay_advancements_enabled is not None:
        lines.append(
            f"{app.relay_advancement_term_plural}: {'Enabled' if app.relay_advancements_enabled else 'Disabled'}"
        )
    return tuple[str, ...](lines)


def _error_status(text: str) -> EditorStatus:
    return EditorStatus(text=text, is_error=True)


def _coerce_status(status: EditorStatus | str | None) -> EditorStatus | None:
    if status is None or isinstance(status, EditorStatus):
        return status
    return EditorStatus(text=status, is_error=status.casefold().startswith("error:"))


def _status_text(status: EditorStatus | None) -> str | None:
    if status is None:
        return None
    return status.text


def _editor_title(title: str, *, status: EditorStatus | None) -> str:
    return f"Error | {title}" if status is not None and status.is_error else title


def _console_action_result_status_text(result: ConsoleActionResult) -> str:
    status_text: str = result.summary
    response_text: str | None = result.text.strip() if result.text else None
    if response_text:
        status_text = f"{status_text}\nResult: {response_text}"
    if result.success:
        return status_text
    if status_text.casefold().startswith("error:"):
        return status_text
    return f"Error: {status_text}"


async def _send_public_action_notice(
    bot: hikari.GatewayBot,
    interaction: hikari.ComponentInteraction | hikari.ModalInteraction,
    message: str,
) -> None:
    channel_id = getattr(interaction, "channel_id", None)
    if channel_id is None:
        return
    await bot.rest.create_message(channel_id, message)


def _current_node_app_url(app_name: str) -> str:
    cmd_app = cast(_CmdAppModule, cast(object, importlib.import_module("cmd_app")))
    return cmd_app.current_node_app_url(app_name)


def _public_setting_update_text(
    *,
    actor_user_id: int,
    app: App,
    setting: Setting,
) -> str:
    if app.settings is None:
        raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")
    return (
        f"<@{actor_user_id}> set {app.friendly} `{setting.label}` to "
        f"`{_setting_display_value(setting, settings_manager=app.settings, actor_user_id=actor_user_id)}`."
    )


def _public_console_action_text(
    *,
    actor_user_id: int,
    app: App,
    action: ConsoleAction,
    raw_value: str | None,
) -> str:
    if action.parameter is None or raw_value is None or not raw_value.strip():
        return f"<@{actor_user_id}> ran {app.friendly} `{action.label}`."
    value = action.parameter.normalise_input(raw_value.strip())
    return f"<@{actor_user_id}> ran {app.friendly} `{action.label}` with `{value}`."


def _mod_upload_meta_from_mapping(raw: Mapping[str, object]) -> ModUploadRequestMeta | None:
    app_name = raw.get("app_name")
    page = raw.get("page")
    selected_mod_slot = raw.get("selected_mod_slot")
    application_id = raw.get("application_id")
    interaction_token = raw.get("interaction_token")
    raw_locale = raw.get("locale")
    if not isinstance(app_name, str) or not app_name:
        return None
    if not isinstance(page, int) or page < 0:
        return None
    if selected_mod_slot is not None and (not isinstance(selected_mod_slot, int) or selected_mod_slot < 0):
        return None
    if not isinstance(application_id, int):
        return None
    if not isinstance(interaction_token, str) or not interaction_token:
        return None
    if not isinstance(raw_locale, str):
        return None
    try:
        locale = hikari.Locale(raw_locale)
    except ValueError:
        locale = hikari.Locale.EN_GB
    return ModUploadRequestMeta(
        app_name=app_name,
        page=page,
        selected_mod_slot=selected_mod_slot,
        application_id=hikari.Snowflake(application_id),
        interaction_token=interaction_token,
        locale=locale,
    )


def _page_for_item_index(index: int) -> int:
    if index < 0:
        raise ValueError("index must not be negative")
    return index // _PAGE_SIZE


def _mod_option_description(mod: Mod) -> str:
    labels = ["Enabled" if mod.cfg.enabled else "Disabled"]
    if mod.is_builtin:
        labels.append("Built-in")
    elif mod.is_coremod_type:
        labels.append("Coremod")
    if not mod.downloadable:
        labels.append(mod.download_block_label or "Not downloadable")
    return ", ".join(labels)


def _mod_overview_lines(view: ModsView) -> tuple[str, ...]:
    return (
        f"total: {view.mods.total_count}",
        f"enabled: {view.enabled_count}",
        f"disabled: {view.disabled_count}",
        f"coremods: {view.coremod_count}",
        f"downloadable: {view.downloadable_count}",
    )


def _mod_status_lines(
    mod: Mod,
    *,
    acl: Access_Control,
    actor_user_id: int,
) -> tuple[str, ...]:
    lines = [
        f"name: {mod.name}",
        f"status: {'Enabled' if mod.cfg.enabled else 'Disabled'}",
        f"origin: {mod.cfg.origin}",
        f"version: {mod.cfg.version or 'none'}",
        f"type: {mod.mod_type.label}",
        f"coremod: {'Yes' if mod.is_coremod_type else 'No'}",
        f"downloadable: {'Yes' if mod.downloadable else 'No'}",
    ]
    if not mod.downloadable:
        lines.append(f"download block: {mod.download_block_label or 'Not downloadable'}")
    if mod.is_protected:
        protected_label = "built-in mod" if mod.is_builtin else "coremod"
        if acl.can(actor_user_id, acl.LvL.sudo):
            lines.append(f"restriction: Protected {protected_label}; sudo override is active")
        else:
            lines.append(f"restriction: Protected {protected_label}; toggle/remove requires sudo")
    return tuple(lines)


def _setting_can_edit(setting: Setting, *, acl: Access_Control, actor_user_id: int) -> bool:
    return acl.can(actor_user_id, setting.power_level)


def _setting_choice_items(setting: Setting) -> tuple[tuple[str, str], ...]:
    return setting.choice_items()


def _setting_supports_choice_select(setting: Setting) -> bool:
    choice_count = len(_setting_choice_items(setting))
    return 0 < choice_count <= 25


def _setting_recent_items(setting: Setting) -> tuple[str, ...]:
    if not setting.supports_recent_inputs:
        return ()
    return setting.recent_inputs


def _setting_supports_recent_select(setting: Setting) -> bool:
    return 0 < len(_setting_recent_items(setting)) <= 25


def _setting_recent_value_at(setting: Setting, raw_index: str) -> str | None:
    if not raw_index.isdecimal():
        return None
    index = int(raw_index)
    recent_items = _setting_recent_items(setting)
    if index < 0 or index >= len(recent_items):
        return None
    return recent_items[index]


def _setting_requires_choice_browser(setting: Setting) -> bool:
    return len(_setting_choice_items(setting)) > 25


def _setting_allows_modal_entry(setting: Setting) -> bool:
    return not setting.choices or not setting.strict_choice


def _setting_current_input_value(
    setting: Setting,
    *,
    settings_manager: Settings_Manager,
    actor_user_id: int,
) -> str:
    return settings_manager.current_input_value(setting, actor_user_id)


def _setting_display_value(
    setting: Setting,
    *,
    settings_manager: Settings_Manager,
    actor_user_id: int,
) -> str:
    return settings_manager.display_value(setting, actor_user_id)


def _setting_choice_summary(setting: Setting, *, limit: int = 6) -> str:
    labels = [label for label, _ in _setting_choice_items(setting)]
    if not labels:
        return "custom value"
    if len(labels) <= limit:
        return ", ".join(labels)
    return f"{', '.join(labels[:limit])}, +{len(labels) - limit} more"


def _setting_option_description(setting: Setting) -> str:
    descriptors = [setting.value_type.__name__, setting.power_level.name.title()]
    choice_count = len(_setting_choice_items(setting))
    if choice_count:
        descriptors.append(f"{choice_count} choices")
    return ", ".join(descriptors)


def _page_for_setting_key(settings: Sequence[Setting], setting_key: str | None) -> int:
    if setting_key is None:
        return 0
    for index, setting in enumerate(settings):
        if setting.key == setting_key:
            return _page_for_item_index(index)
    return 0


def _page_for_setting_choice(
    setting: Setting,
    *,
    settings_manager: Settings_Manager,
    actor_user_id: int,
) -> int:
    choice_label = setting.spec.choice_label_for_value(settings_manager.value_for(setting, actor_user_id))
    if choice_label is None:
        return 0
    for index, (label, _) in enumerate(_setting_choice_items(setting)):
        if label == choice_label:
            return _page_for_item_index(index)
    return 0


def _settings_overview_lines(view: SettingsView) -> tuple[str, ...]:
    return (
        f"total: {view.settings.total_count}",
        f"editable: {view.editable_count}",
        f"restricted: {view.restricted_count}",
    )


def _setting_status_lines(
    setting: Setting,
    *,
    settings_manager: Settings_Manager,
    acl: Access_Control,
    actor_user_id: int,
) -> tuple[str, ...]:
    can_edit = _setting_can_edit(setting, acl=acl, actor_user_id=actor_user_id)
    lines = [
        f"key: {setting.key}",
        f"type: {setting.value_type.__name__}",
        (
            f"value: {_setting_display_value(setting, settings_manager=settings_manager, actor_user_id=actor_user_id)}"
            if can_edit
            else f"value: hidden (requires {setting.power_level.name.title()})"
        ),
        f"permission: {setting.power_level.name.title()}",
    ]
    if settings_manager.has_pending_value(actor_user_id, setting):
        lines.append("draft: pending save")
    choice_count = len(_setting_choice_items(setting))
    if choice_count:
        lines.append(f"choices: {_setting_choice_summary(setting)}")
        if choice_count > 25:
            if _setting_allows_modal_entry(setting):
                lines.append("input: use the choice browser or value modal below")
            else:
                lines.append("input: use the choice browser below")
        else:
            lines.append("input: use the choice selector below")
    else:
        lines.append("input: use the value modal below")
    recent_count = len(_setting_recent_items(setting))
    if recent_count:
        lines.append(f"recent: {recent_count}")
    if setting.desc:
        lines.append(f"details: {setting.desc}")
    return tuple(lines)


def _console_action_choice_items(parameter: ConsoleActionParameter[object] | None) -> tuple[tuple[str, str], ...]:
    if parameter is None:
        return ()
    return parameter.choice_items()


def _console_action_supports_choice_select(action: ConsoleAction) -> bool:
    choice_count = len(_console_action_choice_items(action.parameter))
    return 0 < choice_count <= 25


def _console_action_allows_modal_entry(action: ConsoleAction) -> bool:
    if action.parameter is None:
        return False
    return not action.parameter.choices or not action.parameter.strict_choice


def _console_action_recent_items(parameter: ConsoleActionParameter[object] | None) -> tuple[str, ...]:
    if parameter is None or not parameter.supports_recent_inputs:
        return ()
    return parameter.recent_inputs


def _console_action_supports_recent_select(action: ConsoleAction) -> bool:
    return 0 < len(_console_action_recent_items(action.parameter)) <= 25


def _console_action_recent_value_at(action: ConsoleAction, raw_index: str) -> str | None:
    if not raw_index.isdecimal():
        return None
    recent_items = _console_action_recent_items(action.parameter)
    index = int(raw_index)
    if index < 0 or index >= len(recent_items):
        return None
    return recent_items[index]


def _console_action_option_description(action: ConsoleAction) -> str:
    descriptors = [action.power_level.name.title()]
    if action.parameter is None:
        descriptors.append("no input")
    else:
        descriptors.append(action.parameter.value_type.__name__)
        choice_count = len(_console_action_choice_items(action.parameter))
        if choice_count:
            descriptors.append(f"{choice_count} choices")
    return ", ".join(descriptors)


def _console_action_status_lines_for_view(action: ConsoleAction) -> tuple[str, ...]:
    lines = [
        f"key: {action.key}",
        f"permission: {action.power_level.name.title()}",
        f"details: {action.description}",
    ]
    if action.parameter is None:
        return tuple(lines)
    if not _console_action_supports_choice_select(action):
        lines.append(f"input: {action.parameter.label} ({action.parameter.value_type.__name__})")
    recent_count = len(_console_action_recent_items(action.parameter))
    if recent_count:
        lines.append(f"recent: {recent_count}")
    if action.parameter.desc:
        lines.append(f"argument: {action.parameter.desc}")
    return tuple(lines)


async def ac_console_apps(ctx: lightbulb.AutocompleteContext[str], manager: App_Manager) -> None:
    await ctx.respond([app.friendly for app in manager.apps.values() if app.supports_console_actions])


class AppManageService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(AppManageActionKind)
        self._lock_action_codec = PagedActionCodec(AppManageLockActionKind)
        self._editor = Editor(
            prefix=startup_editor_prefix(_APP_MANAGE_PREFIX),
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
        )
        self._lock_editor = Editor(
            prefix=startup_editor_prefix(_APP_MANAGE_LOCK_PREFIX),
            on_action=self._on_lock_action,
            authoriser=self._authorise_lock_action,
        )
        self._setting_modal = ModalKit(
            prefix=startup_editor_prefix(_APP_SETTING_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_APP_SETTING_VALUE_FIELD_ID,
                        label="Value",
                        style=hikari.TextInputStyle.PARAGRAPH,
                        required=True,
                        max_length=2000,
                    )
                ]
            ),
        )
        self._create_modal = ModalKit(
            prefix=startup_editor_prefix(_APP_CREATE_MODAL_PREFIX),
            schema=_build_create_modal_schema(
                require_admin_password=False,
                include_server_log_file=True,
            ),
        )
        self._create_satisfactory_modal = ModalKit(
            prefix=startup_editor_prefix(_APP_CREATE_SATISFACTORY_MODAL_PREFIX),
            schema=_build_create_modal_schema(
                require_admin_password=True,
                include_server_log_file=False,
            ),
        )
        self._app_locks: dict[hikari.Snowflake, AppManagementLock] = {}
        self._voice_target_service: RelayVoiceTargetService | None = None

    def set_voice_target_service(self, voice_target_service: RelayVoiceTargetService | None) -> None:
        self._voice_target_service = voice_target_service

    @staticmethod
    def _interaction_message_id(interaction: hikari.ComponentInteraction) -> hikari.Snowflake | None:
        message = getattr(interaction, "message", None)
        if message is None:
            return None
        message_id = getattr(message, "id", None)
        if message_id is None:
            return None
        return hikari.Snowflake(message_id)

    @staticmethod
    def _response_message_id(response: object) -> hikari.Snowflake | None:
        if isinstance(response, hikari.Message):
            return response.id
        candidate = getattr(response, "id", None)
        if candidate is None:
            candidate = response
        if not isinstance(candidate, (int, str, hikari.Snowflake)):
            return None
        try:
            return hikari.Snowflake(candidate)
        except TypeError, ValueError:
            return None

    @staticmethod
    def _interaction_guild_id(
        interaction: hikari.ComponentInteraction | hikari.ModalInteraction,
    ) -> hikari.Snowflake | None:
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is None:
            return None
        if not isinstance(guild_id, int | str | hikari.Snowflake):
            return None
        return hikari.Snowflake(guild_id)

    @staticmethod
    def _ensure_aware_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @staticmethod
    async def _resolve_channel(
        *,
        bot: hikari.GatewayBot,
        channel_id: hikari.Snowflakeish,
    ) -> object | None:
        channel_snowflake = hikari.Snowflake(channel_id)
        channel = bot.cache.get_guild_channel(channel_snowflake) or bot.cache.get_thread(channel_snowflake)
        if channel is None:
            try:
                channel = await bot.rest.fetch_channel(channel_snowflake)
            except hikari.NotFoundError:
                return None
            except Exception:
                log.exception("Failed to resolve relay editor channel %s", int(channel_snowflake))
                return None
        if isinstance(channel, hikari.TextableChannel):
            DC_Relay._channel_objects[channel_snowflake] = channel
        return channel

    @classmethod
    async def _resolve_channel_guild_id(
        cls,
        *,
        bot: hikari.GatewayBot,
        channel_id: hikari.Snowflakeish,
    ) -> hikari.Snowflake | None:
        channel = await cls._resolve_channel(bot=bot, channel_id=channel_id)
        guild_id = getattr(channel, "guild_id", None)
        if guild_id is None:
            return None
        return hikari.Snowflake(guild_id)

    @classmethod
    async def _configured_relay_channel_for_guild(
        cls,
        *,
        bot: hikari.GatewayBot,
        channel_ids: Sequence[hikari.Snowflakeish],
        guild_id: hikari.Snowflakeish,
    ) -> hikari.Snowflake | None:
        target_guild_id = hikari.Snowflake(guild_id)
        for channel_id in channel_ids:
            channel_guild_id = await cls._resolve_channel_guild_id(bot=bot, channel_id=channel_id)
            if channel_guild_id == target_guild_id:
                return hikari.Snowflake(channel_id)
        return None

    @classmethod
    async def _configured_app_relay_channel_for_guild(
        cls,
        *,
        bot: hikari.GatewayBot,
        app: App,
        guild_id: hikari.Snowflakeish,
    ) -> hikari.Snowflake | None:
        return await cls._configured_relay_channel_for_guild(
            bot=bot,
            channel_ids=app.chat_channels,
            guild_id=guild_id,
        )

    @classmethod
    async def _configured_app_relay_override_channel_for_guild(
        cls,
        *,
        bot: hikari.GatewayBot,
        app: App,
        guild_id: hikari.Snowflakeish,
    ) -> hikari.Snowflake | None:
        return await cls._configured_relay_channel_for_guild(
            bot=bot,
            channel_ids=_app_override_channels(app),
            guild_id=guild_id,
        )

    @classmethod
    async def _configured_default_relay_channel_for_guild(
        cls,
        *,
        bot: hikari.GatewayBot,
        manager: App_Manager,
        guild_id: hikari.Snowflakeish,
    ) -> hikari.Snowflake | None:
        return await cls._configured_relay_channel_for_guild(
            bot=bot,
            channel_ids=_manager_default_chat_channels(manager),
            guild_id=guild_id,
        )

    @classmethod
    async def _next_relay_channels_for_guild(
        cls,
        *,
        bot: hikari.GatewayBot,
        channel_ids: Sequence[hikari.Snowflakeish],
        guild_id: hikari.Snowflakeish,
        selected_channel_id: hikari.Snowflakeish | None,
    ) -> tuple[hikari.Snowflake, ...]:
        target_guild_id = hikari.Snowflake(guild_id)
        next_channels: list[hikari.Snowflake] = []
        seen_channels: set[int] = set()

        for channel_id in channel_ids:
            existing_channel_id = hikari.Snowflake(channel_id)
            channel_guild_id = await cls._resolve_channel_guild_id(bot=bot, channel_id=existing_channel_id)
            if channel_guild_id == target_guild_id:
                continue
            channel_key = int(existing_channel_id)
            if channel_key in seen_channels:
                continue
            next_channels.append(existing_channel_id)
            seen_channels.add(channel_key)

        if selected_channel_id is not None:
            resolved_channel_id = hikari.Snowflake(selected_channel_id)
            channel_key = int(resolved_channel_id)
            if channel_key not in seen_channels:
                next_channels.append(resolved_channel_id)

        return tuple(next_channels)

    @classmethod
    async def _next_app_relay_channels_for_guild(
        cls,
        *,
        bot: hikari.GatewayBot,
        app: App,
        guild_id: hikari.Snowflakeish,
        selected_channel_id: hikari.Snowflakeish | None,
    ) -> tuple[hikari.Snowflake, ...]:
        return await cls._next_relay_channels_for_guild(
            bot=bot,
            channel_ids=_app_override_channels(app),
            guild_id=guild_id,
            selected_channel_id=selected_channel_id,
        )

    @classmethod
    async def _next_default_relay_channels_for_guild(
        cls,
        *,
        bot: hikari.GatewayBot,
        manager: App_Manager,
        guild_id: hikari.Snowflakeish,
        selected_channel_id: hikari.Snowflakeish | None,
    ) -> tuple[hikari.Snowflake, ...]:
        return await cls._next_relay_channels_for_guild(
            bot=bot,
            channel_ids=_manager_default_chat_channels(manager),
            guild_id=guild_id,
            selected_channel_id=selected_channel_id,
        )

    def _sync_voice_target_to_relay_channel(
        self,
        *,
        guild_id: hikari.Snowflakeish,
        relay_text_channel_id: hikari.Snowflakeish,
        voice_channel_id: hikari.Snowflakeish | None = None,
        expected_primary_tts_channel: hikari.Snowflakeish | None = None,
        require_existing_relay_tts: bool = False,
    ) -> None:
        voice_target_service = self._voice_target_service
        if voice_target_service is None:
            return

        current_target = voice_target_service.voice_target(guild_id)
        if current_target is None and voice_channel_id is None:
            return

        existing_target = current_target
        if existing_target is not None:
            if expected_primary_tts_channel is not None and existing_target.primary_tts_channel != hikari.Snowflake(
                expected_primary_tts_channel
            ):
                return
            if require_existing_relay_tts and not existing_target.relay_tts_enabled:
                return
        if voice_channel_id is not None:
            resolved_voice_channel_id = hikari.Snowflake(voice_channel_id)
        else:
            if existing_target is None:
                return
            resolved_voice_channel_id = existing_target.voice_channel
        secondary_tts_channel = existing_target.secondary_tts_channel if existing_target is not None else None
        secondary_tts_listen_enabled = (
            existing_target.secondary_tts_listen_enabled if existing_target is not None else None
        )
        relay_text_channel = hikari.Snowflake(relay_text_channel_id)
        if secondary_tts_channel == relay_text_channel:
            secondary_tts_channel = None
            secondary_tts_listen_enabled = False

        voice_target_service.set_voice_target_config(
            guild_id,
            voice_channel=resolved_voice_channel_id,
            primary_tts_channel=relay_text_channel,
            primary_tts_listen_enabled=existing_target.primary_tts_listen_enabled
            if existing_target is not None
            else True,
            secondary_tts_channel=secondary_tts_channel,
            secondary_tts_listen_enabled=secondary_tts_listen_enabled,
            relay_tts_enabled=(
                True
                if voice_channel_id is not None
                else existing_target.relay_tts_enabled
                if existing_target is not None
                else False
            ),
        )

    async def _open_create_modal(
        self,
        *,
        interaction: hikari.ComponentInteraction,
        actor_user_id: int,
        state: AppManageState,
    ) -> None:
        if state.app_name is None:
            raise ValueError("Create modal requires a selected app scope.")
        modal = self._create_satisfactory_modal if state.app_name == "satisfactory" else self._create_modal
        values = {
            _APP_CREATE_INSTANCE_KEY_FIELD_ID: "",
            _APP_CREATE_FRIENDLY_NAME_FIELD_ID: "",
            _APP_CREATE_SUBFOLDER_FIELD_ID: "",
            _APP_CREATE_PORT_FIELD_ID: "",
            _APP_CREATE_SERVER_LOG_FILE_FIELD_ID: "",
        }
        if state.app_name == "satisfactory":
            values[_APP_CREATE_ADMIN_PASSWORD_FIELD_ID] = ""
        await interaction.create_modal_response(
            title=f"Create {state.app_name} Instance",
            custom_id=modal.build_id(
                self._build_state_action(AppManageActionKind.CREATE_INSTANCE, state),
                scope_id=actor_user_id,
                user_id=actor_user_id,
            ),
            components=modal.rows(values),
        )

    def _now(self) -> datetime:
        return self._ensure_aware_datetime(self._editor.clock())

    def _lock_deadline(self, now: datetime) -> datetime:
        timeout = self._editor.timeout
        if timeout is None:
            return now + timedelta(days=3650)
        return now + timeout

    def _prune_app_locks(self, *, now: datetime | None = None) -> None:
        current_time = self._now() if now is None else now
        stale_message_ids = [
            message_id
            for message_id in self._app_locks
            if self._editor.session_store.get_session_deadline(message_id, now=current_time) is None
        ]
        for message_id in stale_message_ids:
            self._app_locks.pop(message_id, None)

    def _touch_app_lock(
        self,
        *,
        message_id: hikari.Snowflake,
        user_id: hikari.Snowflakeish,
        app_name: str,
        channel_id: hikari.Snowflakeish | None = None,
        guild_id: hikari.Snowflakeish | None = None,
        application_id: hikari.Snowflakeish | None = None,
        interaction_token: str | None = None,
        response_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        current_time = self._now() if now is None else now
        existing_lock = self._app_locks.get(message_id)
        self._editor.session_store.set_session_deadline(message_id, self._lock_deadline(current_time))
        self._app_locks[message_id] = AppManagementLock(
            message_id=message_id,
            user_id=hikari.Snowflake(user_id),
            app_name=app_name,
            channel_id=(
                hikari.Snowflake(channel_id)
                if channel_id is not None
                else existing_lock.channel_id
                if existing_lock is not None
                else None
            ),
            guild_id=(
                hikari.Snowflake(guild_id)
                if guild_id is not None
                else existing_lock.guild_id
                if existing_lock is not None
                else None
            ),
            application_id=(
                hikari.Snowflake(application_id)
                if application_id is not None
                else existing_lock.application_id
                if existing_lock is not None
                else None
            ),
            interaction_token=(
                interaction_token
                if interaction_token is not None
                else existing_lock.interaction_token
                if existing_lock is not None
                else None
            ),
            response_expires_at=(
                self._ensure_aware_datetime(response_expires_at)
                if response_expires_at is not None
                else existing_lock.response_expires_at
                if existing_lock is not None
                else None
            ),
        )

    def _release_app_lock(self, *, message_id: hikari.Snowflakeish) -> None:
        lock_message_id = hikari.Snowflake(message_id)
        self._app_locks.pop(lock_message_id, None)
        self._editor.session_store.clear_session_deadline(lock_message_id)

    def _find_app_lock(
        self,
        *,
        app_name: str,
        exclude_message_id: hikari.Snowflake | None = None,
        now: datetime | None = None,
    ) -> AppManagementLock | None:
        current_time = self._now() if now is None else now
        self._prune_app_locks(now=current_time)
        for lock in self._app_locks.values():
            if lock.app_name != app_name:
                continue
            if exclude_message_id is not None and lock.message_id == exclude_message_id:
                continue
            return lock
        return None

    def start_lock_reason(self, app: App) -> str | None:
        return None

    def start_lock(self, app: App) -> AppManagementLock | None:
        return self._find_app_lock(app_name=app.name)

    def build_start_lock_response(
        self,
        *,
        actor_user_id: int,
        locale: hikari.Locale,
        app: App,
        lock: AppManagementLock,
    ) -> tuple[str, list[hikari.api.MessageActionRowBuilder], bool]:
        can_force_close = lock.can_force_close(now=self._now())
        lines = [
            f"{app.friendly} is currently being managed and cannot be started.",
            f"Manager: <@{int(lock.user_id)}>",
            f"Location: {lock.location_text()}",
        ]
        editor_ctx = self._lock_editor.context(
            scope_id=actor_user_id,
            user_id=actor_user_id,
            locale=locale,
        )
        layout = EditorLayout(editor_ctx)
        layout.add_buttons(
            EditorButton(
                self._build_lock_action(
                    AppManageLockActionKind.FORCE_INVALIDATE,
                    lock.message_id,
                ),
                "Force Invalidate",
                style=hikari.ButtonStyle.DANGER,
            )
        )
        return "\n".join(lines), layout.build(), can_force_close

    def manage_lock_reason(
        self,
        app: App,
        *,
        message_id: hikari.Snowflake | None = None,
    ) -> str | None:
        return None

    async def open_editor(
        self,
        *,
        ctx: lightbulb.Context,
        acl: Access_Control,
        manager: App_Manager,
        initial_app: App | None = None,
        initial_mode: AppManageMode = AppManageMode.HOME,
        status: EditorStatus | str | None = None,
    ) -> None:
        resolved_status = _coerce_status(status)
        if initial_app is None:
            state = AppManageState(mode=AppManageMode.LANDING, page=0)
            if resolved_status is None:
                resolved_status = EditorStatus(text="Choose an app below.")
        else:
            state = AppManageState(mode=initial_mode, app_name=initial_app.name, page=0)
            if resolved_status is None:
                if initial_mode is AppManageMode.MODS:
                    resolved_status = EditorStatus(text=f"Opened mods for {initial_app.friendly}.")
                elif initial_mode is AppManageMode.SETTINGS:
                    resolved_status = EditorStatus(text=f"Opened settings for {initial_app.friendly}.")
                else:
                    resolved_status = EditorStatus(text=f"Opened {initial_app.friendly}.")
        status_text = _status_text(resolved_status)
        assert status_text is not None
        locale = self._editor.resolve_locale(ctx.interaction)
        embed, components = self._render_editor(
            actor_user_id=int(ctx.user.id),
            locale=locale,
            acl=acl,
            manager=manager,
            state=state,
            status=resolved_status,
            current_guild_id=getattr(ctx.interaction, "guild_id", None),
        )
        if embed is None:
            response = await ctx.respond(
                status_text,
                components=components,
                flags=hikari.MessageFlag.EPHEMERAL,
            )
        else:
            response = await ctx.respond(
                status_text,
                embed=embed,
                components=components,
                flags=hikari.MessageFlag.EPHEMERAL,
            )
        if initial_app is None:
            return
        message_id = self._response_message_id(response)
        if message_id is None:
            try:
                message_id = hikari.Snowflake((await ctx.interaction.fetch_initial_response()).id)
            except Exception:
                log.exception("App.Manage.OpenLock")
                return
        self._touch_app_lock(
            message_id=message_id,
            user_id=ctx.user.id,
            app_name=initial_app.name,
            channel_id=getattr(ctx.interaction, "channel_id", None),
            guild_id=getattr(ctx.interaction, "guild_id", None),
            application_id=ctx.interaction.application_id,
            interaction_token=ctx.interaction.token,
            response_expires_at=self._now() + _INTERACTION_RESPONSE_TTL,
        )

    async def route_component(
        self,
        interaction: hikari.ComponentInteraction,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> bool:
        self._prune_app_locks()
        return await self._lock_editor.route(
            interaction, bot=bot, acl=acl, manager=manager
        ) or await self._editor.route(interaction, bot=bot, acl=acl, manager=manager)

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> bool:
        self._prune_app_locks()
        if await self._setting_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_setting_modal_submit,
            unauthorised_message="You are not authorised to use this app settings editor.",
            invalid_message="App setting input is invalid.",
            bot=bot,
            acl=acl,
            manager=manager,
        ):
            return True
        return await self._create_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_create_modal_submit,
            unauthorised_message="Sudo access is required to create app instances.",
            invalid_message="App instance input is invalid.",
            bot=bot,
            acl=acl,
            manager=manager,
        ) or await self._create_satisfactory_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_create_modal_submit,
            unauthorised_message="Sudo access is required to create app instances.",
            invalid_message="App instance input is invalid.",
            bot=bot,
            acl=acl,
            manager=manager,
        )

    async def route_message(
        self,
        message: hikari.Message,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> bool:
        self._prune_app_locks()
        upload = self._editor.consume_file_upload(message)
        if upload is None:
            return False
        await self._consume_mod_upload(upload=upload, bot=bot, acl=acl, manager=manager)
        return True

    def _clear_pending_upload_request(
        self,
        *,
        channel_id: hikari.Snowflakeish | None,
        user_id: hikari.Snowflakeish,
    ) -> None:
        if channel_id is None:
            return
        self._editor.cancel_file_request(channel_id=channel_id, user_id=user_id)

    async def _authorise_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        acl = self._require_acl(deps)
        manager = self._require_manager(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return False

        required_level = APP_MANAGE_ACTION_LEVELS.get(action.kind)
        if required_level is None:
            return False
        if action.kind is AppManageActionKind.SAVE_SETTINGS:
            state = _state_from_value(action.value, action.page)
            if state is None or state.app_name is None:
                return False
            try:
                app = manager.get(state.app_name)
            except ValueError:
                return False
            required_level = app.settings_save_level(int(req.user_id))
        return acl.can(int(req.user_id), required_level)

    async def _authorise_setting_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        acl = self._require_acl(deps)
        return acl.can(int(req.user_id), Access_Control.LvL.user)

    async def _authorise_lock_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        acl = self._require_acl(deps)
        action = self._lock_action_codec.parse(req.action)
        return (
            action is not None
            and action.kind is AppManageLockActionKind.FORCE_INVALIDATE
            and acl.can(int(req.user_id), Access_Control.LvL.sudo)
        )

    async def _authorise_create_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        acl = self._require_acl(deps)
        return acl.can(int(req.user_id), APP_MANAGE_ACTION_LEVELS[AppManageActionKind.CREATE_INSTANCE])

    async def _on_lock_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse:
        bot = self._require_bot(deps)
        action = self._lock_action_codec.parse(req.action)
        if action is None or action.kind is not AppManageLockActionKind.FORCE_INVALIDATE:
            return EditorResponse.ephemeral("Unknown app manager lock action.")
        if action.value is None or not action.value.isdecimal():
            return EditorResponse.ephemeral("App manager lock is invalid.")
        lock_message_id = hikari.Snowflake(action.value)
        lock = self._app_locks.get(lock_message_id)
        if lock is None:
            return EditorResponse.update("That app manager lock is no longer active.", components=[])

        closed = await self._force_invalidate_lock(
            bot=bot,
            lock=lock,
            actor_user_id=int(req.user_id),
        )
        status = (
            f"Force-invalidated the manager for `{lock.app_name}`. The old manager message was closed."
            if closed
            else f"Force-invalidated the manager for `{lock.app_name}`. The old manager message could not be closed."
        )
        return EditorResponse.update(status, components=[])

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        manager = self._require_manager(deps)
        acl = self._require_acl(deps)
        bot = self._require_bot(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown app manager action.")

        state = _state_from_value(action.value, action.page)
        if state is None:
            return EditorResponse.ephemeral("App manager state is invalid.")

        actor_user_id = int(req.user_id)
        message_id = self._interaction_message_id(req.interaction)
        current_guild_id = self._interaction_guild_id(req.interaction)
        if action.kind is AppManageActionKind.CLOSE:
            self._clear_pending_upload_request(
                channel_id=getattr(req.interaction, "channel_id", None), user_id=req.user_id
            )
            if message_id is not None:
                self._release_app_lock(message_id=message_id)
            return EditorResponse.close("App manager closed.")
        if action.kind is AppManageActionKind.PAGE:
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status="Page updated.",
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.REFRESH:
            if state.is_mods and state.app_name is not None:
                try:
                    app = manager.get(state.app_name)
                except ValueError as xcp:
                    return EditorResponse.ephemeral(str(xcp))
                if message_id is not None:
                    self._touch_app_lock(message_id=message_id, user_id=req.user_id, app_name=app.name)
                reason = self.manage_lock_reason(app, message_id=message_id)
                if reason is not None:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=AppManageState(mode=AppManageMode.MODS, page=state.page, app_name=app.name),
                        status=_error_status(f"Error: {reason}"),
                    )
                if app.mods is None:
                    return EditorResponse.ephemeral(f"{app.friendly} does not support mods.")
                try:
                    await refresh_mod_index(app.has_mod_manager)
                except Exception as xcp:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=AppManageState(mode=AppManageMode.MODS, page=state.page, app_name=app.name),
                        status=_error_status(f"Error: mod refresh failed for `{app.friendly}`: {xcp}"),
                    )
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=AppManageState(mode=AppManageMode.MODS, page=state.page, app_name=app.name),
                    status=f"{app.friendly} mod index refreshed.",
                )
            if (state.is_settings or state.is_setting_choices) and state.app_name is not None:
                try:
                    app = manager.get(state.app_name)
                except ValueError as xcp:
                    return EditorResponse.ephemeral(str(xcp))
                if app.settings is None:
                    return EditorResponse.ephemeral(f"{app.friendly} does not support settings.")
                try:
                    app.settings.load(actor_user_id)
                except Exception as xcp:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=state,
                        status=_error_status(f"Error: settings reload failed for `{app.friendly}`: {xcp}"),
                    )

                next_state = state
                if state.is_setting_choices:
                    selected_setting = self._selected_setting(app=app, state=state)
                    if selected_setting is None:
                        next_state = AppManageState(mode=AppManageMode.SETTINGS, page=0, app_name=app.name)
                    else:
                        next_state = self._state_for_setting(
                            app=app,
                            setting=selected_setting,
                            mode=AppManageMode.SETTING_CHOICES,
                        )
                response = self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=next_state,
                    status=f"{app.friendly} settings reloaded from disk.",
                    current_guild_id=current_guild_id,
                )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status="App manager refreshed.",
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.OPEN_CREATE:
            if state.mode is not AppManageMode.LANDING:
                return EditorResponse.ephemeral("App creation is only available from the landing page.")
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.CREATE, page=0),
                status="Opened app instance creator.",
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.SELECT_CREATE_SCOPE:
            if not state.is_create:
                return EditorResponse.ephemeral("App creation is not available here.")
            if not req.values:
                return EditorResponse.ephemeral("Choose an app scope first.")
            selected_scope = req.values[0]
            available_scopes = manager.list_create_scopes()
            if selected_scope not in available_scopes:
                return EditorResponse.ephemeral("Unknown app scope.")
            await self._open_create_modal(
                interaction=req.interaction,
                actor_user_id=actor_user_id,
                state=AppManageState(mode=AppManageMode.CREATE, page=0, app_name=selected_scope),
            )
            return None
        if action.kind is AppManageActionKind.OPEN_CREATE_MODAL:
            if not state.is_create:
                return EditorResponse.ephemeral("App creation is not available here.")
            if state.app_name is None:
                return EditorResponse.ephemeral("Choose an app scope first.")
            await self._open_create_modal(
                interaction=req.interaction,
                actor_user_id=actor_user_id,
                state=state,
            )
            return None
        if action.kind is AppManageActionKind.OPEN_APP:
            if state.mode is not AppManageMode.LANDING:
                return EditorResponse.ephemeral("This editor is already locked to an app.")
            if not req.values:
                return EditorResponse.ephemeral("Choose an app first.")
            try:
                target_app = manager.get(req.values[0])
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            reason = self.manage_lock_reason(target_app, message_id=message_id)
            if reason is not None:
                return EditorResponse.ephemeral(reason)
            if message_id is None:
                return EditorResponse.ephemeral("App manager message is unavailable.")
            self._touch_app_lock(message_id=message_id, user_id=req.user_id, app_name=target_app.name)
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(
                    mode=AppManageMode.HOME,
                    app_name=target_app.name,
                    page=state.page,
                ),
                status=f"Opened {target_app.friendly}.",
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.OPEN_RELAY:
            if state.mode not in {AppManageMode.LANDING, AppManageMode.HOME}:
                return EditorResponse.ephemeral("Relay management is not available here.")
            if state.app_name is not None:
                app = manager.get(state.app_name)
                if not app.supports_chat_relay:
                    return EditorResponse.ephemeral(f"{app.friendly} does not support chat relay.")
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.RELAY, page=state.page, app_name=state.app_name),
                status="Opened relay manager.",
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.SAVE_RELAY_CHANNEL:
            if not req.values:
                return EditorResponse.ephemeral("Choose a relay channel first.")
            if state.mode is AppManageMode.RELAY and state.app_name is None:
                channel_id = hikari.Snowflake(req.values[0])
                if current_guild_id is None:
                    return EditorResponse.ephemeral("Open this editor in a server to manage relay channels.")
                channel_guild_id = await self._resolve_channel_guild_id(bot=bot, channel_id=channel_id)
                if channel_guild_id != current_guild_id:
                    return EditorResponse.ephemeral("Choose a text channel from this server.")
                previous_channel_id = await self._configured_default_relay_channel_for_guild(
                    bot=bot,
                    manager=manager,
                    guild_id=current_guild_id,
                )
                channel_ids = await self._next_default_relay_channels_for_guild(
                    bot=bot,
                    manager=manager,
                    guild_id=current_guild_id,
                    selected_channel_id=channel_id,
                )
                manager.set_default_chat_channels(channel_ids)
                if previous_channel_id is not None:
                    self._sync_voice_target_to_relay_channel(
                        guild_id=current_guild_id,
                        relay_text_channel_id=channel_id,
                        expected_primary_tts_channel=previous_channel_id,
                        require_existing_relay_tts=True,
                    )
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=f"Default relay text channel for this guild set to <#{int(channel_id)}>.",
                    current_guild_id=current_guild_id,
                )
            if state.mode is not AppManageMode.RELAY or state.app_name is None:
                return EditorResponse.ephemeral("Relay management is not available here.")
            if current_guild_id is None:
                return EditorResponse.ephemeral("Open this editor in a server to manage relay channels.")
            app = manager.get(state.app_name)
            if not app.supports_chat_relay:
                return EditorResponse.ephemeral(f"{app.friendly} does not support chat relay.")
            channel_id = hikari.Snowflake(req.values[0])
            channel_guild_id = await self._resolve_channel_guild_id(bot=bot, channel_id=channel_id)
            if channel_guild_id != current_guild_id:
                return EditorResponse.ephemeral("Choose a text channel from this server.")
            previous_override_channel_id = await self._configured_app_relay_override_channel_for_guild(
                bot=bot,
                app=app,
                guild_id=current_guild_id,
            )
            default_channel_id = await self._configured_default_relay_channel_for_guild(
                bot=bot,
                manager=manager,
                guild_id=current_guild_id,
            )
            selected_override_channel_id = None if channel_id == default_channel_id else channel_id
            channel_ids = await self._next_app_relay_channels_for_guild(
                bot=bot,
                app=app,
                guild_id=current_guild_id,
                selected_channel_id=selected_override_channel_id,
            )
            manager.set_app_chat_channels(app, channel_ids)
            self._sync_voice_target_to_relay_channel(
                guild_id=current_guild_id,
                relay_text_channel_id=channel_id,
            )
            status_text = f"{app.friendly} relay text channel for this guild set to <#{int(channel_id)}>."
            if selected_override_channel_id is None:
                status_text = (
                    f"{app.friendly} already uses the default relay channel for this guild."
                    if previous_override_channel_id is None
                    else f"{app.friendly} relay override cleared for this guild. "
                    "The default relay channel still applies here."
                )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=status_text,
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.SAVE_RELAY_VOICE_CHANNEL:
            if state.mode is AppManageMode.RELAY and state.app_name is None:
                if current_guild_id is None:
                    return EditorResponse.ephemeral("Open this editor in a server to manage relay channels.")
                if not req.values:
                    return EditorResponse.ephemeral("Choose a voice channel first.")
                if self._voice_target_service is None:
                    return EditorResponse.ephemeral("Voice relay editing is unavailable on this node.")
                voice_channel_id = hikari.Snowflake(req.values[0])
                voice_channel_guild_id = await self._resolve_channel_guild_id(bot=bot, channel_id=voice_channel_id)
                if voice_channel_guild_id != current_guild_id:
                    return EditorResponse.ephemeral("Choose a voice channel from this server.")
                relay_text_channel_id = await self._configured_default_relay_channel_for_guild(
                    bot=bot,
                    manager=manager,
                    guild_id=current_guild_id,
                )
                if relay_text_channel_id is None:
                    return EditorResponse.ephemeral("Choose this server's default relay text channel first.")
                try:
                    self._sync_voice_target_to_relay_channel(
                        guild_id=current_guild_id,
                        relay_text_channel_id=relay_text_channel_id,
                        voice_channel_id=voice_channel_id,
                    )
                except (LookupError, ValueError) as xcp:
                    return EditorResponse.ephemeral(str(xcp))
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=f"Default relay voice channel for this guild set to <#{int(voice_channel_id)}>.",
                    current_guild_id=current_guild_id,
                )
            if state.mode is not AppManageMode.RELAY or state.app_name is None:
                return EditorResponse.ephemeral("Relay management is not available here.")
            if current_guild_id is None:
                return EditorResponse.ephemeral("Open this editor in a server to manage relay channels.")
            if not req.values:
                return EditorResponse.ephemeral("Choose a voice channel first.")
            if self._voice_target_service is None:
                return EditorResponse.ephemeral("Voice relay editing is unavailable on this node.")
            app = manager.get(state.app_name)
            if not app.supports_chat_relay:
                return EditorResponse.ephemeral(f"{app.friendly} does not support chat relay.")
            voice_channel_id = hikari.Snowflake(req.values[0])
            voice_channel_guild_id = await self._resolve_channel_guild_id(bot=bot, channel_id=voice_channel_id)
            if voice_channel_guild_id != current_guild_id:
                return EditorResponse.ephemeral("Choose a voice channel from this server.")
            relay_text_channel_id = await self._configured_app_relay_channel_for_guild(
                bot=bot,
                app=app,
                guild_id=current_guild_id,
            )
            if relay_text_channel_id is None:
                return EditorResponse.ephemeral("Choose this server's relay text channel first.")
            try:
                self._sync_voice_target_to_relay_channel(
                    guild_id=current_guild_id,
                    relay_text_channel_id=relay_text_channel_id,
                    voice_channel_id=voice_channel_id,
                )
            except (LookupError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=f"Relay voice channel for this guild set to <#{int(voice_channel_id)}>.",
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.CLEAR_RELAY_CHANNEL:
            if state.mode is AppManageMode.RELAY and state.app_name is None:
                if current_guild_id is None:
                    return EditorResponse.ephemeral("Open this editor in a server to manage relay channels.")
                removed_channel_id = await self._configured_default_relay_channel_for_guild(
                    bot=bot,
                    manager=manager,
                    guild_id=current_guild_id,
                )
                channel_ids = await self._next_default_relay_channels_for_guild(
                    bot=bot,
                    manager=manager,
                    guild_id=current_guild_id,
                    selected_channel_id=None,
                )
                manager.set_default_chat_channels(channel_ids)
                status_text = "Default relay text channel removed for this guild."
                if removed_channel_id is None:
                    status_text = "Default relay has no text channel configured for this guild."
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=status_text,
                    current_guild_id=current_guild_id,
                )
            if state.mode is not AppManageMode.RELAY or state.app_name is None:
                return EditorResponse.ephemeral("Relay management is not available here.")
            if current_guild_id is None:
                return EditorResponse.ephemeral("Open this editor in a server to manage relay channels.")
            app = manager.get(state.app_name)
            if not app.supports_chat_relay:
                return EditorResponse.ephemeral(f"{app.friendly} does not support chat relay.")
            removed_channel_id = await self._configured_app_relay_override_channel_for_guild(
                bot=bot,
                app=app,
                guild_id=current_guild_id,
            )
            channel_ids = await self._next_app_relay_channels_for_guild(
                bot=bot,
                app=app,
                guild_id=current_guild_id,
                selected_channel_id=None,
            )
            manager.set_app_chat_channels(app, channel_ids)
            status_text = f"{app.friendly} relay text channel removed for this guild."
            default_channel_id = await self._configured_default_relay_channel_for_guild(
                bot=bot,
                manager=manager,
                guild_id=current_guild_id,
            )
            if removed_channel_id is None:
                status_text = (
                    f"{app.friendly} has no relay override configured for this guild. "
                    "The default relay channel still applies here."
                    if default_channel_id is not None
                    else f"{app.friendly} has no relay text channel configured for this guild."
                )
            elif not channel_ids and default_channel_id is not None:
                status_text = (
                    f"{app.friendly} relay override cleared for this guild. "
                    "The default relay channel still applies here."
                )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=status_text,
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.TOGGLE_RELAY_ADVANCEMENTS:
            if state.mode is not AppManageMode.RELAY or state.app_name is None:
                return EditorResponse.ephemeral("Relay management is not available here.")
            app = manager.get(state.app_name)
            if not app.supports_relay_advancements:
                return EditorResponse.ephemeral(
                    f"{app.friendly} does not support {app.relay_advancement_term.lower()} relay."
                )
            next_enabled = not bool(app.relay_advancements_enabled)
            manager.set_app_relay_advancements_enabled(app, next_enabled)
            status_text = (
                f"{app.friendly} {app.relay_advancement_term.lower()} relay enabled."
                if next_enabled
                else f"{app.friendly} {app.relay_advancement_term.lower()} relay disabled."
            )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=status_text,
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.BACK_LANDING:
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.LANDING, page=state.page),
                status="Returned to app list.",
                current_guild_id=current_guild_id,
            )

        if state.app_name is None:
            return EditorResponse.ephemeral("Choose an app first.")

        try:
            app = manager.get(state.app_name)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        if message_id is not None:
            self._touch_app_lock(message_id=message_id, user_id=req.user_id, app_name=app.name)

        if action.kind is AppManageActionKind.BACK_HOME:
            self._clear_pending_upload_request(
                channel_id=getattr(req.interaction, "channel_id", None), user_id=req.user_id
            )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.HOME, page=state.page, app_name=app.name),
                status=f"Returned to {app.friendly}.",
                current_guild_id=current_guild_id,
            )
        if action.kind is AppManageActionKind.BACK_SETTINGS:
            if app.settings is None:
                return EditorResponse.ephemeral(f"{app.friendly} does not support settings.")
            selected_setting = self._selected_setting(app=app, state=state)
            next_state = (
                self._state_for_setting(app=app, setting=selected_setting)
                if selected_setting is not None
                else AppManageState(mode=AppManageMode.SETTINGS, page=0, app_name=app.name)
            )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=next_state,
                status=f"Returned to settings for {app.friendly}.",
            )
        if action.kind is AppManageActionKind.OPEN_MODS:
            if app.mods is None:
                return EditorResponse.ephemeral(f"{app.friendly} does not support mods.")
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.MODS, page=0, app_name=app.name),
                status=f"Opened mods for {app.friendly}.",
            )
        if action.kind is AppManageActionKind.OPEN_SETTINGS:
            if app.settings is None:
                return EditorResponse.ephemeral(f"{app.friendly} does not support settings.")
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.SETTINGS, page=0, app_name=app.name),
                status=f"Opened settings for {app.friendly}.",
            )

        if state.is_home:
            if action.kind is AppManageActionKind.TOGGLE_APP:
                reason = self.manage_lock_reason(app, message_id=message_id)
                if reason is not None:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=state,
                        status=_error_status(f"Error: {reason}"),
                    )
                next_state = not app.cfg.enabled
                manager.toggle(app.name, next_state)
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=f"{app.friendly}: {'Enabled' if next_state else 'Disabled'}.",
                )
            if action.kind is AppManageActionKind.DOWNLOAD_APP:
                await self._handle_app_download_action(req=req, acl=acl, manager=manager, state=state, app=app)
                return None
            if action.kind is AppManageActionKind.UPDATE_APP:
                await self._handle_app_update_action(req=req, acl=acl, manager=manager, state=state, app=app)
                return None
            return EditorResponse.ephemeral("Unsupported app manager action.")

        if state.is_settings or state.is_setting_choices:
            if app.settings is None:
                return EditorResponse.ephemeral(f"{app.friendly} does not support settings.")
            settings_view = self._build_settings_view(app=app, state=state, acl=acl, actor_user_id=actor_user_id)
            selected_setting = settings_view.selected_setting
            if selected_setting is None:
                selected_setting = self._selected_setting(app=app, state=state)
            selected_state = (
                self._state_for_setting(app=app, setting=selected_setting)
                if selected_setting is not None
                else AppManageState(
                    mode=AppManageMode.SETTINGS, page=settings_view.settings.page_state.page, app_name=app.name
                )
            )

            if action.kind is AppManageActionKind.SELECT_SETTING:
                if not req.values:
                    return EditorResponse.ephemeral("Choose a setting first.")
                selected_setting = next(
                    (setting for setting in settings_view.settings.visible if setting.key == req.values[0]),
                    None,
                )
                if selected_setting is None:
                    return EditorResponse.ephemeral("Choose a setting from the current page.")
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=self._state_for_setting(
                        app=app,
                        setting=selected_setting,
                        page=settings_view.settings.page_state.page,
                    ),
                    status=f"Selected {selected_setting.label}.",
                )

            if action.kind is AppManageActionKind.UPDATE_SETTING:
                if selected_setting is None:
                    return EditorResponse.ephemeral("Choose a setting first.")
                if not _setting_can_edit(selected_setting, acl=acl, actor_user_id=actor_user_id):
                    return EditorResponse.ephemeral(
                        f"Editing `{selected_setting.label}` requires {selected_setting.power_level.name.title()} access."
                    )
                if not req.values:
                    return EditorResponse.ephemeral("Choose a value first.")
                try:
                    app.settings.update_setting(actor_user_id, selected_setting, req.values[0], remember_input=True)
                except Exception as xcp:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=(
                            self._state_for_setting(
                                app=app,
                                setting=selected_setting,
                                mode=AppManageMode.SETTING_CHOICES
                                if state.is_setting_choices
                                else AppManageMode.SETTINGS,
                                page=state.page if state.is_setting_choices else selected_state.page,
                            )
                        ),
                        status=_error_status(f"Error: setting update failed for `{selected_setting.label}`: {xcp}"),
                    )
                next_state = self._state_for_setting(
                    app=app,
                    setting=selected_setting,
                    mode=AppManageMode.SETTING_CHOICES if state.is_setting_choices else AppManageMode.SETTINGS,
                    page=state.page if state.is_setting_choices else None,
                )
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=next_state,
                    status=(
                        f"{app.friendly} setting `{selected_setting.label}` updated: "
                        f"{_setting_display_value(selected_setting, settings_manager=app.settings, actor_user_id=actor_user_id)}. "
                        "Settings are saved on launch or via Save Settings."
                    ),
                )

            if action.kind is AppManageActionKind.REUSE_SETTING:
                if selected_setting is None:
                    return EditorResponse.ephemeral("Choose a setting first.")
                if not _setting_can_edit(selected_setting, acl=acl, actor_user_id=actor_user_id):
                    return EditorResponse.ephemeral(
                        f"Editing `{selected_setting.label}` requires {selected_setting.power_level.name.title()} access."
                    )
                if not req.values:
                    return EditorResponse.ephemeral("Choose a recent value first.")
                recent_value = _setting_recent_value_at(selected_setting, req.values[0])
                if recent_value is None:
                    return EditorResponse.ephemeral("Choose a recent value from the current list.")
                try:
                    app.settings.update_setting(actor_user_id, selected_setting, recent_value, remember_input=True)
                except Exception as xcp:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=selected_state,
                        status=_error_status(f"Error: setting update failed for `{selected_setting.label}`: {xcp}"),
                    )
                response = self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=selected_state,
                    status=(
                        f"{app.friendly} setting `{selected_setting.label}` updated: "
                        f"{_setting_display_value(selected_setting, settings_manager=app.settings, actor_user_id=actor_user_id)}. "
                        "Settings are saved on launch or via Save Settings."
                    ),
                )
                await _send_public_action_notice(
                    bot,
                    req.interaction,
                    _public_setting_update_text(
                        actor_user_id=actor_user_id,
                        app=app,
                        setting=selected_setting,
                    ),
                )
                return response

            if action.kind is AppManageActionKind.OPEN_SETTING_CHOICES:
                if selected_setting is None:
                    return EditorResponse.ephemeral("Choose a setting first.")
                if not _setting_can_edit(selected_setting, acl=acl, actor_user_id=actor_user_id):
                    return EditorResponse.ephemeral(
                        f"Editing `{selected_setting.label}` requires {selected_setting.power_level.name.title()} access."
                    )
                if not _setting_requires_choice_browser(selected_setting):
                    return EditorResponse.ephemeral("This setting does not need a dedicated choice browser.")
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=self._state_for_setting(
                        app=app,
                        setting=selected_setting,
                        mode=AppManageMode.SETTING_CHOICES,
                        actor_user_id=actor_user_id,
                    ),
                    status=f"Browsing values for {selected_setting.label}.",
                )

            if action.kind is AppManageActionKind.WRITE_SETTING:
                if selected_setting is None:
                    return EditorResponse.ephemeral("Choose a setting first.")
                if not _setting_can_edit(selected_setting, acl=acl, actor_user_id=actor_user_id):
                    return EditorResponse.ephemeral(
                        f"Editing `{selected_setting.label}` requires {selected_setting.power_level.name.title()} access."
                    )
                if not _setting_allows_modal_entry(selected_setting):
                    return EditorResponse.ephemeral("This setting must be changed using its available choices.")
                await req.interaction.create_modal_response(
                    f"Set {selected_setting.label}",
                    self._setting_modal.build_id(
                        self._build_state_action(AppManageActionKind.UPDATE_SETTING, selected_state),
                        scope_id=actor_user_id,
                        user_id=actor_user_id,
                    ),
                    components=self._setting_modal.rows(
                        {
                            _APP_SETTING_VALUE_FIELD_ID: _setting_current_input_value(
                                selected_setting,
                                settings_manager=app.settings,
                                actor_user_id=actor_user_id,
                            ),
                        }
                    ),
                )
                return None

            if action.kind is AppManageActionKind.SAVE_SETTINGS:
                required_level = app.settings_save_level(actor_user_id)
                if not acl.can(actor_user_id, required_level):
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=(
                            self._state_for_setting(
                                app=app,
                                setting=selected_setting,
                                mode=AppManageMode.SETTING_CHOICES
                                if state.is_setting_choices
                                else AppManageMode.SETTINGS,
                                page=state.page if state.is_setting_choices else None,
                            )
                            if selected_setting is not None
                            else selected_state
                        ),
                        status=(
                            f"Error: saving settings for `{app.friendly}` requires "
                            f"{required_level.name.title()} access."
                        ),
                    )
                try:
                    app.settings.save(actor_user_id)
                except Exception as xcp:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=(
                            self._state_for_setting(
                                app=app,
                                setting=selected_setting,
                                mode=AppManageMode.SETTING_CHOICES
                                if state.is_setting_choices
                                else AppManageMode.SETTINGS,
                                page=state.page if state.is_setting_choices else None,
                            )
                            if selected_setting is not None
                            else selected_state
                        ),
                        status=_error_status(f"Error: settings save failed for `{app.friendly}`: {xcp}"),
                    )
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=(
                        self._state_for_setting(
                            app=app,
                            setting=selected_setting,
                            mode=AppManageMode.SETTING_CHOICES if state.is_setting_choices else AppManageMode.SETTINGS,
                            page=state.page if state.is_setting_choices else None,
                        )
                        if selected_setting is not None
                        else selected_state
                    ),
                    status=f"Saved settings for {app.friendly}.",
                )

            return EditorResponse.ephemeral("Unsupported app manager action.")

        if not state.is_mods:
            return EditorResponse.ephemeral("Unsupported app manager state.")
        if app.mods is None:
            return EditorResponse.ephemeral(f"{app.friendly} does not support mods.")

        mods_view = self._build_mods_view(app=app, state=state)
        if action.kind is AppManageActionKind.SELECT_MOD:
            if not req.values:
                return EditorResponse.ephemeral("Choose a mod first.")
            selected_mod_slot = next(
                (index for index, mod in enumerate(mods_view.mods.visible) if mod.name == req.values[0]),
                None,
            )
            if selected_mod_slot is None:
                return EditorResponse.ephemeral("Choose a mod from the current page.")
            selected_mod = mods_view.mods.visible[selected_mod_slot]
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(
                    mode=AppManageMode.MODS,
                    page=mods_view.mods.page_state.page,
                    app_name=app.name,
                    selected_page_slot=selected_mod_slot,
                ),
                status=f"Selected {selected_mod.friendly}.",
            )
        if action.kind is AppManageActionKind.DOWNLOAD_MOD:
            await self._handle_mod_download_action(
                req=req,
                acl=acl,
                manager=manager,
                state=state,
                app=app,
                selected_mod=mods_view.selected_mod,
            )
            return None
        if action.kind is AppManageActionKind.OPEN_MOD_WEB:
            await self._handle_mod_web_action(
                req=req,
                acl=acl,
                manager=manager,
                state=state,
                app=app,
            )
            return None
        if action.kind is AppManageActionKind.REQUEST_MOD_UPLOAD:
            return self._handle_mod_upload_request_action(
                req=req,
                acl=acl,
                manager=manager,
                state=state,
                app=app,
            )
        if action.kind is AppManageActionKind.TOGGLE_MOD:
            if mods_view.selected_mod is None:
                return EditorResponse.ephemeral("Choose a mod first.")
            reason = self.manage_lock_reason(app, message_id=message_id)
            if reason is not None:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=_error_status(f"Error: {reason}"),
                )
            try:
                require_app_stopped_for_mod_mutation(app)
                mod = await toggle_mod(
                    app.has_mod_manager,
                    mods_view.selected_mod.name,
                    acl=acl,
                    actor_user_id=actor_user_id,
                )
            except Exception as xcp:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=_error_status(f"Error: mod toggle failed for `{mods_view.selected_mod.friendly}`: {xcp}"),
                )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=f"{app.friendly} mod `{mod.friendly}`: {'Enabled' if mod.cfg.enabled else 'Disabled'}.",
            )
        if action.kind is AppManageActionKind.TOGGLE_COREMOD:
            if mods_view.selected_mod is None:
                return EditorResponse.ephemeral("Choose a mod first.")
            reason = self.manage_lock_reason(app, message_id=message_id)
            if reason is not None:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=_error_status(f"Error: {reason}"),
                )
            try:
                mod = await toggle_coremod(
                    app.has_mod_manager,
                    mods_view.selected_mod.name,
                    acl=acl,
                    actor_user_id=actor_user_id,
                )
            except Exception as xcp:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=_error_status(
                        f"Error: coremod update failed for `{mods_view.selected_mod.friendly}`: {xcp}"
                    ),
                )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=f"{app.friendly} mod `{mod.friendly}` coremod: {'Enabled' if mod.is_coremod_type else 'Disabled'}.",
            )
        if action.kind is AppManageActionKind.TOGGLE_DOWNLOADABLE:
            if mods_view.selected_mod is None:
                return EditorResponse.ephemeral("Choose a mod first.")
            reason = self.manage_lock_reason(app, message_id=message_id)
            if reason is not None:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=_error_status(f"Error: {reason}"),
                )
            try:
                mod = await toggle_downloadable(
                    app.has_mod_manager,
                    mods_view.selected_mod.name,
                    acl=acl,
                    actor_user_id=actor_user_id,
                )
            except Exception as xcp:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=_error_status(
                        f"Error: downloadability update failed for `{mods_view.selected_mod.friendly}`: {xcp}"
                    ),
                )
            status_value = (
                "Allowed" if mod.downloadable else f"Blocked ({mod.download_block_label or 'not downloadable'})"
            )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=f"{app.friendly} mod `{mod.friendly}` downloads: {status_value}.",
            )
        if action.kind is AppManageActionKind.REMOVE_MOD:
            if mods_view.selected_mod is None:
                return EditorResponse.ephemeral("Choose a mod first.")
            reason = self.manage_lock_reason(app, message_id=message_id)
            next_state = AppManageState(mode=AppManageMode.MODS, page=state.page, app_name=app.name)
            if reason is not None:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=next_state,
                    status=_error_status(f"Error: {reason}"),
                )
            try:
                require_app_stopped_for_mod_mutation(app)
                result = await remove_mods(
                    app.has_mod_manager,
                    (mods_view.selected_mod.name,),
                    acl=acl,
                    actor_user_id=actor_user_id,
                )
            except Exception as xcp:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=next_state,
                    status=_error_status(f"Error: remove failed for `{mods_view.selected_mod.friendly}`: {xcp}"),
                )
            if result.errors:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=next_state,
                    status=_error_status(
                        f"Error: remove failed for `{mods_view.selected_mod.friendly}`: {result.errors[0]}"
                    ),
                )
            removed_mod = result.successful[0]
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=next_state,
                status=f"Removed `{removed_mod.friendly}` from {app.friendly}.",
            )

        return EditorResponse.ephemeral("Unsupported app manager action.")

    async def _on_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        manager = self._require_manager(deps)
        acl = self._require_acl(deps)
        bot = self._require_bot(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown app manager modal action.")

        if action.kind is AppManageActionKind.CREATE_INSTANCE:
            state = _state_from_value(action.value, action.page)
            if state is None or not state.is_create:
                return EditorResponse.ephemeral("App manager state is invalid.")
            if state.app_name is None:
                return EditorResponse.ephemeral("App scope is missing.")
            locale = self._editor.resolve_locale(req.interaction)
            try:
                instance_name = manager.create_instance(
                    AppInstanceCreateRequest(
                        scope=state.app_name,
                        instance_key=req.values.get(_APP_CREATE_INSTANCE_KEY_FIELD_ID, ""),
                        friendly_name=req.values.get(_APP_CREATE_FRIENDLY_NAME_FIELD_ID, ""),
                        subfolder=req.values.get(_APP_CREATE_SUBFOLDER_FIELD_ID, ""),
                        port=_parse_optional_port(req.values.get(_APP_CREATE_PORT_FIELD_ID, "")),
                        server_log_file=req.values.get(_APP_CREATE_SERVER_LOG_FILE_FIELD_ID),
                        admin_password=req.values.get(_APP_CREATE_ADMIN_PASSWORD_FIELD_ID),
                    )
                )
            except Exception as xcp:
                return self._build_editor_response(
                    actor_user_id=int(req.user_id),
                    locale=locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=_error_status(f"Error: app instance creation failed: {xcp}"),
                )
            return EditorResponse.close(
                f"Created `{instance_name}`. Restart the bot after provisioning the app files to load it."
            )
        if action.kind is not AppManageActionKind.UPDATE_SETTING:
            return EditorResponse.ephemeral("Unsupported app manager modal action.")

        state = _state_from_value(action.value, action.page)
        if state is None or not state.is_settings or state.app_name is None:
            return EditorResponse.ephemeral("App manager state is invalid.")

        actor_user_id = int(req.user_id)
        try:
            app = manager.get(state.app_name)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        if app.settings is None:
            return EditorResponse.ephemeral(f"{app.friendly} does not support settings.")

        settings_view = self._build_settings_view(app=app, state=state, acl=acl, actor_user_id=actor_user_id)
        selected_setting = settings_view.selected_setting
        selected_state = (
            self._state_for_setting(app=app, setting=selected_setting, page=settings_view.settings.page_state.page)
            if selected_setting is not None
            else AppManageState(
                mode=AppManageMode.SETTINGS, page=settings_view.settings.page_state.page, app_name=app.name
            )
        )
        if selected_setting is None:
            return EditorResponse.ephemeral("Choose a setting first.")
        if not _setting_can_edit(selected_setting, acl=acl, actor_user_id=actor_user_id):
            return EditorResponse.ephemeral(
                f"Editing `{selected_setting.label}` requires {selected_setting.power_level.name.title()} access."
            )
        if not _setting_allows_modal_entry(selected_setting):
            return EditorResponse.ephemeral("This setting must be changed using its available choices.")

        value = req.values.get(_APP_SETTING_VALUE_FIELD_ID, "").strip()
        if not value:
            return EditorResponse.ephemeral("Value must not be empty.")

        try:
            app.settings.update_setting(actor_user_id, selected_setting, value, remember_input=True)
        except Exception as xcp:
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=self._editor.resolve_locale(req.interaction),
                acl=acl,
                manager=manager,
                state=selected_state,
                status=_error_status(f"Error: setting update failed for `{selected_setting.label}`: {xcp}"),
            )

        response = self._build_editor_response(
            actor_user_id=actor_user_id,
            locale=self._editor.resolve_locale(req.interaction),
            acl=acl,
            manager=manager,
            state=selected_state,
            status=(
                f"{app.friendly} setting `{selected_setting.label}` updated: "
                f"{_setting_display_value(selected_setting, settings_manager=app.settings, actor_user_id=actor_user_id)}. "
                "Settings are saved on launch or via Save Settings."
            ),
        )
        await _send_public_action_notice(
            bot,
            req.interaction,
            _public_setting_update_text(
                actor_user_id=actor_user_id,
                app=app,
                setting=selected_setting,
            ),
        )
        return response

    async def _handle_app_download_action(
        self,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> None:
        manage_actions = _manage_actions_module()
        await manage_actions.handle_app_download_action(
            self,
            req=req,
            acl=acl,
            manager=manager,
            state=state,
            app=app,
        )

    async def _handle_mod_download_action(
        self,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
        selected_mod: Mod | None,
    ) -> None:
        manage_actions = _manage_actions_module()
        await manage_actions.handle_mod_download_action(
            self,
            req=req,
            acl=acl,
            manager=manager,
            state=state,
            app=app,
            selected_mod=selected_mod,
        )

    async def _handle_mod_web_action(
        self,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> None:
        manage_actions = _manage_actions_module()
        await manage_actions.handle_mod_web_action(
            self,
            req=req,
            acl=acl,
            manager=manager,
            state=state,
            app=app,
        )

    def _handle_mod_upload_request_action(
        self,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> EditorResponse:
        message_id = self._interaction_message_id(req.interaction)
        if message_id is None:
            return EditorResponse.ephemeral("App manager message is unavailable.")
        channel_id = getattr(req.interaction, "channel_id", None)
        if channel_id is None:
            return EditorResponse.ephemeral("Upload capture requires a channel.")
        reason = self.manage_lock_reason(app, message_id=message_id)
        if reason is not None:
            return self._build_editor_response(
                actor_user_id=int(req.user_id),
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=_error_status(f"Error: {reason}"),
            )
        ttl_display = Utilities.format_rdelta(Utilities.create_rdelta(int(_MOD_UPLOAD_TTL.total_seconds())))
        meta = ModUploadRequestMeta(
            app_name=app.name,
            page=state.page,
            selected_mod_slot=state.selected_page_slot,
            application_id=hikari.Snowflake(req.interaction.application_id),
            interaction_token=req.interaction.token,
            locale=req.locale,
        )
        self._editor.request_file(
            scope_id=message_id,
            channel_id=channel_id,
            user_id=req.user_id,
            ttl=_MOD_UPLOAD_TTL,
            meta=meta.to_mapping(),
        )
        return self._build_editor_response(
            actor_user_id=int(req.user_id),
            locale=req.locale,
            acl=acl,
            manager=manager,
            state=state,
            status=(
                f"Upload armed for `{app.friendly}`. "
                f"Send one attachment in this channel within {ttl_display}; the first attachment will be installed."
            ),
        )

    async def _handle_app_update_action(
        self,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> None:
        manage_actions = _manage_actions_module()
        await manage_actions.handle_app_update_action(
            self,
            req=req,
            acl=acl,
            manager=manager,
            state=state,
            app=app,
        )

    async def _consume_mod_upload(
        self,
        *,
        upload: EditorFileUpload,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> None:
        manage_actions = _manage_actions_module()
        await manage_actions.consume_mod_upload(
            self,
            upload=upload,
            bot=bot,
            acl=acl,
            manager=manager,
        )

    async def _install_uploaded_mod(
        self,
        *,
        app: App,
        attachment: hikari.Attachment,
    ) -> Mod:
        manage_actions = _manage_actions_module()
        return await manage_actions.install_uploaded_mod(app=app, attachment=attachment)

    async def _edit_upload_response(
        self,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
        meta: ModUploadRequestMeta,
        channel_id: hikari.Snowflakeish,
        status: EditorStatus | str,
        editor_message_id: hikari.Snowflake | None = None,
        actor_user_id: hikari.Snowflakeish | None = None,
        app: App | None = None,
        state: AppManageState | None = None,
    ) -> None:
        manage_actions = _manage_actions_module()
        await manage_actions.edit_upload_response(
            self,
            bot=bot,
            acl=acl,
            manager=manager,
            meta=meta,
            channel_id=channel_id,
            status=status,
            editor_message_id=editor_message_id,
            actor_user_id=actor_user_id,
            app=app,
            state=state,
        )

    async def _force_invalidate_lock(
        self,
        *,
        bot: hikari.GatewayBot,
        lock: AppManagementLock,
        actor_user_id: int,
    ) -> bool:
        manage_actions = _manage_actions_module()
        return await manage_actions.force_invalidate_lock(
            self,
            bot=bot,
            lock=lock,
            actor_user_id=actor_user_id,
        )

    def _extend_editor_session(self, message_id: hikari.Snowflake) -> None:
        if self._editor.timeout is None:
            return
        self._editor.session_store.set_session_deadline(message_id, self._now() + self._editor.timeout)

    def _build_lock_action(self, kind: AppManageLockActionKind, message_id: hikari.Snowflake) -> str:
        return self._lock_action_codec.build(kind, page=0, value=str(int(message_id)))

    def _state_for_mod(
        self,
        *,
        app: App,
        mod_name: str,
        fallback: AppManageState,
    ) -> AppManageState:
        mods = app.has_mod_manager.list_mods()
        for index, mod in enumerate(mods):
            if mod.name != mod_name:
                continue
            page = _page_for_item_index(index)
            slot = index % _PAGE_SIZE
            return AppManageState(mode=AppManageMode.MODS, page=page, app_name=app.name, selected_page_slot=slot)
        return fallback

    def _selected_setting(
        self,
        *,
        app: App,
        state: AppManageState,
    ) -> Setting | None:
        if app.settings is None or state.selected_setting_index is None:
            return None
        options = tuple(app.settings.app.options)
        if state.selected_setting_index >= len(options):
            return None
        return options[state.selected_setting_index]

    def _state_for_setting(
        self,
        *,
        app: App,
        setting: Setting,
        mode: AppManageMode = AppManageMode.SETTINGS,
        page: int | None = None,
        actor_user_id: int | None = None,
    ) -> AppManageState:
        if app.settings is None:
            raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

        resolved_page = page
        if resolved_page is None:
            if mode is AppManageMode.SETTING_CHOICES:
                if actor_user_id is None:
                    resolved_page = 0
                else:
                    resolved_page = _page_for_setting_choice(
                        setting,
                        settings_manager=app.settings,
                        actor_user_id=actor_user_id,
                    )
            else:
                resolved_page = _page_for_setting_key(app.settings.app.options, setting.key)
        selected_setting_index = next(
            (index for index, candidate in enumerate(app.settings.app.options) if candidate.key == setting.key),
            None,
        )
        if selected_setting_index is None:
            raise ValueError(f"Unknown setting key for {app.name}: {setting.key}")
        return AppManageState(
            mode=mode,
            page=resolved_page,
            app_name=app.name,
            selected_setting_index=selected_setting_index,
        )

    async def _edit_editor_message(
        self,
        *,
        interaction: hikari.ComponentInteraction,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | str,
    ) -> None:
        resolved_status: EditorStatus | None = _coerce_status(status)
        status_text: str | None = _status_text(resolved_status)
        assert status_text is not None
        embed, components = self._render_editor(
            actor_user_id=actor_user_id,
            locale=locale,
            acl=acl,
            manager=manager,
            state=state,
            status=resolved_status,
            current_guild_id=self._interaction_guild_id(interaction),
        )
        await interaction.edit_initial_response(
            content=status_text,
            components=components,
            embeds=[] if embed is None else [embed],
        )

    def _build_editor_response(
        self,
        *,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | str,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> EditorResponse:
        resolved_status: EditorStatus | None = _coerce_status(status)
        status_text: str | None = _status_text(resolved_status)
        assert status_text is not None
        embed, components = self._render_editor(
            actor_user_id=actor_user_id,
            locale=locale,
            acl=acl,
            manager=manager,
            state=state,
            status=resolved_status,
            current_guild_id=current_guild_id,
        )
        if embed is None:
            return EditorResponse.update(status_text, components=components, embeds=[])
        return EditorResponse.update(status_text, components=components, embeds=[embed])

    def _render_editor(
        self,
        *,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | str | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed | None, list[hikari.api.MessageActionRowBuilder]]:
        editor_ctx: EditorContext = self._editor.context(
            scope_id=actor_user_id,
            user_id=actor_user_id,
            locale=locale,
        )
        layout: EditorLayout = EditorLayout(editor_ctx)
        resolved_status: EditorStatus | None = _coerce_status(status)
        if state.is_mods and state.app_name is not None:
            return self._render_mods(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=resolved_status,
            )
        if state.is_setting_choices and state.app_name is not None:
            return self._render_setting_choices(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=resolved_status,
            )
        if state.is_settings and state.app_name is not None:
            return self._render_settings(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=resolved_status,
            )
        if state.is_relay:
            return self._render_relay(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=resolved_status,
                current_guild_id=current_guild_id,
            )
        if state.is_create:
            return self._render_create(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=resolved_status,
            )
        if state.is_home and state.app_name is not None:
            return self._render_home(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=resolved_status,
                current_guild_id=current_guild_id,
            )
        return self._render_landing(
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=resolved_status,
            current_guild_id=current_guild_id,
        )

    def _render_landing(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.render_landing(
            self,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
            current_guild_id=current_guild_id,
        )

    def _render_create(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.render_create(
            self,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )

    def _render_home(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.render_home(
            self,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
            current_guild_id=current_guild_id,
        )

    def _render_relay(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
        current_guild_id: hikari.Snowflakeish | None = None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.render_relay(
            self,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
            current_guild_id=current_guild_id,
        )

    def _render_mods(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.render_mods(
            self,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )

    def _render_settings(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.render_settings(
            self,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )

    def _render_setting_choices(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: EditorStatus | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.render_setting_choices(
            self,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )

    def _build_landing_view(self, *, manager: App_Manager, state: AppManageState) -> LandingView:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.build_landing_view(manager=manager, state=state)

    def _build_create_view(self, *, manager: App_Manager) -> CreateView:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.build_create_view(manager=manager)

    def _build_mods_view(self, *, app: App, state: AppManageState) -> ModsView:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.build_mods_view(app=app, state=state)

    def _build_settings_view(
        self,
        *,
        app: App,
        state: AppManageState,
        acl: Access_Control,
        actor_user_id: int,
    ) -> SettingsView:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.build_settings_view(
            app=app,
            state=state,
            acl=acl,
            actor_user_id=actor_user_id,
        )

    def _build_setting_choices_view(
        self,
        *,
        setting: Setting,
        page: int,
    ) -> SettingChoicesView:
        manage_render: _AppManageRenderModule = _manage_render_module()
        return manage_render.build_setting_choices_view(setting=setting, page=page)

    def _build_state_action(self, kind: AppManageActionKind, state: AppManageState | None) -> str:
        return self._action_codec.build(
            kind,
            page=0 if state is None else state.page,
            value=None if state is None else _state_value(state),
        )

    @staticmethod
    def _require_acl(deps: Mapping[str, object]) -> Access_Control:
        value: object | None = deps.get("acl")
        if not isinstance(value, Access_Control):
            raise TypeError("App manager requires Access_Control")
        return value

    @staticmethod
    def _require_manager(deps: Mapping[str, object]) -> App_Manager:
        value: object | None = deps.get("manager")
        if not isinstance(value, App_Manager):
            raise TypeError("App manager requires App_Manager")
        return value

    @staticmethod
    def _require_bot(deps: Mapping[str, object]) -> hikari.GatewayBot:
        value: object | None = deps.get("bot")
        if not isinstance(value, hikari.GatewayBot):
            raise TypeError("App manager requires hikari.GatewayBot")
        return value
