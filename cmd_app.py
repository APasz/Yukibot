from __future__ import annotations

import asyncio
import enum
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Generic, TypeVar

import hikari
import lightbulb
from hikari_ui import (
    Editor,
    EditorButton,
    EditorFileUpload,
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

import _errors
from _discord import Distils, FileDeliveryMode
from _manager import App_Manager, AppInstanceCreateRequest, ac_all_apps, ac_enabled_apps
from _mod_ops import download_paths as build_mod_download_paths
from _mod_ops import install_attachments, refresh_mod_index, remove_mods, toggle_coremod, toggle_mod
from _security import Access_Control
from _sys import Stats_System
from _utils import Utilities
from apps._app import App
from apps._mod import Mod
from apps._settings import Setting

log = logging.getLogger(__name__)

group_app = lightbulb.Group("app", "App Management")  # type: ignore[reportAssignmentType]

_APP_MANAGE_PREFIX = "app-manage:"
_APP_SETTING_MODAL_PREFIX = "app-setting:"
_APP_CREATE_MODAL_PREFIX = "app-create:"
_APP_SETTING_VALUE_FIELD_ID = "value"
_APP_CREATE_INSTANCE_KEY_FIELD_ID = "instance_key"
_APP_CREATE_FRIENDLY_NAME_FIELD_ID = "friendly_name"
_APP_CREATE_SUBFOLDER_FIELD_ID = "subfolder"
_APP_CREATE_PORT_FIELD_ID = "port"
_APP_CREATE_SERVER_LOG_FILE_FIELD_ID = "server_log_file"
_MOD_UPLOAD_TTL = timedelta(minutes=10)
_PAGE_SIZE = 25
_RELAY_CHANNEL_TYPES: tuple[hikari.ChannelType, ...] = (
    hikari.ChannelType.GUILD_TEXT,
    hikari.ChannelType.GUILD_NEWS,
)
_STATE_VALUE_SEPARATOR = "~"

ValueT = TypeVar("ValueT")


class AppManageActionKind(enum.StrEnum):
    BACK_LANDING = "bl"
    BACK_HOME = "bh"
    BACK_SETTINGS = "bs"
    CLEAR_RELAY_CHANNEL = "cr"
    CLOSE = "cl"
    CREATE_INSTANCE = "ci"
    DOWNLOAD_APP = "da"
    DOWNLOAD_MOD = "dm"
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
    REMOVE_MOD = "rm"
    REQUEST_MOD_UPLOAD = "ru"
    SAVE_RELAY_CHANNEL = "sr"
    SAVE_SETTINGS = "sa"
    SELECT_MOD = "sm"
    SELECT_SETTING = "ss"
    TOGGLE_APP = "ta"
    TOGGLE_COREMOD = "tc"
    TOGGLE_MOD = "tm"
    UPDATE_APP = "ua"
    UPDATE_SETTING = "us"
    WRITE_SETTING = "ws"


class AppManageMode(enum.StrEnum):
    CREATE = "ct"
    HOME = "hm"
    LANDING = "ld"
    MODS = "md"
    RELAY = "rl"
    SETTING_CHOICES = "sc"
    SETTINGS = "st"


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


APP_MANAGE_ACTION_LEVELS: dict[AppManageActionKind, Access_Control.LvL] = {
    AppManageActionKind.BACK_LANDING: Access_Control.LvL.user,
    AppManageActionKind.BACK_HOME: Access_Control.LvL.user,
    AppManageActionKind.BACK_SETTINGS: Access_Control.LvL.user,
    AppManageActionKind.CLEAR_RELAY_CHANNEL: Access_Control.LvL.sudo,
    AppManageActionKind.CLOSE: Access_Control.LvL.user,
    AppManageActionKind.CREATE_INSTANCE: Access_Control.LvL.sudo,
    AppManageActionKind.DOWNLOAD_APP: Access_Control.LvL.user,
    AppManageActionKind.DOWNLOAD_MOD: Access_Control.LvL.user,
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
    AppManageActionKind.REMOVE_MOD: Access_Control.LvL.user,
    AppManageActionKind.REQUEST_MOD_UPLOAD: Access_Control.LvL.user,
    AppManageActionKind.SAVE_RELAY_CHANNEL: Access_Control.LvL.sudo,
    AppManageActionKind.SAVE_SETTINGS: Access_Control.LvL.user,
    AppManageActionKind.SELECT_MOD: Access_Control.LvL.user,
    AppManageActionKind.SELECT_SETTING: Access_Control.LvL.user,
    AppManageActionKind.TOGGLE_APP: Access_Control.LvL.sudo,
    AppManageActionKind.TOGGLE_COREMOD: Access_Control.LvL.sudo,
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
    selected_setting_key: str | None = None

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
class AppManagementLock:
    message_id: hikari.Snowflake
    user_id: hikari.Snowflake
    app_name: str
    expires_at: datetime


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
    start = page * _PAGE_SIZE
    end = start + _PAGE_SIZE
    return values[start:end]


def _paginate(values: Sequence[ValueT], page: int) -> PagedItems[ValueT]:
    total_pages = _page_count(len(values))
    current_page = _clamp_page(page, total_pages)
    return PagedItems(
        visible=tuple(_page_slice(values, current_page)),
        total_count=len(values),
        page_state=EditorPageState(page=current_page, total_pages=total_pages),
    )


def _all_apps(manager: App_Manager) -> tuple[App, ...]:
    return tuple(sorted(manager.apps.values(), key=lambda app: app.friendly.casefold()))


def _component_text(value: str, /, *, limit: int = 100) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    if limit <= 3:
        return stripped[:limit]
    return stripped[: limit - 3].rstrip() + "..."


def _display_value(values: Sequence[str]) -> str:
    return "\n".join(values) if values else "None"


def _app_started_response_text(app: App) -> str:
    lines = [f"{app.friendly} Started!"]
    if app.cfg.join_display_address is not None:
        lines.append(f"Join: `{app.cfg.join_display_address}`")
    return "\n".join(lines)


def _parse_optional_port(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    if not value.isdecimal():
        raise ValueError("Port must be a whole number.")
    port = int(value)
    if port <= 0 or port > 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return port


def _required_level_for_capability(capability: AppManageCapability) -> Access_Control.LvL:
    return APP_MANAGE_CAPABILITY_PERMISSIONS[capability]


def _state_value(state: AppManageState) -> str:
    app_name = state.app_name or ""
    selected_page_slot = "" if state.selected_page_slot is None else str(state.selected_page_slot)
    selected_setting_key = state.selected_setting_key or ""
    for value in (state.mode.value, app_name, selected_page_slot, selected_setting_key):
        if _STATE_VALUE_SEPARATOR in value:
            raise ValueError(f"state value must not contain '{_STATE_VALUE_SEPARATOR}'")
    return _STATE_VALUE_SEPARATOR.join((state.mode.value, app_name, selected_page_slot, selected_setting_key))


def _state_from_value(raw: str | None, page: int) -> AppManageState | None:
    if raw is None:
        return AppManageState(mode=AppManageMode.LANDING, page=page)

    parts = raw.split(_STATE_VALUE_SEPARATOR)
    if len(parts) == 2:
        raw_mode, raw_app_name = parts
        raw_selected_page_slot = ""
        raw_selected_setting_key = ""
    elif len(parts) == 3:
        raw_mode, raw_app_name, raw_selected_page_slot = parts
        raw_selected_setting_key = ""
    elif len(parts) == 4:
        raw_mode, raw_app_name, raw_selected_page_slot, raw_selected_setting_key = parts
    elif len(parts) == 5:
        raw_mode, raw_app_name, raw_selected_page_slot, raw_selected_setting_key, _raw_legacy_relay_state = parts
    else:
        return None

    try:
        mode = AppManageMode(raw_mode)
    except ValueError:
        return None

    app_name = raw_app_name or None
    selected_page_slot: int | None = None
    if raw_selected_page_slot:
        try:
            selected_page_slot = int(raw_selected_page_slot)
        except ValueError:
            return None
        if selected_page_slot < 0:
            return None

    state = AppManageState(
        mode=mode,
        app_name=app_name,
        page=page,
        selected_page_slot=selected_page_slot,
        selected_setting_key=raw_selected_setting_key or None,
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
    if state.mode is AppManageMode.SETTING_CHOICES and state.selected_setting_key is None:
        return None
    return state


def _app_capabilities(app: App) -> tuple[AppManageCapability, ...]:
    capabilities: list[AppManageCapability] = []
    if app.directory.exists():
        capabilities.append(AppManageCapability.DOWNLOAD)
    if app.supports_chat_relay:
        capabilities.append(AppManageCapability.CHAT)
    capabilities.append(AppManageCapability.TOGGLE)
    if app.updater is not None:
        capabilities.append(AppManageCapability.UPDATE)
    return tuple(capabilities)


def _app_extra_capability_labels(app: App) -> tuple[str, ...]:
    labels: list[str] = []
    for capability in _app_capabilities(app):
        if capability is AppManageCapability.TOGGLE:
            continue
        if capability is AppManageCapability.CHAT:
            chat_label = app.chat_relay_support.capability_label
            if chat_label is not None:
                labels.append(chat_label)
            continue
        labels.append(capability.value)
    if app.mods is not None:
        labels.append("Mods")
    if app.settings is not None:
        labels.append("Settings")
    return tuple(labels)


def _app_option_description(app: App) -> str:
    labels = _app_extra_capability_labels(app)
    if not labels:
        return "No extra actions"
    return ", ".join(labels)


def _app_status_lines(app: App) -> tuple[str, ...]:
    local_version = "none"
    if app.updater and app.updater.version is not None:
        local_version = app.updater.stringise(app.updater.version)
    return (
        f"scope: {app.scope}",
        f"version: {local_version}",
    )


def _channel_display(channel_id: hikari.Snowflake | None) -> str:
    if channel_id is None:
        return "Not configured"
    return f"<#{int(channel_id)}>"


def _default_relay_lines(manager: App_Manager) -> tuple[str, ...]:
    return (f"channel: {_channel_display(manager.default_chat_channel)}",)


def _app_relay_lines(app: App, manager: App_Manager) -> tuple[str, ...]:
    effective = _channel_display(app.chat_channel)
    if app.chat_channel_override is not None and manager.default_chat_channel is not None:
        effective = f"{effective} [{_channel_display(manager.default_chat_channel)}]"
    return (
        f"support: {app.chat_relay_support.display_value}",
        f"effective: {effective}",
    )


def _status_is_error(status: str | None) -> bool:
    if not status:
        return False
    lowered = status.casefold()
    return lowered.startswith("error:") or " failed" in lowered


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
    if mod.cfg.coremod:
        labels.append("Coremod")
    return ", ".join(labels)


def _mod_overview_lines(view: ModsView) -> tuple[str, ...]:
    return (
        f"total: {view.mods.total_count}",
        f"enabled: {view.enabled_count}",
        f"disabled: {view.disabled_count}",
        f"coremods: {view.coremod_count}",
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
        f"coremod: {'Yes' if mod.cfg.coremod else 'No'}",
    ]
    if mod.cfg.coremod:
        if acl.can(actor_user_id, acl.LvL.sudo):
            lines.append("restriction: Protected coremod; sudo override is active")
        else:
            lines.append("restriction: Protected coremod; toggle/remove requires sudo")
    return tuple(lines)


def _setting_can_edit(setting: Setting, *, acl: Access_Control, actor_user_id: int) -> bool:
    return acl.can(actor_user_id, setting.power_level)


def _setting_choice_items(setting: Setting) -> tuple[tuple[str, str], ...]:
    return setting.choice_items()


def _setting_supports_choice_select(setting: Setting) -> bool:
    choice_count = len(_setting_choice_items(setting))
    return 0 < choice_count <= 25


def _setting_requires_choice_browser(setting: Setting) -> bool:
    return len(_setting_choice_items(setting)) > 25


def _setting_allows_modal_entry(setting: Setting) -> bool:
    return not setting.choices or not setting.strict_choice


def _setting_current_input_value(setting: Setting) -> str:
    label = setting.choice_label_for_value()
    if label is not None:
        return label
    return "" if isinstance(setting.value, hikari.UndefinedType) else str(setting.value)


def _setting_display_value(setting: Setting) -> str:
    return setting.display_value()


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


def _page_for_setting_choice(setting: Setting) -> int:
    choice_label = setting.choice_label_for_value()
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
    acl: Access_Control,
    actor_user_id: int,
) -> tuple[str, ...]:
    can_edit = _setting_can_edit(setting, acl=acl, actor_user_id=actor_user_id)
    lines = [
        f"key: {setting.key}",
        f"type: {setting.value_type.__name__}",
        (
            f"value: {_setting_display_value(setting)}"
            if can_edit
            else f"value: hidden (requires {setting.power_level.name.title()})"
        ),
        f"permission: {setting.power_level.name.title()}",
    ]
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
    if setting.desc:
        lines.append(f"details: {setting.desc}")
    return tuple(lines)


class AppManageService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(AppManageActionKind)
        self._editor = Editor(
            prefix=_APP_MANAGE_PREFIX,
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
        )
        self._setting_modal = ModalKit(
            prefix=_APP_SETTING_MODAL_PREFIX,
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
            prefix=_APP_CREATE_MODAL_PREFIX,
            schema=ModalSchema(
                [
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
                    ModalTextField(
                        id=_APP_CREATE_SERVER_LOG_FILE_FIELD_ID,
                        label="Server Log File",
                        style=hikari.TextInputStyle.SHORT,
                        required=False,
                        max_length=200,
                    ),
                ]
            ),
        )
        self._app_locks: dict[hikari.Snowflake, AppManagementLock] = {}

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
    def _ensure_aware_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    async def _open_create_modal(
        self,
        *,
        interaction: hikari.ComponentInteraction,
        actor_user_id: int,
        state: AppManageState,
    ) -> None:
        if state.app_name is None:
            raise ValueError("Create modal requires a selected app scope.")
        await interaction.create_modal_response(
            title=f"Create {state.app_name} Instance",
            custom_id=self._create_modal.build_id(
                self._build_state_action(AppManageActionKind.CREATE_INSTANCE, state),
                scope_id=actor_user_id,
                user_id=actor_user_id,
            ),
            components=self._create_modal.rows(
                {
                    _APP_CREATE_INSTANCE_KEY_FIELD_ID: "",
                    _APP_CREATE_FRIENDLY_NAME_FIELD_ID: "",
                    _APP_CREATE_SUBFOLDER_FIELD_ID: "",
                    _APP_CREATE_PORT_FIELD_ID: "",
                    _APP_CREATE_SERVER_LOG_FILE_FIELD_ID: "",
                }
            ),
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
        expired_message_ids = [
            message_id for message_id, lock in self._app_locks.items() if lock.expires_at <= current_time
        ]
        for message_id in expired_message_ids:
            self._app_locks.pop(message_id, None)

    def _touch_app_lock(
        self,
        *,
        message_id: hikari.Snowflake,
        user_id: hikari.Snowflakeish,
        app_name: str,
        now: datetime | None = None,
    ) -> None:
        current_time = self._now() if now is None else now
        self._app_locks[message_id] = AppManagementLock(
            message_id=message_id,
            user_id=hikari.Snowflake(user_id),
            app_name=app_name,
            expires_at=self._lock_deadline(current_time),
        )

    def _release_app_lock(self, *, message_id: hikari.Snowflakeish) -> None:
        self._app_locks.pop(hikari.Snowflake(message_id), None)

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
        lock = self._find_app_lock(app_name=app.name)
        if lock is None:
            return None
        return f"{app.friendly} is currently being managed and cannot be started."

    def manage_lock_reason(
        self,
        app: App,
        *,
        message_id: hikari.Snowflake | None = None,
    ) -> str | None:
        if app.check_running():
            return f"{app.friendly} is currently running and cannot be managed."
        lock = self._find_app_lock(app_name=app.name, exclude_message_id=message_id)
        if lock is not None:
            return f"{app.friendly} is already being managed in another editor session."
        return None

    async def open_editor(
        self,
        *,
        ctx: lightbulb.Context,
        acl: Access_Control,
        manager: App_Manager,
        initial_app: App | None = None,
        initial_mode: AppManageMode = AppManageMode.HOME,
        status: str | None = None,
    ) -> None:
        if initial_app is None:
            state = AppManageState(mode=AppManageMode.LANDING, page=0)
            status = "Choose an app below." if status is None else status
        else:
            state = AppManageState(mode=initial_mode, app_name=initial_app.name, page=0)
            if status is None:
                if initial_mode is AppManageMode.MODS:
                    status = f"Opened mods for {initial_app.friendly}."
                elif initial_mode is AppManageMode.SETTINGS:
                    status = f"Opened settings for {initial_app.friendly}."
                else:
                    status = f"Opened {initial_app.friendly}."
        locale = self._editor.resolve_locale(ctx.interaction)
        embed, components = self._render_editor(
            actor_user_id=int(ctx.user.id),
            locale=locale,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )
        if embed is None:
            response = await ctx.respond(
                status,
                components=components,
                flags=hikari.MessageFlag.EPHEMERAL,
            )
        else:
            response = await ctx.respond(
                status,
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
        self._touch_app_lock(message_id=message_id, user_id=ctx.user.id, app_name=initial_app.name)

    async def route_component(
        self,
        interaction: hikari.ComponentInteraction,
        *,
        acl: Access_Control,
        manager: App_Manager,
    ) -> bool:
        self._prune_app_locks()
        return await self._editor.route(interaction, acl=acl, manager=manager)

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
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
        action = self._action_codec.parse(req.action)
        if action is None:
            return False

        required_level = APP_MANAGE_ACTION_LEVELS.get(action.kind)
        if required_level is None:
            return False
        return acl.can(int(req.user_id), required_level)

    async def _authorise_setting_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        acl = self._require_acl(deps)
        return acl.can(int(req.user_id), Access_Control.LvL.user)

    async def _authorise_create_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        acl = self._require_acl(deps)
        return acl.can(int(req.user_id), APP_MANAGE_ACTION_LEVELS[AppManageActionKind.CREATE_INSTANCE])

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        manager = self._require_manager(deps)
        acl = self._require_acl(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown app manager action.")

        state = _state_from_value(action.value, action.page)
        if state is None:
            return EditorResponse.ephemeral("App manager state is invalid.")

        actor_user_id = int(req.user_id)
        message_id = self._interaction_message_id(req.interaction)
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
                        status=f"Error: {reason}",
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
                        status=f"Error: mod refresh failed for `{app.friendly}`: {xcp}",
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
                    app.settings.app.load()
                except Exception as xcp:
                    return self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        acl=acl,
                        manager=manager,
                        state=state,
                        status=f"Error: settings reload failed for `{app.friendly}`: {xcp}",
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
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=next_state,
                    status=f"{app.friendly} settings reloaded from disk.",
                )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status="App manager refreshed.",
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
            )
        if action.kind is AppManageActionKind.SAVE_RELAY_CHANNEL:
            if not req.values:
                return EditorResponse.ephemeral("Choose a relay channel first.")
            channel_id = hikari.Snowflake(req.values[0])
            if state.mode is AppManageMode.RELAY and state.app_name is None:
                manager.set_default_chat_channel(channel_id)
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=f"Default relay channel set to <#{int(channel_id)}>.",
                )
            if state.mode is not AppManageMode.RELAY or state.app_name is None:
                return EditorResponse.ephemeral("Relay management is not available here.")
            app = manager.get(state.app_name)
            if not app.supports_chat_relay:
                return EditorResponse.ephemeral(f"{app.friendly} does not support chat relay.")
            manager.set_app_chat_channel(app, channel_id)
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=f"{app.friendly} relay channel set to <#{int(channel_id)}>.",
            )
        if action.kind is AppManageActionKind.CLEAR_RELAY_CHANNEL:
            if state.mode is AppManageMode.RELAY and state.app_name is None:
                manager.clear_default_chat_channel()
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status="Default relay channel cleared.",
                )
            if state.mode is not AppManageMode.RELAY or state.app_name is None:
                return EditorResponse.ephemeral("Relay management is not available here.")
            app = manager.get(state.app_name)
            if not app.supports_chat_relay:
                return EditorResponse.ephemeral(f"{app.friendly} does not support chat relay.")
            manager.clear_app_chat_channel(app)
            status_text = (
                f"{app.friendly} now uses the default relay channel."
                if manager.default_chat_channel is not None
                else f"{app.friendly} relay override cleared."
            )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=status_text,
            )
        if action.kind is AppManageActionKind.BACK_LANDING:
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.LANDING, page=state.page),
                status="Returned to app list.",
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
                        status=f"Error: {reason}",
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
                    selected_setting.update(req.values[0])
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
                        status=f"Error: setting update failed for `{selected_setting.label}`: {xcp}",
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
                        f"{_setting_display_value(selected_setting)}. Settings are saved on launch or via Save Settings."
                    ),
                )

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
                            _APP_SETTING_VALUE_FIELD_ID: _setting_current_input_value(selected_setting),
                        }
                    ),
                )
                return None

            if action.kind is AppManageActionKind.SAVE_SETTINGS:
                try:
                    app.settings.app.save()
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
                        status=f"Error: settings save failed for `{app.friendly}`: {xcp}",
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
                    status=f"Error: {reason}",
                )
            try:
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
                    status=f"Error: mod toggle failed for `{mods_view.selected_mod.friendly}`: {xcp}",
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
                    status=f"Error: {reason}",
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
                    status=f"Error: coremod update failed for `{mods_view.selected_mod.friendly}`: {xcp}",
                )
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=f"{app.friendly} mod `{mod.friendly}` coremod: {'Enabled' if mod.cfg.coremod else 'Disabled'}.",
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
                    status=f"Error: {reason}",
                )
            try:
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
                    status=f"Error: remove failed for `{mods_view.selected_mod.friendly}`: {xcp}",
                )
            if result.errors:
                return self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    acl=acl,
                    manager=manager,
                    state=next_state,
                    status=f"Error: remove failed for `{mods_view.selected_mod.friendly}`: {result.errors[0]}",
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
                    )
                )
            except Exception as xcp:
                return self._build_editor_response(
                    actor_user_id=int(req.user_id),
                    locale=locale,
                    acl=acl,
                    manager=manager,
                    state=state,
                    status=f"Error: app instance creation failed: {xcp}",
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
            selected_setting.update(value)
        except Exception as xcp:
            return self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=self._editor.resolve_locale(req.interaction),
                acl=acl,
                manager=manager,
                state=selected_state,
                status=f"Error: setting update failed for `{selected_setting.label}`: {xcp}",
            )

        return self._build_editor_response(
            actor_user_id=actor_user_id,
            locale=self._editor.resolve_locale(req.interaction),
            acl=acl,
            manager=manager,
            state=selected_state,
            status=(
                f"{app.friendly} setting `{selected_setting.label}` updated: "
                f"{_setting_display_value(selected_setting)}. Settings are saved on launch or via Save Settings."
            ),
        )

    async def _handle_app_download_action(
        self,
        *,
        req: EditorRequest,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        app: App,
    ) -> None:
        await req.interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
        try:
            if reason := self.manage_lock_reason(app, message_id=self._interaction_message_id(req.interaction)):
                raise RuntimeError(reason)
            if not app.directory.exists():
                raise FileNotFoundError(f"{app.directory} does not exist")

            size = Distils.file.pointer_size(app.directory)
            padded_size = round(size + (size / 100 * 10))
            stats = Stats_System()
            stats.update()
            free_space = stats.disk.usage.free
            if free_space < padded_size:
                raise _errors.NotEnoughDisk(
                    f"{Utilities.humanise_bytes(free_space)} < {Utilities.humanise_bytes(padded_size)}"
                )

            download_message = await Distils.build_direct_file_message([app.directory], app.friendly)
            status = f"Prepared download for `{app.friendly}`.\n{download_message}"
        except Exception as xcp:
            status = f"Error: download failed for `{app.friendly}`: {xcp}"

        await self._edit_editor_message(
            interaction=req.interaction,
            actor_user_id=int(req.user_id),
            locale=req.locale,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
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
        await req.interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
        try:
            if reason := self.manage_lock_reason(app, message_id=self._interaction_message_id(req.interaction)):
                raise RuntimeError(reason)
            if app.mods is None:
                raise _errors.UnsupportedModManager(app.friendly)
            channel_id = getattr(req.interaction, "channel_id", None)
            if channel_id is None:
                raise RuntimeError("Download delivery requires a channel.")

            mod_names = None if selected_mod is None else (selected_mod.name,)
            paths = list(build_mod_download_paths(app.has_mod_manager, mod_names, default_enabled_only=False))
            if not paths:
                raise FileNotFoundError(f"No mods found for {app.friendly}")

            base_name = selected_mod.friendly if selected_mod is not None else f"{app.friendly}_mods"
            delivery = await Distils.send_files(
                req.interaction.app.rest,
                channel_id,
                paths,
                display_name=base_name,
            )
            if selected_mod is not None:
                status = (
                    f"Sent `{selected_mod.friendly}` in a separate message."
                    if delivery is not FileDeliveryMode.DIRECT
                    else f"Posted direct download for `{selected_mod.friendly}` in a separate message."
                )
            else:
                status = (
                    f"Sent mod download for `{app.friendly}` in a separate message."
                    if delivery is not FileDeliveryMode.DIRECT
                    else f"Posted direct mod download for `{app.friendly}` in a separate message."
                )
        except Exception as xcp:
            label = selected_mod.friendly if selected_mod is not None else app.friendly
            status = f"Error: mod download failed for `{label}`: {xcp}"

        await self._edit_editor_message(
            interaction=req.interaction,
            actor_user_id=int(req.user_id),
            locale=req.locale,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
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
                status=f"Error: {reason}",
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
        await req.interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
        try:
            if reason := self.manage_lock_reason(app, message_id=self._interaction_message_id(req.interaction)):
                raise RuntimeError(reason)
            if app.updater is None:
                raise _errors.UnsupportedUpdate(f"{app.friendly} does not have an updater")

            previous = app.updater.stringise(app.updater.version) if app.updater.version is not None else None
            updated = await app.updater.base()
            if updated is None:
                status = f"No new update found for `{app.friendly}`."
            elif previous is None:
                status = f"Downloaded update `{updated}` for `{app.friendly}`."
            else:
                status = f"`{app.friendly}` update: `{previous} -> {updated}`."
        except Exception as xcp:
            status = f"Error: update failed for `{app.friendly}`: {xcp}"

        await self._edit_editor_message(
            interaction=req.interaction,
            actor_user_id=int(req.user_id),
            locale=req.locale,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )

    async def _consume_mod_upload(
        self,
        *,
        upload: EditorFileUpload,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
    ) -> None:
        meta = _mod_upload_meta_from_mapping(upload.request.meta)
        if meta is None:
            await bot.rest.create_message(upload.message.channel_id, "Error: upload request metadata is invalid.")
            return
        try:
            app = manager.get(meta.app_name)
        except ValueError as xcp:
            await self._edit_upload_response(
                bot=bot,
                acl=acl,
                manager=manager,
                meta=meta,
                channel_id=upload.message.channel_id,
                status=f"Error: upload failed for `{meta.app_name}`: {xcp}",
            )
            return

        base_state = AppManageState(
            mode=AppManageMode.MODS,
            page=meta.page,
            app_name=app.name,
            selected_page_slot=meta.selected_mod_slot,
        )
        try:
            reason = self.manage_lock_reason(app, message_id=upload.request.scope_id)
            if reason is not None:
                raise RuntimeError(reason)
            installed = await self._install_uploaded_mod(app=app, attachment=upload.attachment)
            next_state = self._state_for_mod(app=app, mod_name=installed.name, fallback=base_state)
            status = f"Installed `{installed.friendly}` to {app.friendly}."
        except Exception as xcp:
            next_state = base_state
            status = f"Error: upload failed for `{app.friendly}`: {xcp}"

        await self._edit_upload_response(
            bot=bot,
            acl=acl,
            manager=manager,
            meta=meta,
            editor_message_id=upload.request.scope_id,
            actor_user_id=upload.request.user_id,
            channel_id=upload.message.channel_id,
            app=app,
            state=next_state,
            status=status,
        )

    async def _install_uploaded_mod(
        self,
        *,
        app: App,
        attachment: hikari.Attachment,
    ) -> Mod:
        if app.mods is None:
            raise _errors.UnsupportedModManager(app.friendly)
        mod_names_before = {mod.name for mod in app.has_mod_manager.list_mods()}
        installed = await install_attachments(app.has_mod_manager, (attachment,), atomic=True)
        installed_mod = installed[0]
        if installed_mod.name not in mod_names_before:
            return installed_mod
        return app.has_mod_manager.get(installed_mod.name)

    async def _edit_upload_response(
        self,
        *,
        bot: hikari.GatewayBot,
        acl: Access_Control,
        manager: App_Manager,
        meta: ModUploadRequestMeta,
        channel_id: hikari.Snowflakeish,
        status: str,
        editor_message_id: hikari.Snowflake | None = None,
        actor_user_id: hikari.Snowflakeish | None = None,
        app: App | None = None,
        state: AppManageState | None = None,
    ) -> None:
        if app is not None and state is not None and editor_message_id is not None and actor_user_id is not None:
            self._touch_app_lock(message_id=editor_message_id, user_id=actor_user_id, app_name=app.name)
            self._extend_editor_session(editor_message_id)
            embed, components = self._render_editor(
                actor_user_id=int(actor_user_id),
                locale=meta.locale,
                acl=acl,
                manager=manager,
                state=state,
                status=status,
            )
            try:
                await bot.rest.edit_interaction_response(
                    meta.application_id,
                    meta.interaction_token,
                    content=status,
                    components=components,
                    embeds=[] if embed is None else [embed],
                )
                return
            except Exception:
                log.exception("App.Manage.UploadEdit")
        await bot.rest.create_message(channel_id, status)

    def _extend_editor_session(self, message_id: hikari.Snowflake) -> None:
        if self._editor.timeout is None:
            return
        self._editor.session_store.set_session_deadline(message_id, self._now() + self._editor.timeout)

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
        if app.settings is None or state.selected_setting_key is None:
            return None
        return app.settings.app.get_setting(state.selected_setting_key)

    def _state_for_setting(
        self,
        *,
        app: App,
        setting: Setting,
        mode: AppManageMode = AppManageMode.SETTINGS,
        page: int | None = None,
    ) -> AppManageState:
        if app.settings is None:
            raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

        resolved_page = page
        if resolved_page is None:
            if mode is AppManageMode.SETTING_CHOICES:
                resolved_page = _page_for_setting_choice(setting)
            else:
                resolved_page = _page_for_setting_key(app.settings.app.options, setting.key)
        return AppManageState(
            mode=mode,
            page=resolved_page,
            app_name=app.name,
            selected_setting_key=setting.key,
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
        status: str,
    ) -> None:
        embed, components = self._render_editor(
            actor_user_id=actor_user_id,
            locale=locale,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )
        await interaction.edit_initial_response(
            content=status,
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
        status: str,
    ) -> EditorResponse:
        embed, components = self._render_editor(
            actor_user_id=actor_user_id,
            locale=locale,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )
        if embed is None:
            return EditorResponse.update(status, components=components, embeds=[])
        return EditorResponse.update(status, components=components, embeds=[embed])

    def _render_editor(
        self,
        *,
        actor_user_id: int,
        locale: hikari.Locale,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed | None, list[hikari.api.MessageActionRowBuilder]]:
        editor_ctx = self._editor.context(
            scope_id=actor_user_id,
            user_id=actor_user_id,
            locale=locale,
        )
        layout = EditorLayout(editor_ctx)
        if state.is_mods and state.app_name is not None:
            return self._render_mods(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=status,
            )
        if state.is_setting_choices and state.app_name is not None:
            return self._render_setting_choices(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=status,
            )
        if state.is_settings and state.app_name is not None:
            return self._render_settings(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=status,
            )
        if state.is_relay:
            return self._render_relay(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=status,
            )
        if state.is_create:
            return self._render_create(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=status,
            )
        if state.is_home and state.app_name is not None:
            return self._render_home(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=state,
                status=status,
            )
        return self._render_landing(
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=state,
            status=status,
        )

    def _render_landing(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        view = self._build_landing_view(manager=manager, state=state)
        embed = hikari.Embed(
            title=f"{'Error | ' if _status_is_error(status) else ''}App Manager",
            color=0x4B5563,
        )
        embed.add_field(name="Default Relay", value=_display_value(_default_relay_lines(manager)), inline=False)
        can_manage_relay = acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_RELAY_CHANNEL])

        if view.apps.visible:
            layout.add_text_select(
                self._build_state_action(
                    AppManageActionKind.OPEN_APP,
                    AppManageState(mode=AppManageMode.LANDING, page=view.apps.page_state.page),
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(app.friendly),
                        value=app.name,
                        description=_component_text(_app_option_description(app)),
                    )
                    for app in view.apps.visible
                ],
                placeholder="Choose an app to manage",
            )
        layout.add_buttons(
            EditorButton(
                self._build_state_action(AppManageActionKind.OPEN_RELAY, state),
                "Manage Relay",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=not can_manage_relay,
            ),
        )

        prev_action = None
        next_action = None
        if view.apps.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                AppManageActionKind.PAGE,
                AppManageState(mode=AppManageMode.LANDING, page=max(0, view.apps.page_state.page - 1)),
            )
            next_action = self._build_state_action(
                AppManageActionKind.PAGE,
                AppManageState(
                    mode=AppManageMode.LANDING,
                    page=min(view.apps.page_state.total_pages - 1, view.apps.page_state.page + 1),
                ),
            )
        layout.page_footer(
            self._action_codec.build(AppManageActionKind.CLOSE, page=0),
            page_state=view.apps.page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._build_state_action(AppManageActionKind.OPEN_CREATE, state),
                    "Create",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_CREATE]),
                ),
            ),
        )
        return embed, layout.build()

    def _render_create(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        view = self._build_create_view(manager=manager)
        embed = hikari.Embed(
            title=f"{'Error | ' if _status_is_error(status) else ''}Create App Instance",
            color=0x4B5563,
        )
        embed.add_field(
            name="Instructions",
            value=_display_value(
                (
                    "This writes a new instance entry under `apps/<scope>/instances.json`.",
                    "The app stays hidden until the bot is restarted.",
                    "Use a subfolder path relative to `DIR_APP` only.",
                    "Leave port and log file blank to keep the scope template values.",
                )
            ),
            inline=False,
        )
        embed.add_field(name="Selected Scope", value=state.app_name or "None", inline=False)
        if view.scopes:
            layout.add_text_select(
                self._build_state_action(AppManageActionKind.SELECT_CREATE_SCOPE, state),
                options=[
                    EditorSelectOption(
                        label=_component_text(scope),
                        value=scope,
                    )
                    for scope in view.scopes
                ],
                placeholder="Choose the app scope to extend",
            )
        layout.add_buttons(
            EditorButton(
                self._build_state_action(AppManageActionKind.OPEN_CREATE_MODAL, state),
                "Create Instance",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=(
                    state.app_name is None
                    or not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_CREATE_MODAL])
                ),
            )
        )
        layout.page_footer(
            self._action_codec.build(AppManageActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
            back_action=self._build_state_action(
                AppManageActionKind.BACK_LANDING,
                AppManageState(mode=AppManageMode.LANDING, page=state.page),
            ),
        )
        return embed, layout.build()

    def _render_home(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        assert state.app_name is not None
        app = manager.get(state.app_name)
        capabilities = _app_capabilities(app)
        is_running = app.check_running()

        embed = hikari.Embed(
            title=f"{'Error | ' if _status_is_error(status) else ''}{app.friendly} Manager",
            color=app.manage_embed_color,
        )
        embed.add_field(name="Status", value=_display_value(_app_status_lines(app)), inline=False)
        if app.supports_chat_relay:
            embed.add_field(name="Relay", value=_display_value(_app_relay_lines(app, manager)), inline=False)

        primary_buttons: list[EditorButton] = []
        management_buttons: list[EditorButton] = []
        relay_buttons: list[EditorButton] = []
        can_manage_relay = acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_RELAY_CHANNEL])
        primary_buttons.append(
            EditorButton(
                self._build_state_action(AppManageActionKind.TOGGLE_APP, state),
                "Disable App" if app.cfg.enabled else "Enable App",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=is_running
                or not acl.can(actor_user_id, _required_level_for_capability(AppManageCapability.TOGGLE)),
            )
        )
        if AppManageCapability.DOWNLOAD in capabilities:
            primary_buttons.append(
                EditorButton(
                    self._build_state_action(AppManageActionKind.DOWNLOAD_APP, state),
                    "Download",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=is_running
                    or not acl.can(actor_user_id, _required_level_for_capability(AppManageCapability.DOWNLOAD)),
                )
            )
        if AppManageCapability.UPDATE in capabilities:
            primary_buttons.append(
                EditorButton(
                    self._build_state_action(AppManageActionKind.UPDATE_APP, state),
                    "Update",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=is_running
                    or not acl.can(actor_user_id, _required_level_for_capability(AppManageCapability.UPDATE)),
                )
            )
        if app.mods is not None:
            management_buttons.append(
                EditorButton(
                    self._build_state_action(AppManageActionKind.OPEN_MODS, state),
                    "Manage Mods",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=is_running
                    or not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_MODS]),
                )
            )
        if app.settings is not None:
            management_buttons.append(
                EditorButton(
                    self._build_state_action(AppManageActionKind.OPEN_SETTINGS, state),
                    "Manage Settings",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=is_running
                    or not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_SETTINGS]),
                )
            )
        if app.supports_chat_relay:
            relay_buttons.append(
                EditorButton(
                    self._build_state_action(AppManageActionKind.OPEN_RELAY, state),
                    "Manage Relay",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=not can_manage_relay,
                )
            )
        layout.add_buttons(*primary_buttons)
        if management_buttons:
            layout.next_row().add_buttons(*management_buttons)
        if relay_buttons:
            layout.next_row().add_buttons(*relay_buttons)
        layout.page_footer(
            self._action_codec.build(AppManageActionKind.CLOSE, page=state.page),
            page_state=EditorPageState(page=0, total_pages=1),
            extra_buttons=(EditorButton(self._build_state_action(AppManageActionKind.REFRESH, state), "Refresh"),),
        )
        return embed, layout.build()

    def _render_relay(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        can_manage_relay = acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_RELAY_CHANNEL])
        if state.app_name is None:
            embed = hikari.Embed(
                title=f"{'Error | ' if _status_is_error(status) else ''}Default Relay",
                color=0x4B5563,
            )
            embed.add_field(name="Relay", value=_display_value(_default_relay_lines(manager)), inline=False)
            if can_manage_relay:
                layout.add_channel_select(
                    self._build_state_action(AppManageActionKind.SAVE_RELAY_CHANNEL, state),
                    channel_types=_RELAY_CHANNEL_TYPES,
                    placeholder="Choose the default relay channel",
                )
                layout.next_row().add_buttons(
                    EditorButton(
                        self._build_state_action(AppManageActionKind.CLEAR_RELAY_CHANNEL, state),
                        "Clear Default Relay",
                        style=hikari.ButtonStyle.SECONDARY,
                        is_disabled=manager.default_chat_channel is None,
                    )
                )
            layout.page_footer(
                self._action_codec.build(AppManageActionKind.CLOSE, page=0),
                page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
                back_action=self._build_state_action(
                    AppManageActionKind.BACK_LANDING,
                    AppManageState(mode=AppManageMode.LANDING, page=state.page),
                ),
                back_label="Back to Apps",
                extra_buttons=(EditorButton(self._build_state_action(AppManageActionKind.REFRESH, state), "Refresh"),),
            )
            return embed, layout.build()

        app = manager.get(state.app_name)
        embed = hikari.Embed(
            title=f"{'Error | ' if _status_is_error(status) else ''}{app.friendly} Relay",
            color=app.manage_embed_color,
        )
        embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
        if not app.supports_chat_relay:
            embed.add_field(name="Relay", value="Unsupported", inline=False)
        else:
            embed.add_field(name="Relay", value=_display_value(_app_relay_lines(app, manager)), inline=False)
        if can_manage_relay and app.supports_chat_relay:
            layout.add_channel_select(
                self._build_state_action(AppManageActionKind.SAVE_RELAY_CHANNEL, state),
                channel_types=_RELAY_CHANNEL_TYPES,
                placeholder=f"Choose a relay channel for {app.friendly}",
            )
            layout.next_row().add_buttons(
                EditorButton(
                    self._build_state_action(AppManageActionKind.CLEAR_RELAY_CHANNEL, state),
                    "Use Default Relay",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=app.chat_channel_override is None,
                )
            )
        layout.page_footer(
            self._action_codec.build(AppManageActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
            back_action=self._build_state_action(
                AppManageActionKind.BACK_HOME,
                AppManageState(mode=AppManageMode.HOME, page=state.page, app_name=app.name),
            ),
            back_label="Back to App",
            extra_buttons=(EditorButton(self._build_state_action(AppManageActionKind.REFRESH, state), "Refresh"),),
        )
        return embed, layout.build()

    def _render_mods(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        assert state.app_name is not None
        app = manager.get(state.app_name)
        if app.mods is None:
            raise _errors.UnsupportedModManager(app.friendly)

        view = self._build_mods_view(app=app, state=state)
        selected_mod = view.selected_mod
        footer_page_state = EditorPageState(
            page=view.mods.page_state.page,
            total_pages=view.mods.page_state.total_pages,
            is_subpage=True,
        )
        selected_state = AppManageState(
            mode=AppManageMode.MODS,
            page=view.mods.page_state.page,
            app_name=app.name,
            selected_page_slot=view.selected_mod_slot,
        )

        embed = hikari.Embed(
            title=f"{'Error | ' if _status_is_error(status) else ''}{app.friendly} Mods",
            color=app.manage_embed_color,
        )
        embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
        embed.add_field(name="Mods", value=_display_value(_mod_overview_lines(view)), inline=False)
        if selected_mod is not None:
            embed.add_field(
                name="Selected Mod",
                value=_display_value(_mod_status_lines(selected_mod, acl=acl, actor_user_id=actor_user_id)),
                inline=False,
            )
        embed.add_field(
            name="Upload",
            value="Click `Upload Mod`, then send one attachment in this channel. The first attachment from that message is used.",
            inline=False,
        )

        if view.mods.visible:
            layout.add_text_select(
                self._build_state_action(
                    AppManageActionKind.SELECT_MOD,
                    AppManageState(mode=AppManageMode.MODS, page=view.mods.page_state.page, app_name=app.name),
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(mod.friendly),
                        value=mod.name,
                        description=_component_text(_mod_option_description(mod)),
                    )
                    for mod in view.mods.visible
                ],
                placeholder="Choose a mod to manage",
            )

        can_manage_selected_mod = (
            selected_mod is not None
            and acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.TOGGLE_MOD])
            and (not selected_mod.cfg.coremod or acl.can(actor_user_id, acl.LvL.sudo))
        )
        can_remove_selected_mod = (
            selected_mod is not None
            and acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REMOVE_MOD])
            and (not selected_mod.cfg.coremod or acl.can(actor_user_id, acl.LvL.sudo))
        )
        can_toggle_coremod = selected_mod is not None and acl.can(
            actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.TOGGLE_COREMOD]
        )
        layout.add_buttons(
            EditorButton(
                self._build_state_action(AppManageActionKind.REQUEST_MOD_UPLOAD, selected_state),
                "Upload Mod",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=not acl.can(
                    actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REQUEST_MOD_UPLOAD]
                ),
            ),
            EditorButton(
                self._build_state_action(AppManageActionKind.DOWNLOAD_MOD, selected_state),
                "Download" if selected_mod is not None else "Download All",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=view.mods.total_count == 0
                or not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.DOWNLOAD_MOD]),
            ),
        )
        layout.next_row().add_buttons(
            EditorButton(
                self._build_state_action(AppManageActionKind.TOGGLE_MOD, selected_state),
                "Disable Mod" if selected_mod is not None and selected_mod.cfg.enabled else "Enable Mod",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=not can_manage_selected_mod,
            ),
            EditorButton(
                self._build_state_action(AppManageActionKind.TOGGLE_COREMOD, selected_state),
                "Unset Coremod" if selected_mod is not None and selected_mod.cfg.coremod else "Set Coremod",
                style=hikari.ButtonStyle.SECONDARY,
                is_disabled=not can_toggle_coremod,
            ),
            EditorButton(
                self._build_state_action(AppManageActionKind.REMOVE_MOD, selected_state),
                "Remove Mod",
                style=hikari.ButtonStyle.DANGER,
                is_disabled=not can_remove_selected_mod,
            ),
        )

        prev_action = None
        next_action = None
        if footer_page_state.total_pages > 1:
            prev_action = self._build_state_action(
                AppManageActionKind.PAGE,
                AppManageState(mode=AppManageMode.MODS, page=max(0, footer_page_state.page - 1), app_name=app.name),
            )
            next_action = self._build_state_action(
                AppManageActionKind.PAGE,
                AppManageState(
                    mode=AppManageMode.MODS,
                    page=min(footer_page_state.total_pages - 1, footer_page_state.page + 1),
                    app_name=app.name,
                ),
            )
        layout.page_footer(
            self._action_codec.build(AppManageActionKind.CLOSE, page=footer_page_state.page),
            page_state=footer_page_state,
            back_action=self._build_state_action(
                AppManageActionKind.BACK_HOME,
                AppManageState(mode=AppManageMode.HOME, page=footer_page_state.page, app_name=app.name),
            ),
            back_label="Back to App",
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._build_state_action(
                        AppManageActionKind.REFRESH,
                        AppManageState(mode=AppManageMode.MODS, page=footer_page_state.page, app_name=app.name),
                    ),
                    "Refresh Index",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REFRESH]),
                ),
            ),
        )
        return embed, layout.build()

    def _render_settings(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        assert state.app_name is not None
        app = manager.get(state.app_name)
        if app.settings is None:
            raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

        view = self._build_settings_view(app=app, state=state, acl=acl, actor_user_id=actor_user_id)
        selected_setting = view.selected_setting
        footer_page_state = EditorPageState(
            page=view.settings.page_state.page,
            total_pages=view.settings.page_state.total_pages,
            is_subpage=True,
        )
        selected_state = (
            self._state_for_setting(app=app, setting=selected_setting, page=view.settings.page_state.page)
            if selected_setting is not None
            else AppManageState(mode=AppManageMode.SETTINGS, page=view.settings.page_state.page, app_name=app.name)
        )

        embed = hikari.Embed(
            title=f"{'Error | ' if _status_is_error(status) else ''}{app.friendly} Settings",
            color=app.manage_embed_color,
        )
        embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
        embed.add_field(name="Settings", value=_display_value(_settings_overview_lines(view)), inline=False)
        if selected_setting is not None:
            embed.add_field(
                name="Selected Setting",
                value=_display_value(_setting_status_lines(selected_setting, acl=acl, actor_user_id=actor_user_id)),
                inline=False,
            )
        embed.add_field(
            name="Save Behaviour",
            value="Changes remain in memory until the app launches or `Save Settings` is used.",
            inline=False,
        )

        if view.settings.visible:
            layout.add_text_select(
                self._build_state_action(
                    AppManageActionKind.SELECT_SETTING,
                    AppManageState(mode=AppManageMode.SETTINGS, page=view.settings.page_state.page, app_name=app.name),
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(setting.label),
                        value=setting.key,
                        description=_component_text(_setting_option_description(setting)),
                    )
                    for setting in view.settings.visible
                ],
                placeholder="Choose a setting to manage",
            )

        can_edit_selected_setting = selected_setting is not None and _setting_can_edit(
            selected_setting, acl=acl, actor_user_id=actor_user_id
        )
        if (
            selected_setting is not None
            and can_edit_selected_setting
            and _setting_supports_choice_select(selected_setting)
        ):
            layout.add_text_select(
                self._build_state_action(AppManageActionKind.UPDATE_SETTING, selected_state),
                options=[
                    EditorSelectOption(
                        label=_component_text(label),
                        value=label,
                        description=_component_text(raw_value),
                    )
                    for label, raw_value in _setting_choice_items(selected_setting)
                ],
                placeholder=f"Choose a value for {selected_setting.label}",
            )

        layout.add_buttons(
            EditorButton(
                self._build_state_action(AppManageActionKind.WRITE_SETTING, selected_state),
                "Set Value",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=(
                    not can_edit_selected_setting
                    or (selected_setting is not None and not _setting_allows_modal_entry(selected_setting))
                ),
            ),
            EditorButton(
                self._build_state_action(AppManageActionKind.OPEN_SETTING_CHOICES, selected_state),
                "Browse Choices",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=not (
                    selected_setting is not None
                    and can_edit_selected_setting
                    and _setting_requires_choice_browser(selected_setting)
                ),
            ),
            EditorButton(
                self._build_state_action(AppManageActionKind.SAVE_SETTINGS, selected_state),
                "Save Settings",
                style=hikari.ButtonStyle.SECONDARY,
                is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_SETTINGS]),
            ),
        )

        prev_action = None
        next_action = None
        if footer_page_state.total_pages > 1:
            prev_action = self._build_state_action(
                AppManageActionKind.PAGE,
                AppManageState(mode=AppManageMode.SETTINGS, page=max(0, footer_page_state.page - 1), app_name=app.name),
            )
            next_action = self._build_state_action(
                AppManageActionKind.PAGE,
                AppManageState(
                    mode=AppManageMode.SETTINGS,
                    page=min(footer_page_state.total_pages - 1, footer_page_state.page + 1),
                    app_name=app.name,
                ),
            )
        layout.page_footer(
            self._action_codec.build(AppManageActionKind.CLOSE, page=footer_page_state.page),
            page_state=footer_page_state,
            back_action=self._build_state_action(
                AppManageActionKind.BACK_HOME,
                AppManageState(mode=AppManageMode.HOME, page=footer_page_state.page, app_name=app.name),
            ),
            back_label="Back to App",
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._build_state_action(
                        AppManageActionKind.REFRESH,
                        (
                            self._state_for_setting(app=app, setting=selected_setting, page=footer_page_state.page)
                            if selected_setting is not None
                            else AppManageState(
                                mode=AppManageMode.SETTINGS, page=footer_page_state.page, app_name=app.name
                            )
                        ),
                    ),
                    "Refresh",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REFRESH]),
                ),
            ),
        )
        return embed, layout.build()

    def _render_setting_choices(
        self,
        *,
        layout: EditorLayout,
        actor_user_id: int,
        acl: Access_Control,
        manager: App_Manager,
        state: AppManageState,
        status: str | None,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        assert state.app_name is not None
        app = manager.get(state.app_name)
        if app.settings is None:
            raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

        selected_setting = self._selected_setting(app=app, state=state)
        if selected_setting is None:
            return self._render_settings(
                layout=layout,
                actor_user_id=actor_user_id,
                acl=acl,
                manager=manager,
                state=AppManageState(mode=AppManageMode.SETTINGS, page=0, app_name=app.name),
                status="Error: selected setting is no longer available.",
            )

        view = self._build_setting_choices_view(setting=selected_setting, page=state.page)
        footer_page_state = EditorPageState(
            page=view.choices.page_state.page,
            total_pages=view.choices.page_state.total_pages,
            is_subpage=True,
        )
        selected_state = self._state_for_setting(
            app=app,
            setting=selected_setting,
            mode=AppManageMode.SETTING_CHOICES,
            page=view.choices.page_state.page,
        )

        embed = hikari.Embed(
            title=f"{'Error | ' if _status_is_error(status) else ''}{app.friendly} Setting Choices",
            color=app.manage_embed_color,
        )
        embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
        embed.add_field(
            name=selected_setting.label,
            value=_display_value(_setting_status_lines(selected_setting, acl=acl, actor_user_id=actor_user_id)),
            inline=False,
        )
        embed.add_field(
            name="Current Value",
            value=_setting_display_value(selected_setting),
            inline=False,
        )

        if view.choices.visible:
            layout.add_text_select(
                self._build_state_action(AppManageActionKind.UPDATE_SETTING, selected_state),
                options=[
                    EditorSelectOption(
                        label=_component_text(choice.label),
                        value=choice.label,
                        description=_component_text(choice.raw_value),
                    )
                    for choice in view.choices.visible
                ],
                placeholder=f"Choose a value for {selected_setting.label}",
            )

        layout.add_buttons(
            EditorButton(
                self._build_state_action(AppManageActionKind.SAVE_SETTINGS, selected_state),
                "Save Settings",
                style=hikari.ButtonStyle.SECONDARY,
                is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_SETTINGS]),
            )
        )

        prev_action = None
        next_action = None
        if footer_page_state.total_pages > 1:
            prev_action = self._build_state_action(
                AppManageActionKind.PAGE,
                self._state_for_setting(
                    app=app,
                    setting=selected_setting,
                    mode=AppManageMode.SETTING_CHOICES,
                    page=max(0, footer_page_state.page - 1),
                ),
            )
            next_action = self._build_state_action(
                AppManageActionKind.PAGE,
                self._state_for_setting(
                    app=app,
                    setting=selected_setting,
                    mode=AppManageMode.SETTING_CHOICES,
                    page=min(footer_page_state.total_pages - 1, footer_page_state.page + 1),
                ),
            )
        layout.page_footer(
            self._action_codec.build(AppManageActionKind.CLOSE, page=footer_page_state.page),
            page_state=footer_page_state,
            back_action=self._build_state_action(
                AppManageActionKind.BACK_SETTINGS,
                self._state_for_setting(app=app, setting=selected_setting),
            ),
            back_label="Back to Settings",
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._build_state_action(
                        AppManageActionKind.REFRESH,
                        self._state_for_setting(
                            app=app,
                            setting=selected_setting,
                            mode=AppManageMode.SETTING_CHOICES,
                            page=footer_page_state.page,
                        ),
                    ),
                    "Reload from Disk",
                    style=hikari.ButtonStyle.SECONDARY,
                    is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REFRESH]),
                ),
            ),
        )
        return embed, layout.build()

    def _build_landing_view(self, *, manager: App_Manager, state: AppManageState) -> LandingView:
        return LandingView(apps=_paginate(_all_apps(manager), state.page))

    def _build_create_view(self, *, manager: App_Manager) -> CreateView:
        return CreateView(scopes=manager.list_create_scopes())

    def _build_mods_view(self, *, app: App, state: AppManageState) -> ModsView:
        manager = app.has_mod_manager
        all_mods = tuple(manager.list_mods())
        mods = _paginate(all_mods, state.page)
        selected_mod_slot = state.selected_page_slot
        selected_mod = None
        if selected_mod_slot is not None and 0 <= selected_mod_slot < len(mods.visible):
            selected_mod = mods.visible[selected_mod_slot]
        else:
            selected_mod_slot = None
        enabled_count = sum(1 for mod in all_mods if mod.cfg.enabled)
        disabled_count = sum(1 for mod in all_mods if not mod.cfg.enabled)
        coremod_count = sum(1 for mod in all_mods if mod.cfg.coremod)
        return ModsView(
            mods=mods,
            enabled_count=enabled_count,
            disabled_count=disabled_count,
            coremod_count=coremod_count,
            selected_mod_slot=selected_mod_slot,
            selected_mod=selected_mod,
        )

    def _build_settings_view(
        self,
        *,
        app: App,
        state: AppManageState,
        acl: Access_Control,
        actor_user_id: int,
    ) -> SettingsView:
        if app.settings is None:
            raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

        all_settings = tuple(app.settings.app.options)
        settings = _paginate(all_settings, state.page)
        selected_setting_slot = None
        selected_setting = None
        if state.selected_setting_key is not None:
            selected_setting_slot = next(
                (index for index, setting in enumerate(settings.visible) if setting.key == state.selected_setting_key),
                None,
            )
            if selected_setting_slot is not None:
                selected_setting = settings.visible[selected_setting_slot]
        editable_count = sum(
            1 for setting in all_settings if _setting_can_edit(setting, acl=acl, actor_user_id=actor_user_id)
        )
        restricted_count = len(all_settings) - editable_count
        return SettingsView(
            settings=settings,
            editable_count=editable_count,
            restricted_count=restricted_count,
            selected_setting_slot=selected_setting_slot,
            selected_setting=selected_setting,
        )

    def _build_setting_choices_view(
        self,
        *,
        setting: Setting,
        page: int,
    ) -> SettingChoicesView:
        choices = _paginate(
            tuple(
                SettingChoiceEntry(label=label, raw_value=raw_value)
                for label, raw_value in _setting_choice_items(setting)
            ),
            page,
        )
        return SettingChoicesView(setting=setting, choices=choices)

    def _build_state_action(self, kind: AppManageActionKind, state: AppManageState | None) -> str:
        return self._action_codec.build(
            kind,
            page=0 if state is None else state.page,
            value=None if state is None else _state_value(state),
        )

    @staticmethod
    def _require_acl(deps: Mapping[str, object]) -> Access_Control:
        value = deps.get("acl")
        if not isinstance(value, Access_Control):
            raise TypeError("App manager requires Access_Control")
        return value

    @staticmethod
    def _require_manager(deps: Mapping[str, object]) -> App_Manager:
        value = deps.get("manager")
        if not isinstance(value, App_Manager):
            raise TypeError("App manager requires App_Manager")
        return value


