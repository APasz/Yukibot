from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import hikari
from hikari_ui import EditorButton, EditorLayout, EditorPageState, EditorSelectOption

import _errors
from _manager import App_Manager
from _security import Access_Control
from apps._app import App
from apps._settings import Setting
from cmd_app_manage import (
    APP_MANAGE_ACTION_LEVELS,
    AppManageActionKind,
    AppManageCapability,
    AppManageMode,
    AppManageState,
    CreateView,
    EditorStatus,
    LandingView,
    ModsView,
    RelayVoiceTargetService,
    SettingChoiceEntry,
    SettingChoicesView,
    SettingsView,
    _EMBED_SPACER,
    _EMBED_SUBTEXT,
    _PAGE_SIZE,
    _RELAY_CHANNEL_TYPES,
    _RELAY_VOICE_CHANNEL_TYPES,
    _all_apps,
    _app_capabilities,
    _app_extra_capability_labels,
    _app_option_description,
    _app_relay_lines,
    _app_status_lines,
    _app_summary_line,
    _component_text,
    _default_relay_lines,
    _display_value,
    _editor_title,
    _error_status,
    _mod_option_description,
    _mod_overview_lines,
    _mod_status_lines,
    _paginate,
    _required_level_for_capability,
    _setting_allows_modal_entry,
    _setting_can_edit,
    _setting_choice_items,
    _setting_display_value,
    _setting_option_description,
    _setting_recent_items,
    _setting_requires_choice_browser,
    _setting_status_lines,
    _setting_supports_choice_select,
    _setting_supports_recent_select,
    _settings_overview_lines,
)


class PagedActionBuilder(Protocol):
    def build(self, kind: AppManageActionKind, *, page: int, value: str | None = None) -> str: ...


class AppManageRenderHost(Protocol):
    _action_codec: PagedActionBuilder
    _voice_target_service: RelayVoiceTargetService | None

    def _build_state_action(self, kind: AppManageActionKind, state: AppManageState | None) -> str: ...

    def _selected_setting(self, *, app: App, state: AppManageState) -> Setting | None: ...

    def _state_for_setting(
        self,
        *,
        app: App,
        setting: Setting,
        mode: AppManageMode = AppManageMode.SETTINGS,
        page: int | None = None,
        actor_user_id: int | None = None,
    ) -> AppManageState: ...


def render_landing(
    host: AppManageRenderHost,
    *,
    layout: EditorLayout,
    actor_user_id: int,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    status: EditorStatus | None,
    current_guild_id: hikari.Snowflakeish | None = None,
) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
    view = build_landing_view(manager=manager, state=state)
    embed = hikari.Embed(
        title=_editor_title("App Manager", status=status),
        color=0x4B5563,
    )
    embed.description = "\n".join(
        [
            "Browse apps and relay defaults.",
            f"{_EMBED_SUBTEXT}Use the selector to open a specific app editor.",
        ]
    )
    embed.add_field(
        name="Relay",
        value=_display_value(
            _default_relay_lines(
                manager,
                current_guild_id=current_guild_id,
                voice_target_service=host._voice_target_service,
            )
        ),
        inline=True,
    )
    embed.add_field(name="Apps", value=f"{len(manager.apps)} loaded", inline=True)
    embed.add_field(name=_EMBED_SPACER, value=_EMBED_SPACER, inline=True)
    can_manage_relay = acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_RELAY_CHANNEL])

    if view.apps.visible:
        layout.add_text_select(
            host._build_state_action(
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
            host._build_state_action(AppManageActionKind.OPEN_RELAY, state),
            "Manage Relay",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=not can_manage_relay,
        ),
    )

    prev_action = None
    next_action = None
    if view.apps.page_state.total_pages > 1:
        prev_action = host._build_state_action(
            AppManageActionKind.PAGE,
            AppManageState(mode=AppManageMode.LANDING, page=max(0, view.apps.page_state.page - 1)),
        )
        next_action = host._build_state_action(
            AppManageActionKind.PAGE,
            AppManageState(
                mode=AppManageMode.LANDING,
                page=min(view.apps.page_state.total_pages - 1, view.apps.page_state.page + 1),
            ),
        )
    layout.page_footer(
        host._action_codec.build(AppManageActionKind.CLOSE, page=0),
        page_state=view.apps.page_state,
        prev_action=prev_action,
        next_action=next_action,
        extra_buttons=(
            EditorButton(
                host._build_state_action(AppManageActionKind.OPEN_CREATE, state),
                "Create",
                style=hikari.ButtonStyle.SECONDARY,
                is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_CREATE]),
            ),
        ),
    )
    return embed, layout.build()


