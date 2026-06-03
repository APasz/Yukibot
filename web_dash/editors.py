from __future__ import annotations

from .runtime_imports import (
    Access_Control,
    App_Manager,
    BadgeTone,
    BotMetadataModWeb,
    Button,
    Callable,
    CodeMirror,
    GatewayBot,
    Input,
    Label,
    Literal,
    LiteralString,
    Mapping,
    ModWebUser,
    NodeApiScope,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeConfigContent,
    NodeConfigEntry,
    NodeConfigList,
    NodeConsoleActionEntry,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleActionParameter,
    NodeModUploadResult,
    NodeSaveEntry,
    NodeSaveList,
    NodeSaveMutationResult,
    NodeSettingChoice,
    NodeSettingEntry,
    NodeSettingList,
    NodeSettingMutationResult,
    NodeSettingsActionResult,
    OwnUser,
    Path,
    Power_Level,
    Protocol,
    PurePosixPath,
    Select,
    Timer,
    asyncio,
    cached_member_role_color,
    cast,
    color_int_to_hex,
    config,
    escape,
    hashlib,
    hikari,
    quote,
)
from .constants import (
    _CONFIG_EDITOR_DOCKERFILE_LANGUAGE,
    _CONFIG_EDITOR_LANGUAGE_BY_SUFFIX,
    _CONFIG_EDITOR_THEME,
    _HIDDEN_SETTING_CYCLE_VARIANT_COUNT,
    _HIDDEN_SETTING_GLYPHS,
    _SAME_ORIGIN_NODE_API_BASE,
    _SAME_ORIGIN_NODE_PROXY_BASE,
    log,
)
from .nicegui_protocols import (
    ModWebEventArgumentsContainer,
    ModWebUi,
    ModWebValueContainer,
    _event_args_as_text,
    _value_as_object,
    _value_as_text,
)
from .types import (
    ModWebBasePageModel,
    ModWebConfigEditorLayout,
    ModWebConfigEditorShape,
    ModWebNodeLink,
    ModWebPageModel,
    ModWebSearchOption,
    ModWebSettingControlKind,
    _ModWebBadgeSpec,
    _SettingSecretConfig,
)

from .service_base import ModWebServiceSupport

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nicegui.elements.codemirror.codemirror import SUPPORTED_LANGUAGES
    from nicegui.elements.dialog import Dialog
    from nicegui.elements.switch import Switch
    from nicegui.elements.upload_files import FileUpload
    from nicegui.events import UploadEventArguments


class _ModWebSelectOptionsControl(Protocol):
    def set_options(self, options: dict[str, str], *, value: str | None = None) -> None: ...