@group_app.register
class CMD_AppStop(
    lightbulb.SlashCommand,
    name="stop",
    description="Stop the current app",
    hooks=[lightbulb.prefab.sliding_window(15, 1, "global")],
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, manager: App_Manager) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        await ctx.defer()
        log.info(f"App.Stop: {ctx.user.display_name}")

        details = await manager.end(manager.current)
        apps: list[str] = []
        for proc in details:
            try:
                apps.append(manager.get(proc).friendly)
            except ValueError:
                apps.append(proc)
        await ctx.respond(f"Ended: {', '.join(sorted(apps, key=str.lower))}" if apps else "No apps found running")


@group_app.register
class CMD_AppStart(
    lightbulb.SlashCommand,
    name="start",
    description="Start an enabled app",
    hooks=[lightbulb.prefab.sliding_window(30, 1, "global")],
):
    app = lightbulb.string("app", "Which app to start", autocomplete=ac_enabled_apps)  # type: ignore[reportAssignmentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        app_editor: AppManageService,
        manager: App_Manager,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        await ctx.defer()
        log.info(f"App.Start; {self.app}: {ctx.user.display_name}")

        app = manager.get(self.app)
        if reason := app_editor.start_lock_reason(app):
            await ctx.respond(reason)
            return
        if await manager.end():
            await asyncio.sleep(5)
        else:
            await asyncio.sleep(1)
        await manager.launch(app)
        await ctx.respond(_app_started_response_text(app))


@group_app.register
class CMD_AppManage(
    lightbulb.SlashCommand,
    name="manage",
    description="Open the app manager",
):
    app = lightbulb.string("app", "App to manage", autocomplete=ac_all_apps, default=None)  # type: ignore[reportAssignmentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        app_editor: AppManageService,
        manager: App_Manager,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        log.info(f"App.Manage; {self.app or '<landing>'}: {ctx.user.display_name}")
        if self.app is None:
            await app_editor.open_editor(ctx=ctx, acl=acl, manager=manager)
            return
        app = manager.get(self.app)
        if reason := app_editor.manage_lock_reason(app):
            await ctx.respond(reason, flags=hikari.MessageFlag.EPHEMERAL)
            return
        await app_editor.open_editor(ctx=ctx, acl=acl, manager=manager, initial_app=app)