def render_create(
    host: AppManageRenderHost,
    *,
    layout: EditorLayout,
    actor_user_id: int,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    status: EditorStatus | None,
) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
    view = build_create_view(manager=manager)
    instruction_lines = [
        "This writes a new instance entry under `apps/<scope>/instances.json`.",
        "The app stays hidden until the bot is restarted.",
        "Use a subfolder path relative to `DIR_APP` only.",
        "Leave port and log file blank to keep the scope template values.",
    ]
    if state.app_name == "satisfactory":
        instruction_lines[-1] = "Leave port blank to keep the scope template value."
        instruction_lines.append("Satisfactory requires an admin password during creation.")
    embed = hikari.Embed(
        title=_editor_title("Create App Instance", status=status),
        color=0x4B5563,
    )
    embed.description = "\n".join(
        [
            "Create a new instance entry.",
            f"{_EMBED_SUBTEXT}This updates `instances.json`; provisioning still happens outside the bot.",
        ]
    )
    embed.add_field(
        name="Instructions",
        value=_display_value(instruction_lines),
        inline=False,
    )
    embed.add_field(name="Scope", value=state.app_name or "None", inline=False)
    if view.scopes:
        layout.add_text_select(
            host._build_state_action(AppManageActionKind.SELECT_CREATE_SCOPE, state),
            options=[EditorSelectOption(label=_component_text(scope), value=scope) for scope in view.scopes],
            placeholder="Choose the app scope to extend",
        )
    layout.add_buttons(
        EditorButton(
            host._build_state_action(AppManageActionKind.OPEN_CREATE_MODAL, state),
            "Create Instance",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=(
                state.app_name is None
                or not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_CREATE_MODAL])
            ),
        )
    )
    layout.page_footer(
        host._action_codec.build(AppManageActionKind.CLOSE, page=0),
        page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
        back_action=host._build_state_action(
            AppManageActionKind.BACK_LANDING,
            AppManageState(mode=AppManageMode.LANDING, page=state.page),
        ),
    )
    return embed, layout.build()