class ModWebEditorsMixin(ModWebServiceSupport):
    def _render_console_editor(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> Callable[[ModWebBasePageModel], None] | None:
        if model.console_actions is None:
            return None
        if not model.console_actions.actions:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Console",
                description="No curated console actions are currently exposed for this app.",
                detail_text="Add or enable console actions in the app definition to populate this tab.",
            )
            return None

        current_model: ModWebBasePageModel = model
        current_console_actions: NodeConsoleActionList = model.console_actions
        selected_action_key: str = current_console_actions.actions[0].key
        draft_values: dict[str, str] = {}
        last_result: NodeConsoleActionExecutionResult | None = None
        last_result_action_key: str | None = None
        action_in_flight = False
        open_console_popup_count = 0
        queued_console_refresh = False

        def selected_action() -> NodeConsoleActionEntry:
            nonlocal selected_action_key
            action_by_key: dict[str, NodeConsoleActionEntry] = {
                action.key: action for action in current_console_actions.actions
            }
            resolved_action: NodeConsoleActionEntry | None = action_by_key.get(selected_action_key)
            if resolved_action is not None:
                return resolved_action
            fallback_action: NodeConsoleActionEntry = current_console_actions.actions[0]
            selected_action_key = fallback_action.key
            return fallback_action

        def refresh_console_body(*, force: bool = False) -> None:
            nonlocal queued_console_refresh
            if not force and open_console_popup_count > 0:
                queued_console_refresh = True
                return
            queued_console_refresh = False
            _console_card_body.refresh()

        def bind_console_popup_refresh_lock(select_control: Select) -> Select:
            def popup_shown(_: object | None = None) -> None:
                nonlocal open_console_popup_count
                open_console_popup_count += 1

            def popup_hidden(_: object | None = None) -> None:
                nonlocal open_console_popup_count, queued_console_refresh
                open_console_popup_count = max(0, open_console_popup_count - 1)
                if open_console_popup_count == 0 and queued_console_refresh:
                    queued_console_refresh = False
                    _console_card_body.refresh()

            select_control.on("popup-show", popup_shown)
            select_control.on("popup-hide", popup_hidden)
            return select_control

        def select_action(event: ModWebValueContainer) -> None:
            nonlocal selected_action_key
            next_action_key: str = _value_as_text(event).strip()
            if not next_action_key:
                return
            selected_action_key = next_action_key
            refresh_console_body(force=True)

        async def run_selected_action() -> None:
            nonlocal action_in_flight, current_console_actions, last_result, last_result_action_key
            if action_in_flight:
                return
            action: NodeConsoleActionEntry = selected_action()
            if not self._console_action_can_execute(action=action, app_stats=current_model.app_stats):
                ui.notify(
                    self._console_action_status_text(
                        action=action,
                        app_friendly=current_model.app_friendly,
                        app_stats=current_model.app_stats,
                    ),
                    type="warning",
                )
                return
            action_in_flight = True
            refresh_console_body(force=True)
            raw_value: str | None = draft_values.get(action.key)
            if raw_value is not None:
                raw_value = raw_value.strip() or None
            try:
                try:
                    result: NodeConsoleActionExecutionResult = await self._execute_console_action(
                        model=current_model,
                        action_key=action.key,
                        raw_value=raw_value,
                        user=user,
                    )
                except Exception as xcp:
                    log.warning(
                        "Console action failed: node=%s app=%s action=%s error=%s",
                        current_model.node_name,
                        current_model.app_name,
                        action.key,
                        xcp,
                    )
                    ui.notify(f"Console action failed: {xcp}", type="negative")
                    return
                try:
                    refreshed_actions: NodeConsoleActionList | None = await self._read_console_action_list(
                        model=current_model, user=user
                    )
                except Exception as xcp:
                    log.warning(
                        "Console action list refresh failed: node=%s app=%s action=%s error=%s",
                        current_model.node_name,
                        current_model.app_name,
                        action.key,
                        xcp,
                    )
                else:
                    if refreshed_actions is not None and refreshed_actions.actions:
                        current_console_actions = refreshed_actions
                last_result = result
                last_result_action_key = action.key
                ui.notify(result.summary, type="positive" if result.success else "warning")
            finally:
                action_in_flight = False
                refresh_console_body(force=True)

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Console",
                    description=self._console_card_description(action_count=len(current_console_actions.actions)),
                )

                @ui.refreshable
                def _console_card_body() -> None:
                    action: NodeConsoleActionEntry = selected_action()
                    action_can_execute = self._console_action_can_execute(
                        action=action, app_stats=current_model.app_stats
                    )
                    action_result: NodeConsoleActionExecutionResult | None = self._console_action_result_for_selection(
                        selected_action_key=action.key,
                        last_result_action_key=last_result_action_key,
                        last_result=last_result,
                    )
                    runtime_badge: _ModWebBadgeSpec | None = self._console_action_runtime_badge(
                        action=action, app_stats=current_model.app_stats
                    )
                    parameter: NodeConsoleActionParameter | None = action.parameter
                    current_value: str = draft_values.get(action.key, "")
                    with ui.column().classes("w-full gap-3"):
                        with ui.row().classes("mod-tab-toolbar mod-tab-toolbar-surface w-full"):
                            action_select: Select = bind_console_popup_refresh_lock(
                                ui.select(
                                    {entry.key: entry.label for entry in current_console_actions.actions},
                                    value=action.key,
                                    on_change=select_action,
                                )
                                .props(self._setting_choice_select_props())
                                .classes("mod-config-select mod-console-action-select")
                            )
                            if len(current_console_actions.actions) == 1 or action_in_flight:
                                action_select.disable()
                            with ui.row().classes("mod-tab-toolbar-actions"):
                                run_button: Button = ui.button("Run Action", on_click=run_selected_action).classes(
                                    "mod-list-button"
                                )
                                if action_in_flight:
                                    run_button.set_text("Running...")
                                    run_button.disable()
                                elif not action_can_execute:
                                    run_button.disable()

                        with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                            with ui.column().classes("gap-1"):
                                ui.label(action.label).classes("text-base font-black mod-title-small")
                                ui.label(action.description).classes("mod-subtitle text-sm")
                            with ui.row().classes("gap-2 flex-wrap"):
                                self._badge(
                                    ui=ui,
                                    text=action.power_level_label,
                                    tone=self._console_action_permission_badge_tone(action),
                                )
                                if runtime_badge is not None:
                                    self._badge(ui=ui, text=runtime_badge.text, tone=runtime_badge.tone)
                                if parameter is not None:
                                    self._badge(ui=ui, text=parameter.value_type_name, tone="purple")

                        if parameter is not None:
                            value_input: Input | None = None
                            with ui.column().classes(self._setting_control_surface_classes(can_edit=action.can_run)):
                                if parameter.choices:

                                    def apply_choice(event: ModWebValueContainer) -> None:
                                        next_value: str = _value_as_text(event)
                                        draft_values[action.key] = next_value
                                        if value_input is not None:
                                            value_input.set_value(next_value)
                                        else:
                                            _console_card_body.refresh()

                                    choice_select: Select = bind_console_popup_refresh_lock(
                                        ui.select(
                                            {choice.raw_value: choice.label for choice in parameter.choices},
                                            value=(
                                                current_value
                                                if current_value in {choice.raw_value for choice in parameter.choices}
                                                else None
                                            ),
                                            on_change=apply_choice if action.can_run else None,
                                        )
                                        .props(
                                            self._console_action_select_props(
                                                prefix="",
                                                clearable=parameter.allows_text_input,
                                            )
                                        )
                                        .classes(
                                            "mod-setting-field mod-setting-field-secondary"
                                            if parameter.allows_text_input
                                            else "mod-setting-field mod-setting-field-primary"
                                        )
                                    )
                                    if not action.can_run:
                                        choice_select.disable()
                                    if action_in_flight:
                                        choice_select.disable()

                                if parameter.allows_text_input:

                                    def sync_input_value(_event: object | None = None) -> None:
                                        if value_input is None:
                                            raise ValueError("Console action input is not available.")
                                        draft_values[action.key] = _value_as_text(value_input)

                                    value_input = (
                                        ui.input(
                                            value=current_value,
                                            placeholder=f"Enter {parameter.label}" if action.can_run else "Restricted",
                                            on_change=(sync_input_value if action.can_run else None),
                                        )
                                        .props(self._console_action_input_props(parameter))
                                        .classes("mod-setting-field mod-setting-field-primary")
                                    )
                                    if not action.can_run:
                                        value_input.disable()
                                    else:
                                        value_input.on("update:model-value", sync_input_value)
                                        sync_input_value()
                                    if action_in_flight:
                                        value_input.disable()

                                if parameter.recent_inputs:

                                    def apply_recent_input(event: ModWebValueContainer) -> None:
                                        next_value: str = _value_as_text(event)
                                        draft_values[action.key] = next_value
                                        if value_input is not None:
                                            value_input.set_value(next_value)
                                        else:
                                            _console_card_body.refresh()

                                    recent_select: Select = bind_console_popup_refresh_lock(
                                        ui.select(
                                            {recent_value: recent_value for recent_value in parameter.recent_inputs},
                                            value=None,
                                            on_change=apply_recent_input if action.can_run else None,
                                        )
                                        .props(self._console_action_select_props(prefix="Recent", clearable=True))
                                        .classes("mod-setting-field mod-setting-field-secondary")
                                    )
                                    if not action.can_run:
                                        recent_select.disable()
                                    if action_in_flight:
                                        recent_select.disable()

                            if parameter.description:
                                ui.label(parameter.description).classes("mod-subtitle text-sm break-all")
                        else:
                            ui.label("This action does not require any input.").classes("mod-subtitle text-sm")

                        ui.label(
                            self._console_action_status_text(
                                action=action,
                                app_friendly=current_model.app_friendly,
                                app_stats=current_model.app_stats,
                            )
                        ).classes("mod-subtitle text-sm")

                        if action_result is not None:
                            with ui.column().classes("w-full gap-2"):
                                with ui.row().classes("gap-2 flex-wrap"):
                                    self._badge(
                                        ui=ui,
                                        text="Success" if action_result.success else "Error",
                                        tone=self._console_action_result_badge_tone(action_result),
                                    )
                                    if action_result.source.value != "none":
                                        self._badge(
                                            ui=ui,
                                            text=action_result.source.value.upper(),
                                            tone=self._console_action_source_badge_tone(action_result),
                                        )
                                ui.label(action_result.summary).classes("mod-subtitle text-sm break-all")
                                if action_result.text:
                                    ui.html(
                                        f'<pre class="mod-chat-code-block"><code>{escape(action_result.text)}</code></pre>'
                                    )

                _console_card_body()

        def apply_runtime_model(runtime_model: ModWebBasePageModel) -> None:
            nonlocal current_model
            current_model = runtime_model
            refresh_console_body()

        return apply_runtime_model

    def _render_saves_editor(self, *, ui: ModWebUi, model: ModWebBasePageModel, user: ModWebUser) -> None:
        saves: NodeSaveList | None = model.saves
        if saves is None:
            return

        save_options: tuple[ModWebSearchOption, ...] = self._save_options(saves.saves)
        save_root_options: dict[str, str] = {root.id: root.label for root in saves.roots}
        selected_root_id: str | None = saves.roots[0].id if saves.roots else None
        can_write: bool = self._user_has_level(user, model.save_write_level)
        show_search: bool = len(save_options) > 1
        show_root_selector: bool = model.supports_save_uploads and len(saves.roots) > 1
        show_upload_action: bool = model.supports_save_uploads and can_write and selected_root_id is not None
        show_write_lock_note: bool = (model.supports_save_uploads or model.supports_save_rename) and not can_write

        async def upload_save(event: "UploadEventArguments") -> None:
            if not model.supports_save_uploads:
                ui.notify(f"{model.app_friendly} does not support save uploads.", type="warning")
                return
            selected_root_value: str | None = (
                selected_root_id if root_select is None else _value_as_text(root_select).strip() or None
            )
            root_id: str | None = selected_root_value or selected_root_id
            if not root_id:
                ui.notify("Select a save root first.", type="warning")
                return
            try:
                result: NodeSaveMutationResult = await self._upload_save(
                    model=model, root_id=root_id, upload_file=event.file, user=user
                )
            except Exception as xcp:
                ui.notify(f"Save upload failed: {xcp}", type="negative")
                return
            upload_dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        root_select: Select | None = None
        with ui.dialog() as upload_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Upload Save").classes("text-xl font-black mod-title-small")
                        ui.label("Upload a replacement save archive for this app.").classes("mod-subtitle text-sm")
                    if show_root_selector:
                        root_select = (
                            ui.select(save_root_options, value=selected_root_id)
                            .props("filled square dense hide-bottom-space color=accent")
                            .classes("mod-config-select")
                        )
                    ui.upload(
                        label="Choose Save Archive",
                        auto_upload=True,
                        on_upload=upload_save,
                    ).classes("mod-list-button")
                    ui.label("ZIP archives are uploaded directly into the selected save target.").classes(
                        "mod-subtitle text-sm"
                    )
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Close", on_click=upload_dialog.close).classes("mod-list-button secondary")

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Saves",
                    description=self._save_card_description(model=model, save_count=len(saves.saves)),
                )

                @ui.refreshable
                def _save_tile_grid(search_query: str) -> None:
                    filtered_saves: tuple[NodeSaveEntry, ...] = self._filter_save_entries(
                        saves=saves.saves,
                        options=save_options,
                        search_query=search_query,
                    )
                    if not filtered_saves:
                        with ui.card().classes("mod-setting-card locked w-full"):
                            ui.label("No saves match that search.").classes("mod-subtitle text-sm")
                        return
                    with ui.element("div").classes("mod-save-grid w-full"):
                        for save in filtered_saves:
                            self._render_save_tile(
                                ui=ui,
                                model=model,
                                user=user,
                                save=save,
                                root_count=len(saves.roots),
                                can_write=can_write,
                            )

                if show_search or show_upload_action:
                    with ui.row().classes("mod-tab-toolbar mod-tab-toolbar-surface w-full"):
                        if show_search:
                            search_input: Input = (
                                ui.input(placeholder="Search saves")
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("mod-config-search")
                            )
                            def _refresh_save_tiles(event: ModWebEventArgumentsContainer) -> None:
                                _save_tile_grid.refresh(_event_args_as_text(event))

                            search_input.on("update:model-value", _refresh_save_tiles)
                        with ui.row().classes("mod-tab-toolbar-actions"):
                            if show_upload_action:
                                ui.button("Upload Save", on_click=upload_dialog.open).classes("mod-list-button")

                if not saves.saves:
                    ui.label("No saves are currently available for this app.").classes(
                        "mod-subtitle text-sm mod-tab-empty-detail"
                    )
                    if show_write_lock_note:
                        ui.label(
                            f"{model.save_write_level.name.title()} access is required to manage saves for this app."
                        ).classes("mod-subtitle text-sm mod-tab-empty-detail")
                    return
                _save_tile_grid("")

    def _render_save_tile(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        save: NodeSaveEntry,
        root_count: int,
        can_write: bool,
    ) -> None:
        detail_path_text: str | None = self._save_detail_path_text(save=save, root_count=root_count)
        rename_dialog: Dialog | None = None
        rename_input: Input | None = None

        async def rename_selected() -> None:
            if rename_input is None:
                raise ValueError("Save rename input is not available.")
            try:
                result: NodeSaveMutationResult = await self._rename_save(
                    model=model,
                    save_id=save.id,
                    new_name=_value_as_text(rename_input),
                    user=user,
                )
            except Exception as xcp:
                ui.notify(f"Save rename failed: {xcp}", type="negative")
                return
            if rename_dialog is not None:
                rename_dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        with ui.card().classes("mod-save-card"):
            with ui.column().classes("w-full gap-3 p-4"):
                with ui.column().classes("gap-1 w-full"):
                    ui.label(save.label).classes("text-base font-black mod-title-small break-all")
                    if detail_path_text is not None:
                        ui.label(detail_path_text).classes("mod-subtitle text-sm break-all mod-save-card-path")
                with ui.row().classes("gap-2 flex-wrap"):
                    if root_count > 1:
                        self._badge(ui=ui, text=save.root_label, tone="grey")
                    self._badge(ui=ui, text=save.kind.title(), tone="grey")
                    if self._save_shows_size_badge(save):
                        self._badge(ui=ui, text=save.size_text, tone="black")
                    self._badge(ui=ui, text=f"Modified {save.modified_at}", tone="purple")
                action_classes = (
                    "mod-save-card-actions mod-save-card-actions-split"
                    if model.supports_save_rename
                    else "mod-save-card-actions mod-save-card-actions-single"
                )

                async def _download_current_save(_: object | None = None) -> None:
                    await self._download_save(
                        ui=ui,
                        model=model,
                        save=save,
                        user=user,
                    )

                with ui.row().classes(action_classes):
                    ui.button(
                        "Download",
                        on_click=_download_current_save,
                    ).classes("mod-list-button mod-save-card-button")
                    if model.supports_save_rename:
                        with ui.dialog().classes("mod-dialog-card") as rename_dialog:
                            rename_dialog_ref: Dialog = rename_dialog
                            with ui.card().classes("mod-card w-full"):
                                with ui.column().classes("w-full gap-4 p-5"):
                                    with ui.column().classes("gap-1"):
                                        ui.label("Rename Save").classes("text-xl font-black mod-title-small")
                                        ui.label(f"Choose a new name for {save.label}.").classes("mod-subtitle text-sm")
                                    rename_input = (
                                        ui.input(value=save.label, placeholder="New save name")
                                        .props("filled square dense clearable hide-bottom-space color=accent")
                                        .classes("w-full")
                                    )
                                    with ui.row().classes("w-full justify-end gap-2"):
                                        ui.button("Cancel", on_click=rename_dialog_ref.close).classes(
                                            "mod-list-button secondary"
                                        )
                                        ui.button("Rename", on_click=rename_selected).classes("mod-list-button")
                        rename_button = ui.button("Rename", on_click=rename_dialog_ref.open).classes(
                            "mod-list-button secondary mod-save-card-button"
                        )
                        if not can_write:
                            rename_button.disable()

    def _render_settings_editor(self, *, ui: ModWebUi, model: ModWebBasePageModel, user: ModWebUser) -> None:
        settings: NodeSettingList | None = model.settings
        if settings is None:
            return
        if not settings.settings:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Settings",
                description="No runtime settings are currently exposed for this app.",
                detail_text="This tab will populate when the app exposes editable or readable settings.",
            )
            return

        setting_options: tuple[ModWebSearchOption, ...] = self._setting_options(settings.settings)
        draft_values: dict[str, bool | str] = {}
        invalid_setting_keys: set[str] = set[str]()
        save_button: Button | None = None
        reload_button: Button | None = None
        required_save_level: Power_Level = (
            Access_Control.parse_level(settings.required_save_level_name) or Power_Level.user
        )
        required_reload_level: Power_Level = (
            Access_Control.parse_level(settings.required_reload_level_name) or Power_Level.user
        )
        can_save_pending: bool = self._user_has_level(user, required_save_level)
        can_reload_pending: bool = self._user_has_level(user, required_reload_level)

        def refresh_save_button() -> None:
            if save_button is None:
                return
            if reload_button is not None:
                if can_reload_pending:
                    reload_button.enable()
                else:
                    reload_button.disable()
            if invalid_setting_keys:
                save_button.disable()
                return
            if settings.has_pending_changes and not can_save_pending:
                save_button.disable()
                return
            if draft_values:
                save_button.enable()
                return
            if settings.has_pending_changes and can_save_pending:
                save_button.enable()
                return
            save_button.disable()

        def set_draft_value(setting: NodeSettingEntry, value: bool | str, force_draft: bool = False) -> None:
            current_value: bool | str = self._setting_current_control_value(setting)
            if not force_draft and value == current_value:
                draft_values.pop(setting.key, None)
            else:
                draft_values[setting.key] = value
            refresh_save_button()

        def set_setting_validity(setting: NodeSettingEntry, is_valid: bool) -> None:
            if is_valid:
                invalid_setting_keys.discard(setting.key)
            else:
                invalid_setting_keys.add(setting.key)
            refresh_save_button()

        async def save_settings() -> None:
            if not draft_values:
                ui.notify("No pending setting changes.", type="warning")
                return
            try:
                for setting in settings.settings:
                    if setting.key not in draft_values:
                        continue
                    await self._write_setting_value(
                        model=model,
                        setting_key=setting.key,
                        value=self._setting_control_value_to_submit(setting, draft_values[setting.key]),
                        user=user,
                    )
                result: NodeSettingsActionResult = await self._save_settings(model=model, user=user)
            except Exception as xcp:
                log.warning(
                    "Settings save failed: node=%s app=%s error=%s",
                    model.node_name,
                    model.app_name,
                    xcp,
                )
                ui.notify(f"Settings save failed: {xcp}", type="negative")
                return
            draft_values.clear()
            refresh_save_button()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        async def reload_settings() -> None:
            try:
                result: NodeSettingsActionResult = await self._reload_settings(model=model, user=user)
            except Exception as xcp:
                log.warning(
                    "Settings reload failed: node=%s app=%s error=%s",
                    model.node_name,
                    model.app_name,
                    xcp,
                )
                ui.notify(f"Settings reload failed: {xcp}", type="negative")
                return
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        @ui.refreshable
        def _setting_card_list(search_query: str) -> None:
            filtered_settings: tuple[NodeSettingEntry, ...] = self._filter_setting_entries(
                settings=settings.settings,
                options=setting_options,
                search_query=search_query,
            )
            if not filtered_settings:
                with ui.card().classes("mod-setting-card locked w-full"):
                    ui.label("No settings match that search.").classes("mod-subtitle text-sm")
                return

            with ui.column().classes("mod-settings-grid w-full"):
                for setting in filtered_settings:
                    self._render_setting_card(
                        ui=ui,
                        setting=setting,
                        draft_value=draft_values.get(setting.key, self._setting_current_control_value(setting)),
                        set_draft_value=set_draft_value,
                        set_setting_validity=set_setting_validity,
                    )

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Settings",
                    description=self._settings_card_description(),
                )
                with ui.row().classes("mod-tab-toolbar mod-tab-toolbar-surface w-full"):
                    search_input = (
                        ui.input(placeholder="Search settings")
                        .props("filled square dense clearable hide-bottom-space color=accent")
                        .classes("mod-config-search mod-settings-search")
                    )
                    def _refresh_setting_cards(event: ModWebEventArgumentsContainer) -> None:
                        _setting_card_list.refresh(_event_args_as_text(event))

                    search_input.on("update:model-value", _refresh_setting_cards)
                    with ui.row().classes("mod-tab-toolbar-actions"):
                        reload_button = ui.button("Reload", on_click=reload_settings).classes(
                            "mod-list-button secondary"
                        )
                        save_button = ui.button("Save", on_click=save_settings).classes("mod-list-button")
                        save_button.disable()
                _setting_card_list("")
                refresh_save_button()

    def _render_setting_card(
        self,
        *,
        ui: ModWebUi,
        setting: NodeSettingEntry,
        draft_value: bool | str,
        set_draft_value: Callable[[NodeSettingEntry, bool | str, bool], None],
        set_setting_validity: Callable[[NodeSettingEntry, bool], None],
    ) -> None:
        control_kind: ModWebSettingControlKind = self._setting_control_kind(setting)
        choice_select: Select | None = None
        value_input: Input | None = None
        invalid_feedback: Label | None = None

        card_classes = "mod-setting-card w-full"
        if not setting.can_edit:
            card_classes: LiteralString = f"{card_classes} locked"

        with ui.card().classes(card_classes):
            with ui.row().classes("mod-setting-shell w-full"):
                with ui.column().classes("mod-setting-control gap-1"):
                    if control_kind is ModWebSettingControlKind.BOOLEAN_SWITCH:
                        if not isinstance(draft_value, bool):
                            raise TypeError(f"Boolean setting {setting.key!r} requires a bool draft value.")

                        def sync_switch_value(event: ModWebValueContainer) -> None:
                            next_value: bool = bool(_value_as_object(event))
                            set_draft_value(setting, next_value, setting.value_is_hidden)
                            set_setting_validity(setting, True)

                        switch_control: Switch = (
                            ui.switch(
                                value=draft_value,
                                on_change=sync_switch_value if setting.can_edit else None,
                            )
                            .props("color=accent")
                            .classes("mod-setting-switch")
                        )
                        if not setting.can_edit:
                            switch_control.disable()
                    elif control_kind is ModWebSettingControlKind.CHOICE_SELECT:
                        if not isinstance(draft_value, str):
                            raise TypeError(f"Choice setting {setting.key!r} requires a string draft value.")

                        def sync_choice_value(event: ModWebValueContainer) -> None:
                            set_draft_value(setting, _value_as_text(event), setting.value_is_hidden)
                            set_setting_validity(setting, True)

                        with ui.column().classes(self._setting_control_surface_classes(can_edit=setting.can_edit)):
                            choice_select = (
                                ui.select(
                                    {choice.label: choice.label for choice in setting.choices},
                                    value=draft_value or None,
                                    on_change=sync_choice_value if setting.can_edit else None,
                                )
                                .props(self._setting_choice_select_props())
                                .classes("mod-setting-field mod-setting-field-primary")
                            )
                        if not setting.can_edit:
                            choice_select.disable()
                    else:
                        if not isinstance(draft_value, str):
                            raise TypeError(f"Text setting {setting.key!r} requires a string draft value.")

                        def sync_input_value(force_draft: bool = False) -> None:
                            if value_input is None:
                                raise ValueError("Text setting input control is not available.")
                            next_value: str = _value_as_text(value_input)
                            set_draft_value(setting, next_value, force_draft)
                            validation_message: str | None = self._setting_text_validation_message(setting, next_value)
                            set_setting_validity(setting, validation_message is None)
                            self._update_setting_text_input_feedback(
                                input_control=value_input,
                                feedback_label=invalid_feedback,
                                message=validation_message,
                            )

                        def _sync_hidden_input(_event: object) -> None:
                            sync_input_value(setting.value_is_hidden)

                        with ui.column().classes(self._setting_control_surface_classes(can_edit=setting.can_edit)):
                            value_input = (
                                ui.input(
                                    value=draft_value,
                                    placeholder=f"Enter {setting.label}" if setting.can_edit else "Restricted",
                                    on_change=(_sync_hidden_input if setting.can_edit else None),
                                )
                                .props(self._setting_text_input_props(setting))
                                .classes("mod-setting-field mod-setting-field-primary")
                            )
                            value_input_control: Input = value_input
                            if not setting.can_edit:
                                value_input_control.disable()
                                set_setting_validity(setting, True)
                            else:
                                value_input_control.on(
                                    "update:model-value",
                                    _sync_hidden_input,
                                )
                            if setting.can_edit:
                                sync_input_value()

                            if setting.choices:

                                def apply_preset_choice(event: ModWebValueContainer) -> None:
                                    next_value: str = _value_as_text(event)
                                    value_input_control.set_value(next_value)

                                preset_select: Select = (
                                    ui.select(
                                        {choice.label: choice.label for choice in setting.choices},
                                        value=None,
                                        on_change=apply_preset_choice,
                                    )
                                    .props(self._setting_aux_select_props(prefix="Preset"))
                                    .classes("mod-setting-field mod-setting-field-secondary")
                                )
                                if not setting.can_edit:
                                    preset_select.disable()

                            if setting.recent_inputs:

                                def apply_recent_input(event: ModWebValueContainer) -> None:
                                    next_value: str = _value_as_text(event)
                                    value_input_control.set_value(next_value)

                                recent_select: Select = (
                                    ui.select(
                                        {recent_value: recent_value for recent_value in setting.recent_inputs},
                                        value=None,
                                        on_change=apply_recent_input,
                                    )
                                    .props(self._setting_aux_select_props(prefix="Recent"))
                                    .classes("mod-setting-field mod-setting-field-secondary")
                                )
                                if not setting.can_edit:
                                    recent_select.disable()
                        invalid_feedback = ui.label("").classes("mod-setting-input-feedback")

                with ui.column().classes("mod-setting-main gap-1"):
                    with ui.row().classes("w-full justify-center"):
                        ui.label(setting.label).classes("mod-setting-name")
                    if setting.description:
                        ui.label(setting.description).classes("mod-setting-desc break-all")
                    ui.label(setting.key).classes("mod-setting-key break-all")

                with ui.row().classes("mod-setting-meta"):
                    self._render_setting_meta_value(ui=ui, setting=setting)
                    if setting.default_text or not setting.value_is_hidden:
                        ui.label(setting.default_text).classes("mod-setting-meta-default")
                with ui.column().classes("mod-setting-badge-rail"):
                    self._badge(
                        ui=ui,
                        text=setting.permission_level,
                        tone=self._setting_permission_badge_tone(setting),
                        extra_classes="mod-setting-badge",
                    )
                    if setting.has_pending_value:
                        self._badge(ui=ui, text="Draft", tone="grey", extra_classes="mod-setting-badge")

    def _render_config_editor(self, *, ui: ModWebUi, model: ModWebBasePageModel, user: ModWebUser) -> None:
        if not model.supports_configs:
            return

        can_read = self._user_has_level(user, model.config_read_level)
        can_write = self._user_has_level(user, model.config_write_level)
        if not can_read:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Configs",
                description=f"{model.config_read_level.name.title()} access is required to read app config files.",
                secondary_description=(
                    f"Writing requires {model.config_write_level.name.title()} access."
                    if model.config_write_level != model.config_read_level
                    else None
                ),
                notepad=True,
            )
            return

        configs: tuple[NodeConfigEntry, ...] = model.configs.configs
        if not configs:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Configs",
                description="No config files are currently indexed for this app.",
                detail_text="Add a readable config root to populate this tab.",
                notepad=True,
            )
            return

        layout: ModWebConfigEditorLayout = self._config_editor_layout(configs)
        config_by_id: dict[str, NodeConfigEntry] = {entry.id: entry for entry in configs}
        configs_by_root: dict[str, tuple[NodeConfigEntry, ...]] = self._configs_by_root(configs)
        root_options: tuple[ModWebSearchOption, ...] = self._config_root_options(configs_by_root)
        state: dict[str, str] = {
            "root_id": layout.selected_root_id,
            "config_id": layout.selected_config_id,
        }

        config_select: Select | None = None

        def set_selected_config(config_id: str | None) -> None:
            state["config_id"] = config_id or ""
            if not config_id:
                return
            selected_entry: NodeConfigEntry | None = config_by_id.get(config_id)
            if selected_entry is not None:
                state["root_id"] = selected_entry.root_id

        async def load_config(config_id: str, *, notify: bool = False) -> None:
            if not config_id:
                ui.notify("Select a config file first.", type="warning")
                return
            if loaded_label is None or meta_label is None:
                raise ValueError("Config tab header labels are not available.")
            try:
                loaded: NodeConfigContent = await self._read_config_content(model=model, config_id=config_id, user=user)
            except Exception as xcp:
                log.warning(
                    "Config load failed: node=%s app=%s config=%s error=%s",
                    model.node_name,
                    model.app_name,
                    config_id,
                    xcp,
                )
                ui.notify(f"Config load failed: {xcp}", type="negative")
                return
            editor.set_language(self._config_editor_language(loaded.config))
            editor.set_value(loaded.content)
            loaded_label.set_text(f"{loaded.config.root_label} / {loaded.config.relative_path}")
            meta_label.set_text(f"{loaded.config.size_text} · modified {loaded.config.modified_at}")
            if notify:
                ui.notify("Config loaded.", type="positive")

        async def load_selected_config() -> None:
            await load_config(state["config_id"], notify=True)

        async def save_selected_config() -> None:
            if not can_write:
                ui.notify(
                    f"{model.config_write_level.name.title()} access is required to save config files.",
                    type="warning",
                )
                return
            if loaded_label is None or meta_label is None:
                raise ValueError("Config tab header labels are not available.")
            config_id: str = state["config_id"]
            if not config_id:
                ui.notify("Select a config file first.", type="warning")
                return
            try:
                saved: NodeConfigContent = await self._write_config_content(
                    model=model,
                    config_id=config_id,
                    content=_value_as_text(editor),
                    user=user,
                )
            except Exception as xcp:
                log.warning(
                    "Config save failed: node=%s app=%s config=%s error=%s",
                    model.node_name,
                    model.app_name,
                    config_id,
                    xcp,
                )
                ui.notify(f"Config save failed: {xcp}", type="negative")
                return
            loaded_label.set_text(f"{saved.config.root_label} / {saved.config.relative_path}")
            meta_label.set_text(f"{saved.config.size_text} · modified {saved.config.modified_at}")
            ui.notify("Config saved.", type="positive")

        async def download_selected_root() -> None:
            root_id: str = state["root_id"]
            if not root_id:
                ui.notify("Select a config root first.", type="warning")
                return
            root_entries: tuple[NodeConfigEntry, ...] = configs_by_root.get(root_id, ())
            if not root_entries:
                ui.notify("No readable config files are available in that root.", type="warning")
                return
            await self._start_download(
                ui=ui,
                url=self._config_root_download_url(model=model, root_id=root_id, user=user),
                message=f"Preparing download for config root {root_entries[0].root_label} from {model.app_friendly}.",
            )

        def config_file_options(root_id: str) -> tuple[ModWebSearchOption, ...]:
            return self._config_file_options(configs_by_root.get(root_id, ()))

        def single_selector_options() -> tuple[ModWebSearchOption, ...]:
            if layout.shape is ModWebConfigEditorShape.SINGLE_FOLDER_MULTI_FILE:
                return self._config_file_options(configs)
            if layout.shape is ModWebConfigEditorShape.MULTI_FOLDER_SINGLE_FILE:
                return self._config_single_file_root_options(configs_by_root)
            raise ValueError(f"Config layout does not use a single selector: {layout.shape.value}")

        def set_file_selector_options(*, root_id: str, preferred_config_id: str | None) -> str | None:
            if config_select is None:
                raise ValueError("Config selector is not available.")
            options: dict[str, str] = {option.option_id: option.label for option in config_file_options(root_id)}
            next_config_id: str | None = preferred_config_id if preferred_config_id in options else None
            if next_config_id is None and options:
                next_config_id = next(iter(options))
            select_control = cast(_ModWebSelectOptionsControl, config_select)
            select_control.set_options(options, value=next_config_id)
            set_selected_config(next_config_id)
            return next_config_id

        async def config_selection_changed(event: ModWebValueContainer) -> None:
            config_id: str = _value_as_text(event).strip()
            if not config_id:
                return
            set_selected_config(config_id)
            await load_config(config_id)

        async def load_initial_config() -> None:
            await load_config(layout.selected_config_id)

        async def root_selection_changed(event: ModWebValueContainer) -> None:
            root_id: str = _value_as_text(event).strip()
            state["root_id"] = root_id
            next_config_id: str | None = set_file_selector_options(root_id=root_id, preferred_config_id=None)
            if next_config_id is not None:
                await load_config(next_config_id)

        def set_line_wrapping(event: ModWebValueContainer) -> None:
            editor.set_line_wrapping(bool(_value_as_object(event)))

        with ui.card().classes(self._flat_tab_card_classes(notepad=True)):
            with ui.column().classes(self._tab_section_body_classes()):
                loaded_label, meta_label = self._render_flat_tab_header(
                    ui=ui,
                    title="Configs",
                    description=self._config_card_description(),
                    secondary_description="Loading selected config metadata...",
                )
                if loaded_label is None or meta_label is None:
                    raise ValueError("Config tab header labels are not available.")
                with ui.row().classes("mod-tab-toolbar mod-tab-toolbar-surface w-full"):
                    if layout.shows_root_selector:
                        ui.select(
                            {option.option_id: option.label for option in root_options},
                            label="Config Area",
                            value=layout.selected_root_id,
                            on_change=root_selection_changed,
                        ).props(self._config_select_props(clearable=False)).classes("mod-config-select")
                    if layout.shows_file_selector:
                        initial_options: tuple[ModWebSearchOption, ...] = (
                            config_file_options(layout.selected_root_id)
                            if layout.shape is ModWebConfigEditorShape.MULTI_FOLDER_MULTI_FILE
                            else single_selector_options()
                        )
                        config_select = (
                            ui.select(
                                {option.option_id: option.label for option in initial_options},
                                label=layout.primary_selector_label,
                                value=layout.selected_config_id,
                                on_change=config_selection_changed,
                                with_input=True,
                            )
                            .props(self._config_select_props(clearable=True))
                            .classes("mod-config-select")
                        )
                    with ui.row().classes("mod-tab-toolbar-actions"):
                        ui.button("Reload", on_click=load_selected_config).classes("mod-list-button secondary")
                        ui.button("Download All", on_click=download_selected_root).classes("mod-list-button secondary")
                        save_button: Button = ui.button("Save", on_click=save_selected_config).classes(
                            "mod-list-button"
                        )
                        if not can_write:
                            save_button.disable()
                with ui.column().classes("mod-config-editor-shell relative w-full"):
                    editor: CodeMirror = ui.codemirror(
                        value="",
                        language=self._config_editor_language(config_by_id[layout.selected_config_id]),
                        theme=_CONFIG_EDITOR_THEME,
                        line_wrapping=True,
                    ).classes("mod-config-editor w-full full-width")
                    ui.checkbox("Wrap", value=True, on_change=set_line_wrapping).props("dense size=xs").classes(
                        "mod-config-wrap-toggle"
                    )
                load_timer: Timer = ui.timer(0.1, load_initial_config, once=True)
                self._register_timer_cleanup(ui=ui, timer=load_timer)

    @staticmethod
    def _download_base_url(model: ModWebPageModel) -> str:
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            return f"{_SAME_ORIGIN_NODE_API_BASE}/apps/{quote(model.app_name, safe='')}/mods/download"
        return (
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{quote(model.node_name, safe='')}"
            f"/apps/{quote(model.app_name, safe='')}/mods/download"
        )

    def _save_download_url(self, *, model: ModWebBasePageModel, save: NodeSaveEntry, user: ModWebUser) -> str:
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            return self._node_api.save_download_url(
                model.app_name,
                save.id,
                base_url=_SAME_ORIGIN_NODE_API_BASE,
            )
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return self._remote_download_url(
            node=node,
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/saves/{quote(save.id, safe='/')}/download",
            query={},
            user=user,
            scopes=(NodeApiScope.SAVES_DOWNLOAD,),
        )

    def _config_root_download_url(self, *, model: ModWebBasePageModel, root_id: str, user: ModWebUser) -> str:
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            return self._node_api.config_root_download_url(
                model.app_name,
                root_id,
                base_url=_SAME_ORIGIN_NODE_API_BASE,
            )
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return self._remote_download_url(
            node=node,
            app_name=model.app_name,
            path=f"/apps/{quote(model.app_name, safe='')}/configs/roots/{quote(root_id, safe='')}/download",
            query={},
            user=user,
            scopes=(NodeApiScope.CONFIGS_READ,),
        )

    async def _read_config_content(
        self,
        *,
        model: ModWebBasePageModel,
        config_id: str,
        user: ModWebUser,
    ) -> NodeConfigContent:
        required_level: Power_Level = self._config_read_level_for_id(model=model, config_id=config_id)
        if not self._user_has_level(user, required_level):
            raise PermissionError(
                f"{required_level.name.title()} access is required to read config files for {model.app_friendly}."
            )
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await asyncio.to_thread(self._node_api.read_config_file, app=app, config_id=config_id)
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_config_content, node, model.app_name, config_id, user)

    async def _write_config_content(
        self,
        *,
        model: ModWebBasePageModel,
        config_id: str,
        content: str,
        user: ModWebUser,
    ) -> NodeConfigContent:
        if not self._user_has_level(user, model.config_write_level):
            raise PermissionError(
                f"{model.config_write_level.name.title()} access is required to write config files for {model.app_friendly}."
            )
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await asyncio.to_thread(
                self._node_api.write_config_file, app=app, config_id=config_id, content=content
            )
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_config_write, node, model.app_name, config_id, content, user)

    async def _download_save(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        save: NodeSaveEntry,
        user: ModWebUser,
    ) -> None:
        await self._start_download(
            ui=ui,
            url=self._save_download_url(model=model, save=save, user=user),
            message=f"Preparing download for save {save.label} from {model.app_friendly}.",
        )

    async def _upload_save(
        self,
        *,
        model: ModWebBasePageModel,
        root_id: str,
        upload_file: "FileUpload",
        user: ModWebUser,
    ) -> NodeSaveMutationResult:
        if not self._user_has_level(user, model.save_write_level):
            raise PermissionError(
                f"{model.save_write_level.name.title()} access is required to upload saves for {model.app_friendly}."
            )
        temp_path: Path = await self._persist_uploaded_file(upload_file)
        try:
            if model.node_name == config.MOD_WEB_SERVER.node_name:
                app = self._resolve_app(model.app_name)
                return self._node_api.upload_save_path(
                    app=app,
                    root_id=root_id,
                    source_path=temp_path,
                    upload_name=upload_file.name,
                    actor_user_id=user.discord_id,
                )
            node = self._remote_node_link(model.node_name)
            return await asyncio.to_thread(
                self._remote_save_upload,
                node,
                model.app_name,
                root_id,
                temp_path,
                upload_file.name,
                user,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    async def _upload_mod(
        self,
        *,
        model: ModWebPageModel,
        upload_file: "FileUpload",
        user: ModWebUser,
    ) -> NodeModUploadResult:
        if not self._user_has_level(user, Power_Level.user):
            raise PermissionError(f"User access is required to upload mods for {model.app_friendly}.")
        temp_path: Path = await self._persist_uploaded_file(upload_file)
        try:
            if model.node_name == config.MOD_WEB_SERVER.node_name:
                app = self._resolve_app(model.app_name)
                return await self._node_api.upload_mod_path(
                    app=app,
                    source_path=temp_path,
                    upload_name=upload_file.name,
                    actor_user_id=user.discord_id,
                )
            node = self._remote_node_link(model.node_name)
            return await asyncio.to_thread(
                self._remote_mod_upload,
                node,
                model.app_name,
                temp_path,
                upload_file.name,
                user,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    async def _rename_save(
        self,
        *,
        model: ModWebBasePageModel,
        save_id: str,
        new_name: str,
        user: ModWebUser,
    ) -> NodeSaveMutationResult:
        if not self._user_has_level(user, model.save_write_level):
            raise PermissionError(
                f"{model.save_write_level.name.title()} access is required to rename saves for {model.app_friendly}."
            )
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await self._node_api.rename_save_file(
                app=app,
                save_id=save_id,
                new_name=new_name,
                actor_user_id=user.discord_id,
            )
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_save_rename, node, model.app_name, save_id, new_name, user)

    async def _write_setting_value(
        self,
        *,
        model: ModWebBasePageModel,
        setting_key: str,
        value: str,
        user: ModWebUser,
    ) -> NodeSettingMutationResult:
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await self._node_api.update_setting(
                app=app,
                setting_key=setting_key,
                value=value,
                actor_user_id=user.discord_id,
            )
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_setting_write, node, model.app_name, setting_key, value, user)

    async def _save_settings(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> NodeSettingsActionResult:
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await self._node_api.save_settings(app=app, actor_user_id=user.discord_id)
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_settings_save, node, model.app_name, user)

    async def _reload_settings(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> NodeSettingsActionResult:
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await self._node_api.reload_settings(app=app, actor_user_id=user.discord_id)
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_settings_reload, node, model.app_name, user)

    async def _read_console_action_list(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> NodeConsoleActionList | None:
        if model.console_actions is None:
            return None
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return self._node_api.build_console_action_list(app=app, actor_user_id=user.discord_id)
        node = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_console_action_list, node, model.app_name, user)

    async def _execute_console_action(
        self,
        *,
        model: ModWebBasePageModel,
        action_key: str,
        raw_value: str | None,
        user: ModWebUser,
    ) -> NodeConsoleActionExecutionResult:
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await self._node_api.execute_console_action(
                app=app,
                action_key=action_key,
                raw_value=raw_value,
                actor_user_id=user.discord_id,
            )
        node = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(
            self._remote_execute_console_action,
            node,
            model.app_name,
            action_key,
            raw_value,
            user,
        )

    @staticmethod
    def _empty_config_list(*, app_name: str, app_friendly: str, node_name: str) -> NodeConfigList:
        return NodeConfigList(
            app_name=app_name,
            app_friendly=app_friendly,
            node=node_name,
            configs=(),
        )

    def _config_read_level_for_id(self, *, model: ModWebBasePageModel, config_id: str) -> Power_Level:
        for config_entry in model.configs.configs:
            if config_entry.id == config_id:
                return config_entry.read_power_level
        if model.node_name != config.MOD_WEB_SERVER.node_name:
            return model.config_read_level
        try:
            app = self._resolve_app(model.app_name)
            return app.config_file_read_level_for_id(config_id)
        except ValueError:
            return model.config_read_level

    @staticmethod
    def _hero_card_style(color_hex: str | None) -> str:
        if color_hex is None:
            return ""
        return f"--mod-hero-border: {color_hex}; --mod-hero-border-fade: var(--mod-border);"

    def _primary_guild_bot_role_color_hex(self) -> str | None:
        return self._node_role_color_hex(node_name=config.MOD_WEB_SERVER.node_name)

    def _node_role_color_hex(self, *, node_name: str) -> str | None:
        bot: GatewayBot | None = self._mod_web_bot()
        if bot is None:
            return None
        target_user_id: int | None = self._node_bot_user_id(node_name=node_name, bot=bot)
        if target_user_id is None:
            return None
        return color_int_to_hex(cached_member_role_color(bot, guild_id=config.DISCORD_GUILD, user_id=target_user_id))

    def _node_bot_user_id(self, *, node_name: str, bot: GatewayBot) -> int | None:
        if node_name.casefold() == config.MOD_WEB_SERVER.node_name.casefold():
            me: OwnUser | None = bot.get_me()
            return int(me.id) if me is not None else None
        for snapshot in self._known_bot_snapshots():
            mod_web: BotMetadataModWeb | None = snapshot.features.mod_web
            if mod_web is None or mod_web.node_name.casefold() != node_name.casefold():
                continue
            return int(hikari.Snowflake(snapshot.profile.id))
        return None

    def _node_text_style(self, *, node_name: str) -> str | None:
        if color_hex := self._node_role_color_hex(node_name=node_name):
            return f"color: {color_hex} !important;"
        return None

    def _mod_web_bot(self) -> hikari.GatewayBot | None:
        manager: App_Manager | None = self._manager
        if manager is None:
            return None
        if manager.bot is not None:
            return manager.bot
        for app in manager.apps.values():
            return app.bot
        return None

    @staticmethod
    def _node_badge_style(color_hex: str) -> str:
        return f"border-color: {color_hex} !important;"

    def _user_level_label(self, user: ModWebUser) -> str:
        return self._user_level(user).name.title()

    def _user_level_tone(self, user: ModWebUser) -> BadgeTone:
        level: Power_Level = self._user_level(user)
        if level >= Power_Level.sudo:
            return "red"
        if level >= Power_Level.admin:
            return "purple"
        if level >= Power_Level.user:
            return "black"
        if level >= Power_Level.visitor:
            return "grey"
        return "grey"

    @staticmethod
    def _config_option_label(entry: NodeConfigEntry) -> str:
        return f"{entry.root_label} / {entry.relative_path}"

    @staticmethod
    def _config_editor_language(entry: NodeConfigEntry) -> SUPPORTED_LANGUAGES | None:
        relative_path: PurePosixPath = PurePosixPath(entry.relative_path)
        file_name: str = relative_path.name.casefold()
        suffix: str = relative_path.suffix.casefold()

        if file_name in {"dockerfile"}:
            return _CONFIG_EDITOR_DOCKERFILE_LANGUAGE

        return _CONFIG_EDITOR_LANGUAGE_BY_SUFFIX.get(suffix)

    @staticmethod
    def _configs_by_root(configs: tuple[NodeConfigEntry, ...]) -> dict[str, tuple[NodeConfigEntry, ...]]:
        grouped: dict[str, list[NodeConfigEntry]] = {}
        for entry in configs:
            grouped.setdefault(entry.root_id, []).append(entry)
        return {root_id: tuple[NodeConfigEntry, ...](entries) for root_id, entries in grouped.items()}

    @classmethod
    def _config_root_options(
        cls, configs_by_root: Mapping[str, tuple[NodeConfigEntry, ...]]
    ) -> tuple[ModWebSearchOption, ...]:
        options: list[ModWebSearchOption] = []
        for root_id, entries in configs_by_root.items():
            if not entries:
                continue
            root_label: str = entries[0].root_label
            options.append(
                ModWebSearchOption(
                    option_id=root_id,
                    label=root_label,
                    search_text=f"{root_label} {root_id}".casefold(),
                )
            )
        return tuple(options)

    @classmethod
    def _config_editor_layout(cls, configs: tuple[NodeConfigEntry, ...]) -> ModWebConfigEditorLayout:
        if not configs:
            raise ValueError("Config layout requires at least one config entry.")
        configs_by_root: dict[str, tuple[NodeConfigEntry, ...]] = cls._configs_by_root(configs)
        if len(configs) == 1:
            shape = ModWebConfigEditorShape.SINGLE_FILE
        elif len(configs_by_root) == 1:
            shape = ModWebConfigEditorShape.SINGLE_FOLDER_MULTI_FILE
        elif all(len(entries) == 1 for entries in configs_by_root.values()):
            shape = ModWebConfigEditorShape.MULTI_FOLDER_SINGLE_FILE
        else:
            shape = ModWebConfigEditorShape.MULTI_FOLDER_MULTI_FILE
        return ModWebConfigEditorLayout(
            shape=shape,
            selected_root_id=configs[0].root_id,
            selected_config_id=configs[0].id,
        )

    @classmethod
    def _config_single_file_root_options(
        cls, configs_by_root: Mapping[str, tuple[NodeConfigEntry, ...]]
    ) -> tuple[ModWebSearchOption, ...]:
        options: list[ModWebSearchOption] = []
        for root_id, entries in configs_by_root.items():
            if len(entries) != 1:
                raise ValueError(f"Config root {root_id!r} does not contain exactly one file.")
            entry: NodeConfigEntry = entries[0]
            options.append(
                ModWebSearchOption(
                    option_id=entry.id,
                    label=entry.root_label,
                    search_text=" ".join((entry.root_label, root_id, entry.relative_path, entry.label)).casefold(),
                )
            )
        return tuple[ModWebSearchOption, ...](options)

    @classmethod
    def _config_file_options(cls, configs: tuple[NodeConfigEntry, ...]) -> tuple[ModWebSearchOption, ...]:
        return tuple[ModWebSearchOption, ...](
            ModWebSearchOption(
                option_id=entry.id,
                label=entry.relative_path,
                search_text=" ".join((entry.root_label, entry.relative_path, entry.label)).casefold(),
            )
            for entry in configs
        )

    @classmethod
    def _config_options(cls, configs: tuple[NodeConfigEntry, ...]) -> tuple[ModWebSearchOption, ...]:
        return tuple[ModWebSearchOption, ...](
            ModWebSearchOption(
                option_id=entry.id,
                label=cls._config_option_label(entry),
                search_text=" ".join((entry.root_label, entry.relative_path, entry.label)).casefold(),
            )
            for entry in configs
        )

    @staticmethod
    def _save_option_label(entry: NodeSaveEntry) -> str:
        return f"{entry.root_label} / {entry.relative_path}"

    @staticmethod
    def _save_card_description(*, model: ModWebBasePageModel, save_count: int) -> str:
        if save_count == 0 and model.supports_save_uploads:
            return "No saves are currently available. Upload one to seed this app."
        if save_count == 0:
            return "No save files are currently available for this app."
        if model.supports_save_uploads and model.supports_save_rename:
            return "Download the current save, upload replacements, or rename supported entries."
        if model.supports_save_uploads:
            return "Download the current save or upload a replacement."
        if model.supports_save_rename:
            return "Browse, download, and rename the current save artifacts."
        return "Browse and download the current save artifacts."

    @staticmethod
    def _save_detail_path_text(*, save: NodeSaveEntry, root_count: int) -> str | None:
        if root_count > 1:
            return f"{save.root_label} / {save.relative_path}"
        if save.relative_path != save.label:
            return save.relative_path
        return None

    @classmethod
    def _save_options(cls, saves: tuple[NodeSaveEntry, ...]) -> tuple[ModWebSearchOption, ...]:
        return tuple[ModWebSearchOption, ...](
            ModWebSearchOption(
                option_id=entry.id,
                label=cls._save_option_label(entry),
                search_text=" ".join((entry.root_label, entry.relative_path, entry.label, entry.kind)).casefold(),
            )
            for entry in saves
        )

    @staticmethod
    def _save_shows_size_badge(save: NodeSaveEntry) -> bool:
        return save.size_text.strip().casefold() != save.kind.strip().casefold()

    @staticmethod
    def _setting_option_label(entry: NodeSettingEntry) -> str:
        return entry.label

    @staticmethod
    def _setting_search_text(entry: NodeSettingEntry) -> str:
        return " ".join(filter(None, (entry.label, entry.key))).casefold()

    @classmethod
    def _setting_options(cls, settings: tuple[NodeSettingEntry, ...]) -> tuple[ModWebSearchOption, ...]:
        return tuple[ModWebSearchOption, ...](
            ModWebSearchOption(
                option_id=entry.key,
                label=cls._setting_option_label(entry),
                search_text=cls._setting_search_text(entry),
            )
            for entry in settings
        )

    @staticmethod
    def _search_query_tokens(search_query: str) -> tuple[str, ...]:
        return tuple[str, ...](token for token in search_query.casefold().split() if token)

    @staticmethod
    def _search_option_matches(option: ModWebSearchOption, *, query_tokens: tuple[str, ...]) -> bool:
        return all(token in option.search_text for token in query_tokens)

    @classmethod
    def _matching_search_options(
        cls,
        *,
        options: tuple[ModWebSearchOption, ...],
        search_query: str,
    ) -> tuple[ModWebSearchOption, ...]:
        query_tokens: tuple[str, ...] = cls._search_query_tokens(search_query)
        return tuple[ModWebSearchOption, ...](
            option for option in options if cls._search_option_matches(option, query_tokens=query_tokens)
        )

    @classmethod
    def _filtered_search_options(
        cls,
        *,
        options: tuple[ModWebSearchOption, ...],
        search_query: str,
        selected_id: str | None,
    ) -> dict[str, str]:
        matching_options: tuple[ModWebSearchOption, ...] = cls._matching_search_options(
            options=options, search_query=search_query
        )
        filtered: dict[str, str] = {option.option_id: option.label for option in matching_options}
        if selected_id is None or selected_id in filtered:
            return filtered
        selected_option: ModWebSearchOption | None = next(
            (option for option in options if option.option_id == selected_id), None
        )
        if selected_option is None:
            return filtered
        return {selected_option.option_id: selected_option.label, **filtered}

    @classmethod
    def _filter_setting_entries(
        cls,
        *,
        settings: tuple[NodeSettingEntry, ...],
        options: tuple[ModWebSearchOption, ...],
        search_query: str,
    ) -> tuple[NodeSettingEntry, ...]:
        matching_ids: set[str] = {
            option.option_id for option in cls._matching_search_options(options=options, search_query=search_query)
        }
        if not matching_ids and cls._search_query_tokens(search_query):
            return ()
        return tuple[NodeSettingEntry, ...](
            setting for setting in settings if setting.key in matching_ids or not matching_ids
        )

    @classmethod
    def _filter_save_entries(
        cls,
        *,
        saves: tuple[NodeSaveEntry, ...],
        options: tuple[ModWebSearchOption, ...],
        search_query: str,
    ) -> tuple[NodeSaveEntry, ...]:
        matching_ids: set[str] = {
            option.option_id for option in cls._matching_search_options(options=options, search_query=search_query)
        }
        if not matching_ids and cls._search_query_tokens(search_query):
            return ()
        return tuple[NodeSaveEntry, ...](save for save in saves if save.id in matching_ids or not matching_ids)

    @staticmethod
    def _setting_choice_for_value(setting: NodeSettingEntry, value: str) -> NodeSettingChoice | None:
        target: str = value.strip()
        if not target:
            return None
        return next(
            (
                choice
                for choice in setting.choices
                if choice.label.casefold() == target.casefold() or choice.raw_value.casefold() == target.casefold()
            ),
            None,
        )

    @staticmethod
    def _parse_boolean_text(raw_value: str) -> bool:
        value: str = raw_value.strip().casefold()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{raw_value!r} is not a recognised boolean value.")

    @classmethod
    def _is_boolean_setting(cls, setting: NodeSettingEntry) -> bool:
        return setting.type_name.casefold() == "bool"

    @classmethod
    def _setting_control_kind(cls, setting: NodeSettingEntry) -> ModWebSettingControlKind:
        if cls._is_boolean_setting(setting):
            return ModWebSettingControlKind.BOOLEAN_SWITCH
        if setting.choices and setting.strict_choice:
            return ModWebSettingControlKind.CHOICE_SELECT
        return ModWebSettingControlKind.TEXT_INPUT

    @classmethod
    def _setting_current_control_value(cls, setting: NodeSettingEntry) -> bool | str:
        if cls._setting_control_kind(setting) is ModWebSettingControlKind.BOOLEAN_SWITCH:
            return cls._setting_switch_value(setting)
        return setting.current_input_value

    @classmethod
    def _setting_control_value_to_submit(cls, setting: NodeSettingEntry, value: bool | str) -> str:
        if cls._setting_control_kind(setting) is ModWebSettingControlKind.BOOLEAN_SWITCH:
            if not isinstance(value, bool):
                raise TypeError(f"Boolean setting {setting.key!r} requires a bool value.")
            return cls._setting_boolean_submit_value(setting, value)
        if not isinstance(value, str):
            raise TypeError(f"Setting {setting.key!r} requires a string value.")
        return value.strip()

    @staticmethod
    def _console_card_description(*, action_count: int) -> str:
        if action_count == 1:
            return "Run the single curated console action exposed by this app."
        return f"Run any of the {action_count} curated console actions exposed by this app."

    @staticmethod
    def _console_action_count_badge_text(*, action_count: int) -> str:
        action_label = "action" if action_count == 1 else "actions"
        return f"{action_count} console {action_label}"

    @staticmethod
    def _console_action_runtime_badge(
        *,
        action: NodeConsoleActionEntry,
        app_stats: NodeAppRuntimeSummary | None,
    ) -> _ModWebBadgeSpec | None:
        if not action.requires_running:
            return None
        if app_stats is None:
            return _ModWebBadgeSpec(text="Runtime Unknown", tone="warn")
        if app_stats.transition_state is NodeAppTransitionState.STOPPING:
            return _ModWebBadgeSpec(text="Stopping", tone="warn")
        if app_stats.transition_state is NodeAppTransitionState.STARTING:
            return _ModWebBadgeSpec(text="Starting", tone="purple")
        if app_stats.running:
            return _ModWebBadgeSpec(text="Running", tone="grey")
        if not app_stats.enabled:
            return _ModWebBadgeSpec(text="Disabled", tone="red")
        return _ModWebBadgeSpec(text="Stopped", tone="warn")

    @staticmethod
    def _console_action_can_execute(
        *,
        action: NodeConsoleActionEntry,
        app_stats: NodeAppRuntimeSummary | None,
    ) -> bool:
        if not action.can_run:
            return False
        if not action.requires_running:
            return True
        if app_stats is None:
            return False
        if app_stats.transition_state is not NodeAppTransitionState.NONE:
            return False
        return app_stats.running

    @staticmethod
    def _console_action_status_text(
        *,
        action: NodeConsoleActionEntry,
        app_friendly: str,
        app_stats: NodeAppRuntimeSummary | None,
    ) -> str:
        if not action.can_run:
            return f"{action.power_level_label} access is required to run this action."
        if not action.requires_running:
            return "Run the action against the selected app instance."
        if app_stats is None:
            return "App runtime status is unavailable. Refresh and try again."
        if app_stats.transition_state is NodeAppTransitionState.STARTING:
            return f"{app_friendly} is starting. Wait until it is running before using this action."
        if app_stats.transition_state is NodeAppTransitionState.STOPPING:
            return f"{app_friendly} is stopping. Wait until it settles before using this action."
        if app_stats.running:
            return "Run the action against the selected app instance."
        if not app_stats.enabled:
            return f"{app_friendly} is disabled. Enable and start it before using this action."
        return f"{app_friendly} must be running before this action can be used."

    @staticmethod
    def _console_action_result_for_selection(
        *,
        selected_action_key: str,
        last_result_action_key: str | None,
        last_result: NodeConsoleActionExecutionResult | None,
    ) -> NodeConsoleActionExecutionResult | None:
        if last_result is None or last_result_action_key != selected_action_key:
            return None
        return last_result

    @staticmethod
    def _console_action_input_props(parameter: NodeConsoleActionParameter) -> str:
        if parameter.multiline:
            return (
                "filled square clearable hide-bottom-space color=accent type=textarea autogrow "
                "input-style=min-height:7rem;"
            )
        if parameter.value_type_name.casefold() == "int":
            return "filled square dense clearable hide-bottom-space color=accent type=number inputmode=numeric step=1"
        return "filled square dense clearable hide-bottom-space color=accent"

    @staticmethod
    def _console_action_select_props(*, prefix: Literal["", "Choice", "Recent"], clearable: bool) -> str:
        clearable_token: Literal[" clearable", ""] = " clearable" if clearable else ""
        return (
            f"filled square dense{clearable_token} hide-bottom-space color=accent options-dense "
            f"popup-content-class=mod-setting-menu prefix={prefix}"
        )

    @staticmethod
    def _console_action_permission_badge_tone(action: NodeConsoleActionEntry) -> BadgeTone:
        return "grey" if action.can_run else "warn"

    @staticmethod
    def _console_action_result_badge_tone(result: NodeConsoleActionExecutionResult) -> BadgeTone:
        return "purple" if result.success else "warn"

    @staticmethod
    def _console_action_source_badge_tone(result: NodeConsoleActionExecutionResult) -> BadgeTone:
        if result.source.value == "rcon":
            return "purple"
        if result.source.value == "telnet":
            return "grey"
        return "black"

    @staticmethod
    def _setting_text_input_props(setting: NodeSettingEntry) -> str:
        if setting.type_name.casefold() == "int":
            return "filled square dense clearable hide-bottom-space color=accent type=number inputmode=numeric step=1"
        if setting.value_is_hidden:
            return "filled square dense clearable hide-bottom-space color=accent type=password autocomplete=off"
        return "filled square dense clearable hide-bottom-space color=accent"

    @staticmethod
    def _setting_select_props() -> str:
        return "filled square dense clearable hide-bottom-space color=accent options-dense popup-content-class=mod-setting-menu"

    @staticmethod
    def _setting_choice_select_props() -> str:
        return "filled square dense hide-bottom-space color=accent options-dense popup-content-class=mod-setting-menu"

    @staticmethod
    def _setting_aux_select_props(*, prefix: Literal["Preset", "Recent"]) -> str:
        return f"{ModWebEditorsMixin._setting_select_props()} prefix={prefix}"

    @staticmethod
    def _fake_chat_select_props(*, clearable: bool) -> str:
        clearable_token: Literal[" clearable", ""] = " clearable" if clearable else ""
        return (
            f"filled square dense{clearable_token} hide-bottom-space color=accent "
            "options-dense popup-content-class=mod-fake-chat-menu"
        )

    @staticmethod
    def _config_select_props(*, clearable: bool) -> str:
        clearable_token: Literal[" clearable", ""] = " clearable" if clearable else ""
        return f"outlined{clearable_token} options-dense popup-content-class=mod-notepad-menu"

    @staticmethod
    def _register_client_cleanup(*, ui: ModWebUi, cleanup: Callable[[], None]) -> None:
        ui_context: object | None = getattr(cast(object, ui), "context", None)
        client: object | None = getattr(ui_context, "client", None) if ui_context is not None else None
        if client is None:
            return
        on_delete_candidate: object | None = getattr(client, "on_delete", None)
        if not callable(on_delete_candidate):
            return
        on_delete = cast(Callable[[Callable[..., object]], None], on_delete_candidate)

        def _run_cleanup(*_args: object) -> None:
            cleanup()

        on_delete(_run_cleanup)

    @staticmethod
    def _register_timer_cleanup(*, ui: ModWebUi, timer: object) -> None:
        cancel = getattr(timer, "cancel", None)
        if not callable(cancel):
            return

        def _cancel_timer() -> None:
            cancel(with_current_invocation=True)

        ModWebEditorsMixin._register_client_cleanup(ui=ui, cleanup=_cancel_timer)

    @staticmethod
    def _setting_control_surface_classes(*, can_edit: bool) -> str:
        classes = "mod-setting-control-surface"
        if not can_edit:
            return f"{classes} locked"
        return classes

    @staticmethod
    def _setting_text_validation_message(setting: NodeSettingEntry, value: str) -> str | None:
        trimmed_value: str = value.strip()
        if not trimmed_value and setting.allows_blank_input:
            return None
        if not trimmed_value:
            return "Value required."
        if setting.type_name.casefold() == "int":
            try:
                int(trimmed_value)
            except ValueError:
                return "Enter a whole number."
        return None

    @staticmethod
    def _setting_permission_badge_tone(setting: NodeSettingEntry) -> BadgeTone:
        return "grey" if setting.can_edit else "warn"

    @classmethod
    def _hidden_setting_display_text(cls, setting: NodeSettingEntry, *, variant: int = 0) -> str:
        if setting.is_sensitive and variant == 0:
            return setting.value_text
        digest: bytes = hashlib.sha256(
            f"{setting.key}:{setting.permission_level_name}:{setting.type_name}:{variant}".encode()
        ).digest()
        glyphs: LiteralString = "".join(
            _HIDDEN_SETTING_GLYPHS[byte % len(_HIDDEN_SETTING_GLYPHS)] for byte in digest[:12]
        )
        return " ".join((glyphs[:4], glyphs[4:8], glyphs[8:12]))

    @staticmethod
    def _setting_secret_config(setting: NodeSettingEntry) -> _SettingSecretConfig:
        digest: bytes = hashlib.sha256(
            f"{setting.key}:{setting.permission_level_name}:{setting.type_name}".encode()
        ).digest()

        def scaled_value(*, index: int, base: float, span: float) -> float:
            return base + (digest[index] / 255.0) * span

        def seconds(*, value: float) -> str:
            return f"{value:.2f}s"

        def rem(*, index: int, base: float, span: float) -> str:
            return f"{scaled_value(index=index, base=base, span=span):.3f}rem"

        def scalar(*, index: int, base: float, span: float) -> str:
            return f"{scaled_value(index=index, base=base, span=span):.3f}"

        flicker_duration_seconds: float = scaled_value(index=0, base=1.95, span=0.85)
        flicker_delay_seconds: float = -scaled_value(index=1, base=0.15, span=2.8)
        shift_duration_seconds_a: float = scaled_value(index=2, base=1.55, span=0.9)
        shift_delay_seconds_a: float = -scaled_value(index=3, base=0.1, span=2.4)
        shift_duration_seconds_b: float = scaled_value(index=4, base=1.75, span=1.05)
        shift_delay_seconds_b: float = -scaled_value(index=5, base=0.2, span=2.9)
        cycle_duration_seconds: float = scaled_value(index=16, base=4.8, span=1.9)
        cycle_delay_seconds: float = -scaled_value(index=17, base=0.2, span=5.2)

        style_parts: tuple[str, str, str, str, str, str, str, str, str, str, str, str, str, str, str, str, str, str] = (
            f"--mod-setting-secret-flicker-duration: {seconds(value=flicker_duration_seconds)}",
            f"--mod-setting-secret-flicker-delay: {seconds(value=flicker_delay_seconds)}",
            f"--mod-setting-secret-shift-duration-a: {seconds(value=shift_duration_seconds_a)}",
            f"--mod-setting-secret-shift-delay-a: {seconds(value=shift_delay_seconds_a)}",
            f"--mod-setting-secret-shift-duration-b: {seconds(value=shift_duration_seconds_b)}",
            f"--mod-setting-secret-shift-delay-b: {seconds(value=shift_delay_seconds_b)}",
            f"--mod-setting-secret-cycle-duration: {seconds(value=cycle_duration_seconds)}",
            f"--mod-setting-secret-cycle-delay: {seconds(value=cycle_delay_seconds)}",
            f"--mod-setting-secret-main-kick-x: {rem(index=6, base=0.028, span=0.04)}",
            f"--mod-setting-secret-main-kick-y: {rem(index=7, base=0.006, span=0.018)}",
            f"--mod-setting-secret-main-blur: {rem(index=8, base=0.006, span=0.014)}",
            f"--mod-setting-secret-shift-x-a: {rem(index=9, base=0.075, span=0.055)}",
            f"--mod-setting-secret-shift-y-a: {rem(index=10, base=0.008, span=0.022)}",
            f"--mod-setting-secret-shift-x-b: {rem(index=11, base=0.082, span=0.06)}",
            f"--mod-setting-secret-shift-y-b: {rem(index=12, base=0.01, span=0.026)}",
            f"--mod-setting-secret-shadow-opacity-a: {scalar(index=13, base=0.14, span=0.16)}",
            f"--mod-setting-secret-shadow-opacity-b: {scalar(index=14, base=0.12, span=0.15)}",
            f"--mod-setting-secret-shadow-blur: {rem(index=15, base=0.008, span=0.016)}",
        )
        return _SettingSecretConfig(
            style="; ".join(style_parts),
            cycle_duration_seconds=cycle_duration_seconds,
            cycle_delay_seconds=cycle_delay_seconds,
        )

    @classmethod
    def _hidden_setting_cycle_texts(
        cls, setting: NodeSettingEntry, *, count: int = _HIDDEN_SETTING_CYCLE_VARIANT_COUNT
    ) -> tuple[str, ...]:
        if count < 1:
            raise ValueError("Hidden setting cycle must contain at least one text variant.")
        base_text = cls._hidden_setting_display_text(setting)
        mutable_positions: tuple[int, ...] = tuple[int, ...](
            index for index, char in enumerate(base_text) if char != " "
        )
        if not mutable_positions:
            return tuple[str, ...](base_text for _ in range(count))
        digest: bytes = hashlib.sha256(
            f"{setting.key}:{setting.permission_level_name}:{setting.type_name}:cycle".encode()
        ).digest()
        variants: list[str] = [base_text]
        current_chars: list[str] = list[str](base_text)
        for variant_index in range(1, count):
            replacement_count: int = 1 + digest[(variant_index - 1) % len(digest)] % 2
            for mutation_index in range(replacement_count):
                digest_index: int = ((variant_index - 1) * 4 + mutation_index * 2) % len(digest)
                position: int = mutable_positions[digest[digest_index] % len(mutable_positions)]
                glyph_index: int = digest[(digest_index + 1) % len(digest)] % len(_HIDDEN_SETTING_GLYPHS)
                replacement: LiteralString = _HIDDEN_SETTING_GLYPHS[glyph_index]
                if replacement == current_chars[position]:
                    replacement = _HIDDEN_SETTING_GLYPHS[
                        (glyph_index + variant_index + mutation_index + 1) % len(_HIDDEN_SETTING_GLYPHS)
                    ]
                current_chars[position] = replacement
            variants.append("".join(current_chars))
        return tuple[str, ...](variants)

    @classmethod
    def _setting_secret_style(cls, setting: NodeSettingEntry) -> str:
        return cls._setting_secret_config(setting).style

    @staticmethod
    def _hidden_setting_reveal_class_suffix(setting: NodeSettingEntry) -> str:
        if not setting.can_reveal_hidden_text:
            return ""
        return " mod-setting-meta-secret-revealable"

    @staticmethod
    def _hidden_setting_reveal_container_attrs(setting: NodeSettingEntry) -> str:
        if not setting.can_reveal_hidden_text:
            return ""
        return ' tabindex="0"'

    @staticmethod
    def _hidden_setting_reveal_markup(setting: NodeSettingEntry) -> str:
        if not setting.can_reveal_hidden_text:
            return ""
        return f'<span class="mod-setting-meta-secret-reveal">{escape(setting.revealed_value_text)}</span>'

    @classmethod
    def _render_hidden_sensitive_meta_value(
        cls, *, ui: ModWebUi, setting: NodeSettingEntry, secret_config: _SettingSecretConfig
    ) -> None:
        main_text: str = escape(cls._hidden_setting_display_text(setting))
        shadow_text_a: str = escape(cls._hidden_setting_display_text(setting, variant=1))
        shadow_text_b: str = escape(cls._hidden_setting_display_text(setting, variant=2))
        secret_style: str = escape(secret_config.style)
        reveal_class_suffix: str = cls._hidden_setting_reveal_class_suffix(setting)
        reveal_container_attrs: str = cls._hidden_setting_reveal_container_attrs(setting)
        reveal_markup: str = cls._hidden_setting_reveal_markup(setting)
        ui.html(
            "".join(
                (
                    (
                        f'<div class="mod-setting-meta-value mod-setting-meta-secret{reveal_class_suffix}"'
                        f'{reveal_container_attrs} style="{secret_style}">'
                    ),
                    f'<span class="mod-setting-meta-secret-layer mod-setting-meta-secret-main">{main_text}</span>',
                    (
                        '<span class="mod-setting-meta-secret-layer mod-setting-meta-secret-shadow '
                        f'mod-setting-meta-secret-shadow-a" aria-hidden="true">{shadow_text_a}</span>'
                    ),
                    (
                        '<span class="mod-setting-meta-secret-layer mod-setting-meta-secret-shadow '
                        f'mod-setting-meta-secret-shadow-b" aria-hidden="true">{shadow_text_b}</span>'
                    ),
                    reveal_markup,
                    "</div>",
                )
            )
        )

    @classmethod
    def _render_hidden_non_sensitive_meta_value(
        cls, *, ui: ModWebUi, setting: NodeSettingEntry, secret_config: _SettingSecretConfig
    ) -> None:
        cycle_texts: tuple[str, ...] = cls._hidden_setting_cycle_texts(setting)
        cycle_step_seconds: float = secret_config.cycle_duration_seconds / len(cycle_texts)
        secret_style: str = escape(secret_config.style)
        reveal_class_suffix: str = cls._hidden_setting_reveal_class_suffix(setting)
        reveal_container_attrs: str = cls._hidden_setting_reveal_container_attrs(setting)
        reveal_markup: str = cls._hidden_setting_reveal_markup(setting)
        html_parts: list[str] = [
            (
                f'<div class="mod-setting-meta-value mod-setting-meta-secret-cycle{reveal_class_suffix}"'
                f'{reveal_container_attrs} style="{secret_style}">'
            ),
            (f'<span class="mod-setting-meta-secret-cycle-sizer" aria-hidden="true">{escape(cycle_texts[0])}</span>'),
        ]
        for index, cycle_text in enumerate[str](cycle_texts):
            token_delay_seconds: float = secret_config.cycle_delay_seconds - cycle_step_seconds * index
            html_parts.append(
                "".join(
                    (
                        '<span class="mod-setting-meta-secret-cycle-token" ',
                        f'style="--mod-setting-secret-cycle-token-delay: {token_delay_seconds:.2f}s" ',
                        f'data-text="{escape(cycle_text)}">{escape(cycle_text)}</span>',
                    )
                )
            )
        html_parts.append(reveal_markup)
        html_parts.append("</div>")
        ui.html("".join(html_parts))

    @classmethod
    def _render_setting_meta_value(cls, *, ui: ModWebUi, setting: NodeSettingEntry) -> None:
        if setting.value_is_hidden:
            secret_config: _SettingSecretConfig = cls._setting_secret_config(setting)
            if setting.is_sensitive:
                cls._render_hidden_sensitive_meta_value(ui=ui, setting=setting, secret_config=secret_config)
            else:
                cls._render_hidden_non_sensitive_meta_value(ui=ui, setting=setting, secret_config=secret_config)
            return
        ui.label(setting.value_text).classes("mod-setting-meta-value mod-setting-meta-current")

    @staticmethod
    def _update_setting_text_input_feedback(
        *,
        input_control: Input,
        feedback_label: Label | None,
        message: str | None,
    ) -> None:
        invalid_classes = "mod-setting-field-invalid mod-setting-field-shake"
        if message is None:
            input_control.classes(remove=invalid_classes)
            if feedback_label is not None:
                feedback_label.set_text("")
                feedback_label.classes(remove="active")
            return
        input_control.classes(remove="mod-setting-field-shake")
        input_control.classes(add=invalid_classes)
        if feedback_label is not None:
            feedback_label.set_text(message)
            feedback_label.classes(add="active")

    @classmethod
    def _setting_switch_value(cls, setting: NodeSettingEntry) -> bool:
        if not cls._is_boolean_setting(setting):
            raise ValueError(f"Setting {setting.key!r} is not a boolean setting.")
        matched_choice: NodeSettingChoice | None = cls._setting_choice_for_value(setting, setting.current_input_value)
        if matched_choice is not None:
            return cls._parse_boolean_text(matched_choice.raw_value)
        if not setting.current_input_value.strip():
            return False
        return cls._parse_boolean_text(setting.current_input_value)

    @classmethod
    def _setting_boolean_submit_value(cls, setting: NodeSettingEntry, value: bool) -> str:
        for choice in setting.choices:
            try:
                if cls._parse_boolean_text(choice.raw_value) is value:
                    return choice.raw_value
            except ValueError:
                continue
        return "true" if value else "false"