def render_home(
    host: AppManageRenderHost,
    *,
    layout: EditorLayout,
    actor_user_id: int,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    status: EditorStatus | None,
    current_guild_id: hikari.Snowflakeish | None = None,
) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
    assert state.app_name is not None
    app = manager.get(state.app_name)
    capabilities = _app_capabilities(app)

    embed = hikari.Embed(
        title=_editor_title(f"{app.friendly} Manager", status=status),
        color=app.manage_embed_color,
    )
    embed.description = "\n".join(
        [
            _app_summary_line(app),
            f"{_EMBED_SUBTEXT}{', '.join(_app_extra_capability_labels(app)) or 'No extra actions'}",
        ]
    )
    embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
    if app.supports_chat_relay:
        embed.add_field(
            name="Relay",
            value=_display_value(
                _app_relay_lines(
                    app,
                    manager,
                    current_guild_id=current_guild_id,
                    voice_target_service=host._voice_target_service,
                )
            ),
            inline=False,
        )

    primary_buttons: list[EditorButton] = []
    management_buttons: list[EditorButton] = []
    relay_buttons: list[EditorButton] = []
    can_manage_relay = acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_RELAY_CHANNEL])
    primary_buttons.append(
        EditorButton(
            host._build_state_action(AppManageActionKind.TOGGLE_APP, state),
            "Disable App" if app.cfg.enabled else "Enable App",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=not acl.can(actor_user_id, _required_level_for_capability(AppManageCapability.TOGGLE)),
        )
    )
    if AppManageCapability.DOWNLOAD in capabilities:
        primary_buttons.append(
            EditorButton(
                host._build_state_action(AppManageActionKind.DOWNLOAD_APP, state),
                "Download",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=not acl.can(actor_user_id, _required_level_for_capability(AppManageCapability.DOWNLOAD)),
            )
        )
    if AppManageCapability.UPDATE in capabilities:
        primary_buttons.append(
            EditorButton(
                host._build_state_action(AppManageActionKind.UPDATE_APP, state),
                "Update",
                style=hikari.ButtonStyle.PRIMARY,
                is_disabled=not acl.can(actor_user_id, _required_level_for_capability(AppManageCapability.UPDATE)),
            )
        )
    if app.mods is not None:
        management_buttons.append(
            EditorButton(
                host._build_state_action(AppManageActionKind.OPEN_MODS, state),
                "Manage Mods",
                style=hikari.ButtonStyle.SECONDARY,
                is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_MODS]),
            )
        )
    if app.settings is not None:
        management_buttons.append(
            EditorButton(
                host._build_state_action(AppManageActionKind.OPEN_SETTINGS, state),
                "Manage Settings",
                style=hikari.ButtonStyle.SECONDARY,
                is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_SETTINGS]),
            )
        )
    if app.supports_chat_relay:
        relay_buttons.append(
            EditorButton(
                host._build_state_action(AppManageActionKind.OPEN_RELAY, state),
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
        host._action_codec.build(AppManageActionKind.CLOSE, page=state.page),
        page_state=EditorPageState(page=0, total_pages=1),
        extra_buttons=(EditorButton(host._build_state_action(AppManageActionKind.REFRESH, state), "Refresh"),),
    )
    return embed, layout.build()


def render_relay(
    host: AppManageRenderHost,
    *,
    layout: EditorLayout,
    actor_user_id: int,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    status: EditorStatus | None,
    current_guild_id: hikari.Snowflakeish | None = None,
) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
    can_manage_relay = acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.SAVE_RELAY_CHANNEL])
    if state.app_name is None:
        embed = hikari.Embed(
            title=_editor_title("Default Relay", status=status),
            color=0x4B5563,
        )
        description_lines = [
            "Default relay routing.",
            f"{_EMBED_SUBTEXT}Apps without an override use these guild defaults.",
        ]
        if current_guild_id is None:
            description_lines.append(f"{_EMBED_SUBTEXT}Open this editor in a server to manage guild relay channels.")
        else:
            description_lines.append(f"{_EMBED_SUBTEXT}Selections below only change relay routing for this server.")
            if host._voice_target_service is None:
                description_lines.append(f"{_EMBED_SUBTEXT}Voice relay editing is unavailable on this node.")
        embed.description = "\n".join(description_lines)
        embed.add_field(
            name="Relay",
            value=_display_value(
                _default_relay_lines(
                    manager,
                    current_guild_id=current_guild_id,
                    voice_target_service=host._voice_target_service,
                )
            ),
            inline=False,
        )
        if can_manage_relay and current_guild_id is not None:
            layout.add_channel_select(
                host._build_state_action(AppManageActionKind.SAVE_RELAY_CHANNEL, state),
                channel_types=_RELAY_CHANNEL_TYPES,
                placeholder="Choose default relay text channel for this server",
            )
            if host._voice_target_service is not None:
                layout.add_channel_select(
                    host._build_state_action(AppManageActionKind.SAVE_RELAY_VOICE_CHANNEL, state),
                    channel_types=_RELAY_VOICE_CHANNEL_TYPES,
                    placeholder="Choose default relay voice channel for this server",
                )
            layout.next_row().add_buttons(
                EditorButton(
                    host._build_state_action(AppManageActionKind.CLEAR_RELAY_CHANNEL, state),
                    "Remove This Guild Default",
                    style=hikari.ButtonStyle.SECONDARY,
                )
            )
        layout.page_footer(
            host._action_codec.build(AppManageActionKind.CLOSE, page=0),
            page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
            back_action=host._build_state_action(
                AppManageActionKind.BACK_LANDING,
                AppManageState(mode=AppManageMode.LANDING, page=state.page),
            ),
            back_label="Back",
            extra_buttons=(EditorButton(host._build_state_action(AppManageActionKind.REFRESH, state), "Refresh"),),
        )
        return embed, layout.build()

    app = manager.get(state.app_name)
    embed = hikari.Embed(
        title=_editor_title(f"{app.friendly} Relay", status=status),
        color=app.manage_embed_color,
    )
    description_lines = [
        _app_summary_line(app),
        f"{_EMBED_SUBTEXT}Relay routing for this app.",
    ]
    if current_guild_id is None:
        description_lines.append(f"{_EMBED_SUBTEXT}Open this editor in a server to manage guild relay channels.")
    else:
        description_lines.append(f"{_EMBED_SUBTEXT}Selections below only change relay routing for this server.")
        if host._voice_target_service is None:
            description_lines.append(f"{_EMBED_SUBTEXT}Voice relay editing is unavailable on this node.")
    embed.description = "\n".join(description_lines)
    embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
    if not app.supports_chat_relay:
        embed.add_field(name="Relay", value="Unsupported", inline=False)
    else:
        embed.add_field(
            name="Relay",
            value=_display_value(
                _app_relay_lines(
                    app,
                    manager,
                    current_guild_id=current_guild_id,
                    voice_target_service=host._voice_target_service,
                )
            ),
            inline=False,
        )
    if can_manage_relay and app.supports_chat_relay:
        relay_buttons: list[EditorButton] = []
        if current_guild_id is not None:
            layout.add_channel_select(
                host._build_state_action(AppManageActionKind.SAVE_RELAY_CHANNEL, state),
                channel_types=_RELAY_CHANNEL_TYPES,
                placeholder=f"Choose relay text channel for {app.friendly}",
            )
            if host._voice_target_service is not None:
                layout.add_channel_select(
                    host._build_state_action(AppManageActionKind.SAVE_RELAY_VOICE_CHANNEL, state),
                    channel_types=_RELAY_VOICE_CHANNEL_TYPES,
                    placeholder=f"Choose relay voice channel for {app.friendly}",
                )
            relay_buttons.append(
                EditorButton(
                    host._build_state_action(AppManageActionKind.CLEAR_RELAY_CHANNEL, state),
                    "Remove This Guild Relay",
                    style=hikari.ButtonStyle.SECONDARY,
                )
            )
        if app.supports_relay_advancements:
            relay_buttons.append(
                EditorButton(
                    host._build_state_action(AppManageActionKind.TOGGLE_RELAY_ADVANCEMENTS, state),
                    (
                        f"Disable {app.relay_advancement_term_plural}"
                        if app.relay_advancements_enabled
                        else f"Enable {app.relay_advancement_term_plural}"
                    ),
                    style=hikari.ButtonStyle.SECONDARY,
                )
            )
        if relay_buttons:
            layout.next_row().add_buttons(*relay_buttons)
    layout.page_footer(
        host._action_codec.build(AppManageActionKind.CLOSE, page=0),
        page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
        back_action=host._build_state_action(
            AppManageActionKind.BACK_HOME,
            AppManageState(mode=AppManageMode.HOME, page=state.page, app_name=app.name),
        ),
        back_label="Back",
        extra_buttons=(EditorButton(host._build_state_action(AppManageActionKind.REFRESH, state), "Refresh"),),
    )
    return embed, layout.build()


def render_mods(
    host: AppManageRenderHost,
    *,
    layout: EditorLayout,
    actor_user_id: int,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    status: EditorStatus | None,
) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
    assert state.app_name is not None
    app = manager.get(state.app_name)
    if app.mods is None:
        raise _errors.UnsupportedModManager(app.friendly)

    view = build_mods_view(app=app, state=state)
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
        title=_editor_title(f"{app.friendly} Mods", status=status),
        color=app.manage_embed_color,
    )
    embed.description = "\n".join(
        [
            _app_summary_line(app),
            f"{_EMBED_SUBTEXT}Mod index, uploads, and per-mod actions.",
        ]
    )
    embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
    embed.add_field(name="Mods", value=_display_value(_mod_overview_lines(view)), inline=True)
    if selected_mod is not None:
        embed.add_field(
            name="Selected Mod",
            value=_display_value(_mod_status_lines(selected_mod, acl=acl, actor_user_id=actor_user_id)),
            inline=True,
        )
    else:
        embed.add_field(name=_EMBED_SPACER, value=_EMBED_SPACER, inline=True)
    embed.add_field(
        name="Upload",
        value="\n".join(
            [
                "Click `Upload Mod`, then send one attachment in this channel.",
                f"{_EMBED_SUBTEXT}The first attachment in that message is used.",
            ]
        ),
        inline=False,
    )

    if view.mods.visible:
        layout.add_text_select(
            host._build_state_action(
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
        and (not selected_mod.is_protected or acl.can(actor_user_id, acl.LvL.sudo))
    )
    can_remove_selected_mod = (
        selected_mod is not None
        and acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REMOVE_MOD])
        and (not selected_mod.is_protected or acl.can(actor_user_id, acl.LvL.sudo))
    )
    can_toggle_coremod = (
        selected_mod is not None
        and not selected_mod.is_builtin
        and acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.TOGGLE_COREMOD])
    )
    can_toggle_downloadable = (
        selected_mod is not None
        and selected_mod.default_download_block_reason() is None
        and acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.TOGGLE_DOWNLOADABLE])
    )
    layout.add_buttons(
        EditorButton(
            host._build_state_action(AppManageActionKind.REQUEST_MOD_UPLOAD, selected_state),
            "Upload Mod",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REQUEST_MOD_UPLOAD]),
        ),
        EditorButton(
            host._build_state_action(AppManageActionKind.DOWNLOAD_MOD, selected_state),
            "Download" if selected_mod is not None else "Download All",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=(selected_mod is not None and not selected_mod.downloadable)
            or (selected_mod is None and view.downloadable_count == 0)
            or not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.DOWNLOAD_MOD]),
        ),
        EditorButton(
            host._build_state_action(AppManageActionKind.OPEN_MOD_WEB, selected_state),
            "Web",
            style=hikari.ButtonStyle.SECONDARY,
            is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.OPEN_MOD_WEB]),
        ),
    )
    layout.next_row().add_buttons(
        EditorButton(
            host._build_state_action(AppManageActionKind.TOGGLE_MOD, selected_state),
            "Disable Mod" if selected_mod is not None and selected_mod.cfg.enabled else "Enable Mod",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=not can_manage_selected_mod,
        ),
        EditorButton(
            host._build_state_action(AppManageActionKind.TOGGLE_COREMOD, selected_state),
            "Unset Coremod" if selected_mod is not None and selected_mod.is_coremod_type else "Set Coremod",
            style=hikari.ButtonStyle.SECONDARY,
            is_disabled=not can_toggle_coremod,
        ),
        EditorButton(
            host._build_state_action(AppManageActionKind.TOGGLE_DOWNLOADABLE, selected_state),
            "Block Download" if selected_mod is not None and selected_mod.downloadable else "Allow Download",
            style=hikari.ButtonStyle.SECONDARY,
            is_disabled=not can_toggle_downloadable,
        ),
        EditorButton(
            host._build_state_action(AppManageActionKind.REMOVE_MOD, selected_state),
            "Remove Mod",
            style=hikari.ButtonStyle.DANGER,
            is_disabled=not can_remove_selected_mod,
        ),
    )

    prev_action = None
    next_action = None
    if footer_page_state.total_pages > 1:
        prev_action = host._build_state_action(
            AppManageActionKind.PAGE,
            AppManageState(mode=AppManageMode.MODS, page=max(0, footer_page_state.page - 1), app_name=app.name),
        )
        next_action = host._build_state_action(
            AppManageActionKind.PAGE,
            AppManageState(
                mode=AppManageMode.MODS,
                page=min(footer_page_state.total_pages - 1, footer_page_state.page + 1),
                app_name=app.name,
            ),
        )
    layout.page_footer(
        host._action_codec.build(AppManageActionKind.CLOSE, page=footer_page_state.page),
        page_state=footer_page_state,
        back_action=host._build_state_action(
            AppManageActionKind.BACK_HOME,
            AppManageState(mode=AppManageMode.HOME, page=footer_page_state.page, app_name=app.name),
        ),
        back_label="Back",
        prev_action=prev_action,
        next_action=next_action,
        extra_buttons=(
            EditorButton(
                host._build_state_action(
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


def render_settings(
    host: AppManageRenderHost,
    *,
    layout: EditorLayout,
    actor_user_id: int,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    status: EditorStatus | None,
) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
    assert state.app_name is not None
    app = manager.get(state.app_name)
    if app.settings is None:
        raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

    view = build_settings_view(app=app, state=state, acl=acl, actor_user_id=actor_user_id)
    selected_setting = view.selected_setting
    footer_page_state = EditorPageState(
        page=view.settings.page_state.page,
        total_pages=view.settings.page_state.total_pages,
        is_subpage=True,
    )
    selected_state = (
        host._state_for_setting(app=app, setting=selected_setting, page=view.settings.page_state.page)
        if selected_setting is not None
        else AppManageState(mode=AppManageMode.SETTINGS, page=view.settings.page_state.page, app_name=app.name)
    )

    embed = hikari.Embed(
        title=_editor_title(f"{app.friendly} Settings", status=status),
        color=app.manage_embed_color,
    )
    embed.description = "\n".join(
        [
            _app_summary_line(app),
            f"{_EMBED_SUBTEXT}Review, edit, and save runtime settings.",
        ]
    )
    embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=True)
    embed.add_field(name="Settings", value=_display_value(_settings_overview_lines(view)), inline=True)
    if selected_setting is not None:
        embed.add_field(
            name="Selected Setting",
            value=_display_value(
                _setting_status_lines(
                    selected_setting,
                    settings_manager=app.settings,
                    acl=acl,
                    actor_user_id=actor_user_id,
                )
            ),
            inline=False,
        )
    else:
        embed.add_field(name=_EMBED_SPACER, value=_EMBED_SPACER, inline=True)
    embed.add_field(
        name="Save",
        value="\n".join(
            [
                "Changes stay in memory until launch or `Save Settings`.",
                f"{_EMBED_SUBTEXT}Use save when you need to persist before the next launch.",
            ]
        ),
        inline=False,
    )

    if view.settings.visible:
        layout.add_text_select(
            host._build_state_action(
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
            host._build_state_action(AppManageActionKind.UPDATE_SETTING, selected_state),
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

    if (
        selected_setting is not None
        and can_edit_selected_setting
        and _setting_supports_recent_select(selected_setting)
    ):
        layout.add_text_select(
            host._build_state_action(AppManageActionKind.REUSE_SETTING, selected_state),
            options=[
                EditorSelectOption(
                    label=_component_text(value),
                    value=str(index),
                    description="Recent value",
                )
                for index, value in enumerate(_setting_recent_items(selected_setting))
            ],
            placeholder=f"Reuse a recent value for {selected_setting.label}",
        )

    layout.add_buttons(
        EditorButton(
            host._build_state_action(AppManageActionKind.WRITE_SETTING, selected_state),
            "Set Value",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=(
                not can_edit_selected_setting
                or (selected_setting is not None and not _setting_allows_modal_entry(selected_setting))
            ),
        ),
        EditorButton(
            host._build_state_action(AppManageActionKind.OPEN_SETTING_CHOICES, selected_state),
            "Browse Choices",
            style=hikari.ButtonStyle.PRIMARY,
            is_disabled=not (
                selected_setting is not None
                and can_edit_selected_setting
                and _setting_requires_choice_browser(selected_setting)
            ),
        ),
        EditorButton(
            host._build_state_action(AppManageActionKind.SAVE_SETTINGS, selected_state),
            "Save Settings",
            style=hikari.ButtonStyle.SECONDARY,
            is_disabled=not acl.can(actor_user_id, app.settings_save_level(actor_user_id)),
        ),
    )

    prev_action = None
    next_action = None
    if footer_page_state.total_pages > 1:
        prev_action = host._build_state_action(
            AppManageActionKind.PAGE,
            AppManageState(mode=AppManageMode.SETTINGS, page=max(0, footer_page_state.page - 1), app_name=app.name),
        )
        next_action = host._build_state_action(
            AppManageActionKind.PAGE,
            AppManageState(
                mode=AppManageMode.SETTINGS,
                page=min(footer_page_state.total_pages - 1, footer_page_state.page + 1),
                app_name=app.name,
            ),
        )
    layout.page_footer(
        host._action_codec.build(AppManageActionKind.CLOSE, page=footer_page_state.page),
        page_state=footer_page_state,
        back_action=host._build_state_action(
            AppManageActionKind.BACK_HOME,
            AppManageState(mode=AppManageMode.HOME, page=footer_page_state.page, app_name=app.name),
        ),
        back_label="Back",
        prev_action=prev_action,
        next_action=next_action,
        extra_buttons=(
            EditorButton(
                host._build_state_action(
                    AppManageActionKind.REFRESH,
                    (
                        host._state_for_setting(app=app, setting=selected_setting, page=footer_page_state.page)
                        if selected_setting is not None
                        else AppManageState(mode=AppManageMode.SETTINGS, page=footer_page_state.page, app_name=app.name)
                    ),
                ),
                "Refresh",
                style=hikari.ButtonStyle.SECONDARY,
                is_disabled=not acl.can(actor_user_id, APP_MANAGE_ACTION_LEVELS[AppManageActionKind.REFRESH]),
            ),
        ),
    )
    return embed, layout.build()


def render_setting_choices(
    host: AppManageRenderHost,
    *,
    layout: EditorLayout,
    actor_user_id: int,
    acl: Access_Control,
    manager: App_Manager,
    state: AppManageState,
    status: EditorStatus | None,
) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
    assert state.app_name is not None
    app = manager.get(state.app_name)
    if app.settings is None:
        raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

    selected_setting = host._selected_setting(app=app, state=state)
    if selected_setting is None:
        return render_settings(
            host,
            layout=layout,
            actor_user_id=actor_user_id,
            acl=acl,
            manager=manager,
            state=AppManageState(mode=AppManageMode.SETTINGS, page=0, app_name=app.name),
            status=_error_status("Error: selected setting is no longer available."),
        )

    view = build_setting_choices_view(setting=selected_setting, page=state.page)
    footer_page_state = EditorPageState(
        page=view.choices.page_state.page,
        total_pages=view.choices.page_state.total_pages,
        is_subpage=True,
    )
    selected_state = host._state_for_setting(
        app=app,
        setting=selected_setting,
        mode=AppManageMode.SETTING_CHOICES,
        page=view.choices.page_state.page,
    )

    embed = hikari.Embed(
        title=_editor_title(f"{app.friendly} Setting Choices", status=status),
        color=app.manage_embed_color,
    )
    embed.description = "\n".join(
        [
            _app_summary_line(app),
            f"{_EMBED_SUBTEXT}Choose from the available values for this setting.",
        ]
    )
    embed.add_field(name="App", value=_display_value(_app_status_lines(app)), inline=False)
    embed.add_field(
        name=selected_setting.label,
        value=_display_value(
            _setting_status_lines(
                selected_setting,
                settings_manager=app.settings,
                acl=acl,
                actor_user_id=actor_user_id,
            )
        ),
        inline=False,
    )
    embed.add_field(
        name="Current Value",
        value=_setting_display_value(selected_setting, settings_manager=app.settings, actor_user_id=actor_user_id),
        inline=False,
    )

    if view.choices.visible:
        layout.add_text_select(
            host._build_state_action(AppManageActionKind.UPDATE_SETTING, selected_state),
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
            host._build_state_action(AppManageActionKind.SAVE_SETTINGS, selected_state),
            "Save Settings",
            style=hikari.ButtonStyle.SECONDARY,
            is_disabled=not acl.can(actor_user_id, app.settings_save_level(actor_user_id)),
        )
    )

    prev_action = None
    next_action = None
    if footer_page_state.total_pages > 1:
        prev_action = host._build_state_action(
            AppManageActionKind.PAGE,
            host._state_for_setting(
                app=app,
                setting=selected_setting,
                mode=AppManageMode.SETTING_CHOICES,
                page=max(0, footer_page_state.page - 1),
            ),
        )
        next_action = host._build_state_action(
            AppManageActionKind.PAGE,
            host._state_for_setting(
                app=app,
                setting=selected_setting,
                mode=AppManageMode.SETTING_CHOICES,
                page=min(footer_page_state.total_pages - 1, footer_page_state.page + 1),
            ),
        )
    layout.page_footer(
        host._action_codec.build(AppManageActionKind.CLOSE, page=footer_page_state.page),
        page_state=footer_page_state,
        back_action=host._build_state_action(
            AppManageActionKind.BACK_SETTINGS,
            host._state_for_setting(app=app, setting=selected_setting),
        ),
        back_label="Back to Settings",
        prev_action=prev_action,
        next_action=next_action,
        extra_buttons=(
            EditorButton(
                host._build_state_action(
                    AppManageActionKind.REFRESH,
                    host._state_for_setting(
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


def build_landing_view(*, manager: App_Manager, state: AppManageState) -> LandingView:
    return LandingView(apps=_paginate(_all_apps(manager), state.page))


def build_create_view(*, manager: App_Manager) -> CreateView:
    return CreateView(scopes=manager.list_create_scopes())


def build_mods_view(*, app: App, state: AppManageState) -> ModsView:
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
    coremod_count = sum(1 for mod in all_mods if mod.counts_as_coremod)
    downloadable_count = sum(1 for mod in all_mods if mod.downloadable)
    return ModsView(
        mods=mods,
        enabled_count=enabled_count,
        disabled_count=disabled_count,
        coremod_count=coremod_count,
        downloadable_count=downloadable_count,
        selected_mod_slot=selected_mod_slot,
        selected_mod=selected_mod,
    )


def build_settings_view(
    *,
    app: App,
    state: AppManageState,
    acl: Access_Control,
    actor_user_id: int,
) -> SettingsView:
    if app.settings is None:
        raise _errors.UnsupportedSettings(f"{app.friendly} does not support settings")

    all_settings: Sequence[Setting] = tuple(app.settings.app.options)
    settings = _paginate(all_settings, state.page)
    selected_setting_slot = None
    selected_setting = None
    if state.selected_setting_index is not None:
        page_start = settings.page_state.page * _PAGE_SIZE
        page_end = page_start + len(settings.visible)
        if page_start <= state.selected_setting_index < page_end:
            selected_setting_slot = state.selected_setting_index - page_start
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


def build_setting_choices_view(
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
