from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

from apps._blueprint_files import BlueprintUploadPair, classify_blueprint_upload_filenames

from .constants import (
    _CONFIG_EDITOR_DOCKERFILE_LANGUAGE,
    _CONFIG_EDITOR_LANGUAGE_BY_SUFFIX,
    _CONFIG_EDITOR_THEME,
    _DIRECT_UPLOAD_TOKEN_REFRESH_SECONDS,
    _HIDDEN_SETTING_CYCLE_VARIANT_COUNT,
    _HIDDEN_SETTING_GLYPHS,
    _SAME_ORIGIN_NODE_PROXY_BASE,
    log,
)
from .nicegui_protocols import (
    ModWebNotificationType,
    ModWebUi,
    ModWebValueContainer,
    _value_as_object,
    _value_as_text,
)
from .runtime_imports import (
    Access_Control,
    App_Manager,
    BadgeTone,
    Button,
    Callable,
    CodeMirror,
    GatewayBot,
    Html,
    Input,
    Label,
    Literal,
    LiteralString,
    Mapping,
    ModPlacement,
    ModType,
    ModWebUser,
    NodeApiScope,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeBlueprintEntry,
    NodeBlueprintFileEntry,
    NodeBlueprintList,
    NodeBlueprintMutationResult,
    NodeConfigContent,
    NodeConfigEntry,
    NodeConfigList,
    NodeConsoleActionEntry,
    NodeConsoleActionExecutionResult,
    NodeConsoleActionList,
    NodeConsoleActionParameter,
    NodeConsoleStdoutSnapshot,
    NodeModEntry,
    NodeModUploadBatchResult,
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
    Textarea,
    Timer,
    TypeVar,
    Upload,
    asyncio,
    cached_member_role_color,
    cast,
    color_int_to_hex,
    config,
    escape,
    hashlib,
    hikari,
    json,
    quote,
    tempfile,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModWebBasePageModel,
    ModWebConfigEditorLayout,
    ModWebConfigEditorShape,
    ModWebDirectUploadTarget,
    ModWebFileSortOrder,
    ModWebNodeLink,
    ModWebModSortOrder,
    ModWebPageModel,
    ModWebSearchOption,
    ModWebSettingControlKind,
    _ModWebBadgeSpec,
    _SettingSecretConfig,
)


_SERVER_RENDERED_LIST_PAGE_SIZE = 40

_SortableFileEntry = TypeVar("_SortableFileEntry", NodeSaveEntry, NodeBlueprintEntry)

if TYPE_CHECKING:
    from nicegui.elements.codemirror.codemirror import SUPPORTED_LANGUAGES
    from nicegui.elements.dialog import Dialog
    from nicegui.elements.switch import Switch
    from nicegui.elements.upload_files import FileUpload
    from nicegui.events import MultiUploadEventArguments

_SEVENDAYS_TRADER_BIOME_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "TraderRektBiome",
        "TraderJenBiome",
        "TraderBobBiome",
        "TraderHughBiome",
        "TraderJoelBiome",
    }
)
_CONSOLE_STDOUT_MAX_LINES = 200
_CONSOLE_STDOUT_HEIGHT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("18rem", "Compact"),
    ("26rem", "Default"),
    ("36rem", "Tall"),
    ("48rem", "XL"),
)
_UPLOAD_PROGRESS_CHUNK_BYTES = 1024 * 1024
_UPLOAD_RECEIVE_PROGRESS_PERCENT = 72.0
_UPLOAD_APPLY_PROGRESS_PERCENT = 92.0
_TRANSFER_CAPACITY_WAIT_SECONDS = 0.2


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

        current_model: ModWebBasePageModel = model
        current_console_actions: NodeConsoleActionList = model.console_actions
        selected_action_key: str | None = (
            current_console_actions.actions[0].key if current_console_actions.actions else None
        )
        draft_values: dict[str, str] = {}
        last_result: NodeConsoleActionExecutionResult | None = None
        last_result_action_key: str | None = None
        action_in_flight = False
        open_console_popup_count = 0
        queued_console_refresh = False
        stdout_snapshot: NodeConsoleStdoutSnapshot | None = None
        stdout_feed_id = (
            f"mod-console-stdout-{hashlib.sha1(f'{model.node_name}:{model.app_name}'.encode('utf-8')).hexdigest()[:12]}"
        )
        stdout_height_value: str = "26rem"
        stdout_feed: Html | None = None
        console_actions_available: bool = bool(current_console_actions.actions)

        def _ensure_console_stdout_client_script() -> None:
            ui.add_head_html(
                """
                <script>
                window.modWebConsoleFeed = (() => {
                    const bindVersion = '2026-06-17-atomic-stdout';
                    const bottomThresholdPx = 24;
                    const scheduledById = new Map();
                    const programmaticScrollIds = new Set();
                    const resizeBoundIds = new Set();
                    const get = (elementId) => document.getElementById(elementId);
                    const clampScrollTop = (element, value) =>
                      Math.max(0, Math.min(Math.max(0, element.scrollHeight - element.clientHeight), value));
                    const clearScheduled = (elementId) => {
                      const state = scheduledById.get(elementId);
                      if (!state) {
                        return;
                      }
                      for (const frameId of state.frameIds) {
                        cancelAnimationFrame(frameId);
                      }
                      for (const timeoutId of state.timeoutIds) {
                        clearTimeout(timeoutId);
                      }
                      scheduledById.delete(elementId);
                    };
                    const setScrollTop = (elementId, element, value) => {
                      programmaticScrollIds.add(elementId);
                      element.scrollTop = clampScrollTop(element, value);
                      sync(elementId);
                      requestAnimationFrame(() => programmaticScrollIds.delete(elementId));
                    };
                    const schedule = (elementId, task) => {
                      clearScheduled(elementId);
                      const frameIds = [];
                      const timeoutIds = [];
                      task();
                      frameIds.push(requestAnimationFrame(task));
                      frameIds.push(requestAnimationFrame(() => requestAnimationFrame(task)));
                      timeoutIds.push(setTimeout(task, 0));
                      timeoutIds.push(setTimeout(task, 120));
                      timeoutIds.push(setTimeout(task, 320));
                      scheduledById.set(elementId, { frameIds, timeoutIds });
                    };
                    const sync = (elementId) => {
                      const element = get(elementId);
                      if (!element) {
                        return false;
                      }
                      const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
                      const pinned = remaining <= bottomThresholdPx;
                      element.dataset.modConsolePinned = pinned ? '1' : '0';
                      return pinned;
                    };
                    const captureState = (elementId) => {
                      const element = get(elementId);
                      if (!element) {
                        return null;
                      }
                      const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
                      return {
                        wasPinned: remaining <= bottomThresholdPx,
                        scrollTop: element.scrollTop,
                      };
                    };
                    const settleBottom = (elementId) => {
                      const element = get(elementId);
                      if (!element) {
                        return;
                      }
                      setScrollTop(elementId, element, element.scrollHeight);
                    };
                    const jump = (elementId) => {
                      schedule(elementId, () => settleBottom(elementId));
                    };
                    const restore = (elementId, previousState) => {
                      schedule(elementId, () => {
                        const element = get(elementId);
                        if (!element) {
                          return;
                        }
                        setScrollTop(elementId, element, previousState.scrollTop);
                      });
                    };
                    const setHeight = (elementId, heightValue) => {
                      const element = get(elementId);
                      if (!element) {
                        return;
                      }
                      element.style.height = heightValue;
                      element.style.maxHeight = heightValue;
                      jump(elementId);
                    };
                    const bind = (elementId) => {
                      const element = get(elementId);
                      if (!element) {
                        return;
                      }
                      if (element.dataset.modConsoleBound !== bindVersion) {
                        element.dataset.modConsoleBound = bindVersion;
                        element.addEventListener('scroll', () => {
                          if (!programmaticScrollIds.has(elementId)) {
                            clearScheduled(elementId);
                          }
                          sync(elementId);
                        }, { passive: true });
                      }
                      if (!resizeBoundIds.has(elementId)) {
                        resizeBoundIds.add(elementId);
                        window.addEventListener(
                          'resize',
                          () => {
                            const currentElement = get(elementId);
                            if (!currentElement) {
                              return;
                            }
                            if (currentElement.dataset.modConsolePinned !== '0') {
                              jump(elementId);
                            } else {
                              sync(elementId);
                            }
                          },
                          { passive: true },
                        );
                      }
                      sync(elementId);
                    };
                    const update = (elementId, text, forceScroll) => {
                      bind(elementId);
                      const element = get(elementId);
                      if (!element) {
                        return;
                      }
                      const previousState = captureState(elementId);
                      const code = element.querySelector('code') || element;
                      code.textContent = text;
                      if (forceScroll || previousState?.wasPinned) {
                        jump(elementId);
                        return;
                      }
                      if (previousState) {
                        restore(elementId, previousState);
                      }
                      sync(elementId);
                    };
                    return { bind, jump, setHeight, update };
                })();
                </script>
                """
            )

        def _console_stdout_text(snapshot: NodeConsoleStdoutSnapshot | None) -> str:
            if snapshot is None:
                return "Connecting..."
            if not snapshot.lines:
                return "Waiting for output..." if snapshot.running else "No output yet."
            return "\n".join(snapshot.lines)

        def _console_stdout_markup(snapshot: NodeConsoleStdoutSnapshot | None) -> str:
            content = _console_stdout_text(snapshot)
            return (
                f'<pre id="{stdout_feed_id}" class="mod-chat-code-block" '
                f'style="height: {stdout_height_value}; max-height: {stdout_height_value}; overflow: auto;"><code>'
                f"{escape(content)}"
                "</code></pre>"
            )

        def _run_stdout_feed_javascript(code: str) -> None:
            target = stdout_feed
            if target is None:
                return
            try:
                target.client.run_javascript(code, timeout=0.1)
            except RuntimeError:
                return

        def _bind_stdout_feed() -> None:
            _run_stdout_feed_javascript(f"window.modWebConsoleFeed?.bind({stdout_feed_id!r});")

        def _jump_stdout_feed_to_bottom() -> None:
            _run_stdout_feed_javascript(f"window.modWebConsoleFeed?.jump({stdout_feed_id!r});")

        def _set_stdout_feed_height(height_value: str) -> None:
            _run_stdout_feed_javascript(f"window.modWebConsoleFeed?.setHeight({stdout_feed_id!r}, {height_value!r});")

        def _update_stdout_feed_text(text: str, *, force_scroll: bool) -> None:
            _run_stdout_feed_javascript(
                (
                    "window.modWebConsoleFeed?.update("
                    f"{stdout_feed_id!r}, {json.dumps(text)}, {str(force_scroll).lower()}"
                    ");"
                )
            )

        def _apply_stdout_snapshot(snapshot: NodeConsoleStdoutSnapshot) -> None:
            nonlocal stdout_snapshot
            stdout_snapshot = snapshot
            if stdout_feed is not None:
                _update_stdout_feed_text(_console_stdout_text(snapshot), force_scroll=False)

        def selected_action() -> NodeConsoleActionEntry | None:
            nonlocal selected_action_key
            if not current_console_actions.actions or selected_action_key is None:
                return None
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
            if not console_actions_available:
                return
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

        def select_stdout_height(event: ModWebValueContainer) -> None:
            nonlocal stdout_height_value
            next_height_value: str = _value_as_text(event).strip()
            valid_height_values: set[str] = {value for value, _label in _CONSOLE_STDOUT_HEIGHT_OPTIONS}
            if next_height_value not in valid_height_values:
                return
            stdout_height_value = next_height_value
            _set_stdout_feed_height(next_height_value)

        from nicegui.context import context as nicegui_context

        notify_client = nicegui_context.client

        def _notify(message: str, *, tone: ModWebNotificationType) -> None:
            with notify_client:
                ui.notify(message, type=tone)

        async def run_selected_action() -> None:
            nonlocal action_in_flight, current_console_actions, last_result, last_result_action_key
            if action_in_flight:
                return
            action = selected_action()
            if action is None:
                return
            if not self._console_action_can_execute(action=action, app_stats=current_model.app_stats):
                _notify(
                    self._console_action_status_text(
                        action=action,
                        app_friendly=current_model.app_friendly,
                        app_stats=current_model.app_stats,
                    ),
                    tone="warning",
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
                    _notify(f"Console action failed: {xcp}", tone="negative")
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
                _notify(result.summary, tone="positive" if result.success else "warning")
            finally:
                action_in_flight = False
                refresh_console_body(force=True)

        with ui.column().classes("w-full gap-3"):
            _ensure_console_stdout_client_script()
            if console_actions_available:
                with ui.card().classes(self._flat_tab_card_classes()):
                    with ui.column().classes(self._tab_section_body_classes()):
                        self._render_flat_tab_header(
                            ui=ui,
                            title="Console",
                            description=self._console_card_description(
                                action_count=len(current_console_actions.actions)
                            ),
                        )

                        @ui.refreshable
                        def _console_card_body() -> None:
                            action = selected_action()
                            if action is None:
                                return
                            action_can_execute = self._console_action_can_execute(
                                action=action, app_stats=current_model.app_stats
                            )
                            action_result: NodeConsoleActionExecutionResult | None = (
                                self._console_action_result_for_selection(
                                    selected_action_key=action.key,
                                    last_result_action_key=last_result_action_key,
                                    last_result=last_result,
                                )
                            )
                            action_status_text: str = self._console_action_status_text(
                                action=action,
                                app_friendly=current_model.app_friendly,
                                app_stats=current_model.app_stats,
                            )
                            runtime_badge: _ModWebBadgeSpec | None = self._console_action_runtime_badge(
                                action=action, app_stats=current_model.app_stats
                            )
                            parameter: NodeConsoleActionParameter | None = action.parameter
                            current_value: str = draft_values.get(action.key, "")
                            with ui.column().classes("w-full gap-3"):
                                with ui.row().classes(
                                    "mod-tab-toolbar mod-tab-toolbar-surface mod-inline-toolbar w-full"
                                ):
                                    action_select: Select = bind_console_popup_refresh_lock(
                                        ui.select(
                                            {entry.key: entry.label for entry in current_console_actions.actions},
                                            value=action.key,
                                            on_change=select_action,
                                        )
                                        .props(self._setting_choice_select_props())
                                        .classes("mod-console-select mod-console-select-action")
                                    )
                                    if len(current_console_actions.actions) == 1 or action_in_flight:
                                        action_select.disable()
                                    with ui.row().classes("mod-tab-toolbar-actions mod-inline-toolbar-actions"):
                                        run_button: Button = ui.button("Run", on_click=run_selected_action).classes(
                                            "mod-list-button"
                                        )
                                        if action_in_flight:
                                            run_button.set_text("Running")
                                            run_button.disable()
                                        elif not action_can_execute:
                                            run_button.disable()

                                with ui.column().classes("w-full gap-3 mod-tab-toolbar-surface"):
                                    with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                                        with ui.column().classes("gap-1 min-w-0 flex-1"):
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
                                        value_input: Input | None = None
                                        with ui.column().classes("w-full gap-2"):
                                            ui.label(parameter.label).classes(
                                                "text-[0.68rem] uppercase tracking-[0.18em] mod-subtitle"
                                            )
                                            with ui.column().classes(
                                                self._setting_control_surface_classes(can_edit=action.can_run)
                                            ):
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
                                                            {
                                                                choice.raw_value: choice.label
                                                                for choice in parameter.choices
                                                            },
                                                            value=(
                                                                current_value
                                                                if current_value
                                                                in {choice.raw_value for choice in parameter.choices}
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
                                                            placeholder=(
                                                                f"Enter {parameter.label}"
                                                                if action.can_run
                                                                else "Restricted"
                                                            ),
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
                                                            {
                                                                recent_value: recent_value
                                                                for recent_value in parameter.recent_inputs
                                                            },
                                                            value=None,
                                                            on_change=apply_recent_input if action.can_run else None,
                                                        )
                                                        .props(
                                                            self._console_action_select_props(
                                                                prefix="Recent",
                                                                clearable=True,
                                                            )
                                                        )
                                                        .classes("mod-setting-field mod-setting-field-secondary")
                                                    )
                                                    if not action.can_run:
                                                        recent_select.disable()
                                                    if action_in_flight:
                                                        recent_select.disable()

                                            if parameter.description:
                                                ui.label(parameter.description).classes(
                                                    "mod-subtitle text-xs break-all"
                                                )

                                    if action_status_text != "Ready.":
                                        ui.label(action_status_text).classes("mod-subtitle text-sm")

                                if action_result is not None:
                                    with ui.column().classes("w-full gap-2 mod-tab-toolbar-surface"):
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
            else:
                with ui.card().classes(self._flat_tab_card_classes()):
                    with ui.column().classes(self._tab_section_body_classes()):
                        self._render_flat_tab_header(
                            ui=ui,
                            title="Console",
                            description="No console actions available for this app.",
                            secondary_description="Stdout is still available below.",
                        )

            with ui.card().classes(self._flat_tab_card_classes()):
                with ui.column().classes(self._tab_section_body_classes()):
                    with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                        with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                            ui.label("Live stdout tail.").classes("mod-subtitle text-sm w-full")
                            ui.label(f"Latest {_CONSOLE_STDOUT_MAX_LINES} lines.").classes(
                                "mod-subtitle text-xs w-full"
                            )
                        (
                            ui.select(
                                {value: label for value, label in _CONSOLE_STDOUT_HEIGHT_OPTIONS},
                                value=stdout_height_value,
                                on_change=select_stdout_height,
                            )
                            .props(
                                "filled square dense hide-bottom-space color=accent "
                                "options-dense popup-content-class=mod-console-select-menu"
                            )
                            .classes("mod-console-select mod-console-select-compact mod-console-select-black")
                        )
                    stdout_feed = cast(Html, ui.html(_console_stdout_markup(stdout_snapshot)).classes("w-full"))
                    _bind_stdout_feed()
                    _set_stdout_feed_height(stdout_height_value)
                    _jump_stdout_feed_to_bottom()

        loop = asyncio.get_running_loop()

        def _queue_stdout_snapshot(snapshot: NodeConsoleStdoutSnapshot) -> None:
            try:
                loop.call_soon_threadsafe(lambda: _apply_stdout_snapshot(snapshot))
            except RuntimeError:
                return

        try:
            node = self._remote_node_link(model.node_name)
            unsubscribe_stdout = self._create_remote_console_stdout_subscription(
                node=node,
                app_name=model.app_name,
                max_lines=_CONSOLE_STDOUT_MAX_LINES,
                user=user,
                on_update=_queue_stdout_snapshot,
            )
        except Exception as xcp:
            _update_stdout_feed_text(f"Stdout stream unavailable: {xcp}", force_scroll=True)
        else:
            self._register_client_cleanup(ui=ui, cleanup=unsubscribe_stdout)

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
        show_sort: bool = len(save_options) > 1
        show_root_selector: bool = model.supports_save_uploads and len(saves.roots) > 1
        show_upload_action: bool = model.supports_save_uploads and can_write and selected_root_id is not None
        show_write_lock_note: bool = (model.supports_save_uploads or model.supports_save_rename) and not can_write
        current_search_query: str = model.search_query
        current_sort_order: ModWebFileSortOrder = ModWebFileSortOrder.LATEST_MODIFIED
        save_page_number = 1

        root_select: Select | None = None
        save_upload_control: Upload | None = None
        direct_save_transfer_id: int | None = None

        def ensure_direct_save_transfer() -> int | None:
            nonlocal direct_save_transfer_id
            if direct_save_transfer_id is not None:
                return direct_save_transfer_id
            try:
                direct_save_transfer_id = self._start_direct_upload_transfer(
                    model=model,
                    user=user,
                    label="Save upload",
                    detail_text=f"Sending a save directly to {model.app_friendly}.",
                )
            except RuntimeError as xcp:
                ui.notify(f"Upload started, but tray tracking is unavailable: {xcp}", type="warning")
            return direct_save_transfer_id

        def finish_direct_save_transfer(*, error: str | None) -> None:
            nonlocal direct_save_transfer_id
            transfer_id: int | None = direct_save_transfer_id
            direct_save_transfer_id = None
            if transfer_id is None:
                return
            if error is None:
                self._backend.complete_transfer(
                    transfer_id=transfer_id,
                    detail_text=f"Uploaded a save for {model.app_friendly}.",
                )
            else:
                self._backend.fail_transfer(transfer_id=transfer_id, detail_text=error)

        def interrupt_direct_save_transfer() -> None:
            if direct_save_transfer_id is None:
                return
            finish_direct_save_transfer(error="Save upload was interrupted because the app page was closed.")

        def selected_save_root_id() -> str:
            selected_root_value: str | None = (
                selected_root_id if root_select is None else _value_as_text(root_select).strip() or None
            )
            root_id: str | None = selected_root_value or selected_root_id
            if not root_id:
                raise ValueError("Select a save root before uploading.")
            if root_id not in save_root_options:
                raise ValueError(f"Unknown save root: {root_id}")
            return root_id

        def refresh_direct_save_upload_target() -> None:
            if save_upload_control is None:
                raise RuntimeError("Save upload control is not available.")
            root_id: str = selected_save_root_id()
            target: ModWebDirectUploadTarget = self._direct_save_upload_target(model=model, user=user)
            save_upload_control.props["url"] = target.url
            save_upload_control.props["headers"] = [
                {"name": "Authorization", "value": target.authorization_header},
            ]
            save_upload_control.props["form-fields"] = [
                {"name": "root_id", "value": root_id},
            ]

        def direct_save_upload_started() -> None:
            ui.notify(
                f"Upload acknowledged. Sending the save directly to {model.app_friendly}.",
                type="info",
            )
            ensure_direct_save_transfer()

        def direct_save_upload_succeeded() -> None:
            finish_direct_save_transfer(error=None)
            upload_dialog.close()
            ui.notify(f"Uploaded the save for {model.app_friendly}.", type="positive")
            ui.navigate.reload()

        def direct_save_upload_failed() -> None:
            error = f"Save upload failed before {model.app_friendly} accepted it."
            ensure_direct_save_transfer()
            finish_direct_save_transfer(error=error)
            ui.notify(
                f"{error} "
                "The node may be unavailable, out of temporary space, or may have rejected the file.",
                type="negative",
                multi_line=True,
            )

        def direct_save_upload_rejected() -> None:
            error = "The selected save was rejected before upload."
            ensure_direct_save_transfer()
            finish_direct_save_transfer(error=error)
            ui.notify(
                f"{error} Check the file type and upload limits.",
                type="warning",
            )

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
                    save_upload_control = ui.upload(
                        label="Choose Save Archive",
                        auto_upload=True,
                    ).classes("mod-list-button")
                    if show_upload_action:
                        save_upload_control.props["field-name"] = "upload"
                        save_upload_control.on("start", direct_save_upload_started, args=[])
                        save_upload_control.on("uploaded", direct_save_upload_succeeded, args=[])
                        save_upload_control.on("failed", direct_save_upload_failed, args=[])
                        save_upload_control.on("rejected", direct_save_upload_rejected, args=[])
                        refresh_direct_save_upload_target()
                        direct_save_upload_token_timer: Timer = ui.timer(
                            _DIRECT_UPLOAD_TOKEN_REFRESH_SECONDS,
                            refresh_direct_save_upload_target,
                        )
                        self._register_timer_cleanup(ui=ui, timer=direct_save_upload_token_timer)
                        self._register_client_cleanup(ui=ui, cleanup=interrupt_direct_save_transfer)
                        if root_select is not None:
                            root_select.on("update:model-value", lambda: refresh_direct_save_upload_target())
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
                    nonlocal save_page_number
                    filtered_saves: tuple[NodeSaveEntry, ...] = self._filter_save_entries(
                        saves=saves.saves,
                        options=save_options,
                        search_query=search_query,
                    )
                    filtered_saves = self._sort_file_entries(filtered_saves, current_sort_order)
                    if not filtered_saves:
                        with ui.card().classes("mod-setting-card locked w-full"):
                            ui.label("No saves match that search.").classes("mod-subtitle text-sm")
                        return
                    page_count: int = max(
                        1,
                        (len(filtered_saves) + _SERVER_RENDERED_LIST_PAGE_SIZE - 1)
                        // _SERVER_RENDERED_LIST_PAGE_SIZE,
                    )
                    save_page_number = min(save_page_number, page_count)
                    page_start: int = (save_page_number - 1) * _SERVER_RENDERED_LIST_PAGE_SIZE
                    visible_saves: tuple[NodeSaveEntry, ...] = filtered_saves[
                        page_start : page_start + _SERVER_RENDERED_LIST_PAGE_SIZE
                    ]
                    with ui.element("div").classes("mod-save-grid w-full"):
                        for save in visible_saves:
                            self._render_save_tile(
                                ui=ui,
                                model=model,
                                user=user,
                                save=save,
                                root_count=len(saves.roots),
                                can_write=can_write,
                            )
                    if page_count > 1:
                        def select_save_page(event: ModWebValueContainer) -> None:
                            nonlocal save_page_number
                            save_page_number = int(_value_as_text(event))
                            _save_tile_grid.refresh(current_search_query)

                        ui.pagination(
                            1,
                            page_count,
                            value=save_page_number,
                            direction_links=True,
                            on_change=select_save_page,
                        ).classes("mod-list-pagination")

                if show_search or show_sort or show_upload_action:
                    with ui.row().classes("mod-tab-toolbar mod-tab-toolbar-surface w-full"):
                        if show_search:
                            search_input: Input = (
                                ui.input(placeholder="Search saves", value=current_search_query)
                                .props("filled square dense clearable hide-bottom-space color=accent")
                                .classes("mod-config-search mod-settings-search")
                            )

                            def _submit_save_search() -> None:
                                nonlocal current_search_query, save_page_number
                                current_search_query = _value_as_text(search_input)
                                save_page_number = 1
                                self._replace_browser_search_query(ui=ui, search_query=current_search_query)
                                _save_tile_grid.refresh(current_search_query)

                            search_input.on("keydown.enter", _submit_save_search)
                        if show_sort:

                            def _sort_save_tiles(event: ModWebValueContainer) -> None:
                                nonlocal current_sort_order, save_page_number
                                current_sort_order = ModWebFileSortOrder(_value_as_text(event))
                                save_page_number = 1
                                _save_tile_grid.refresh(current_search_query)

                            self._render_file_sort_select(
                                ui=ui,
                                default_order=ModWebFileSortOrder.LATEST_MODIFIED,
                                on_sort=_sort_save_tiles,
                            )
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
                _save_tile_grid(current_search_query)

    def _render_blueprints_editor(self, *, ui: ModWebUi, model: ModWebBasePageModel, user: ModWebUser) -> None:
        blueprints: NodeBlueprintList | None = model.blueprints
        if blueprints is None:
            self._render_flat_tab_empty_state(
                ui=ui,
                title="Blueprints",
                description="Blueprint management is only available to signed-in users with app access.",
                detail_text="Open this app with a user-level session to browse and manage Satisfactory blueprint files.",
            )
            return

        blueprint_options: tuple[ModWebSearchOption, ...] = self._blueprint_options(blueprints.blueprints)
        show_search: bool = len(blueprint_options) > 1
        show_sort: bool = len(blueprint_options) > 1
        target_session_name: str | None = blueprints.default_session_name
        can_upload_blueprints: bool = target_session_name is not None
        current_search_query: str = model.search_query
        current_sort_order: ModWebFileSortOrder = ModWebFileSortOrder.NAME_ASCENDING
        blueprint_page_number = 1

        async def upload_blueprints(event: "MultiUploadEventArguments") -> None:
            if target_session_name is None:
                ui.notify(
                    "Blueprint upload is unavailable because the current Satisfactory session could not be determined.",
                    type="warning",
                )
                return
            try:
                result: NodeBlueprintMutationResult = await self._upload_blueprints(
                    model=model,
                    session_name=target_session_name,
                    upload_files=tuple(event.files),
                    user=user,
                )
            except Exception as xcp:
                ui.notify(f"Blueprint upload failed: {xcp}", type="negative")
                return
            upload_dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        with ui.dialog() as upload_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Upload Blueprint").classes("text-xl font-black mod-title-small")
                        ui.label(
                            "Upload one `.sbp` blueprint file and an optional matching `.sbpcfg` config file."
                        ).classes("mod-subtitle text-sm")
                    if target_session_name is None:
                        ui.label(
                            "The current Satisfactory session is unavailable, so blueprint uploads are temporarily disabled."
                        ).classes("mod-subtitle text-sm")
                    else:
                        ui.label(f"Target session: {target_session_name}").classes("mod-subtitle text-sm")
                        ui.upload(
                            label="Choose Blueprint File(s)",
                            auto_upload=True,
                            multiple=True,
                            max_files=2,
                            on_multi_upload=upload_blueprints,
                        ).props("accept=.sbp,.sbpcfg").classes("mod-list-button")
                    ui.label(
                        "Config files are optional, but they must be uploaded alongside a matching `.sbp` file."
                    ).classes(
                        "mod-subtitle text-sm"
                    )
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Close", on_click=upload_dialog.close).classes("mod-list-button secondary")

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Blueprints",
                    description=self._blueprint_card_description(blueprint_count=len(blueprints.blueprints)),
                )

                @ui.refreshable
                def _blueprint_tile_grid(search_query: str) -> None:
                    nonlocal blueprint_page_number
                    filtered_blueprints: tuple[NodeBlueprintEntry, ...] = self._filter_blueprint_entries(
                        blueprints=blueprints.blueprints,
                        options=blueprint_options,
                        search_query=search_query,
                    )
                    filtered_blueprints = self._sort_file_entries(filtered_blueprints, current_sort_order)
                    if not filtered_blueprints:
                        with ui.card().classes("mod-setting-card locked w-full"):
                            ui.label("No blueprint files match that search.").classes("mod-subtitle text-sm")
                        return
                    page_count: int = max(
                        1,
                        (len(filtered_blueprints) + _SERVER_RENDERED_LIST_PAGE_SIZE - 1)
                        // _SERVER_RENDERED_LIST_PAGE_SIZE,
                    )
                    blueprint_page_number = min(blueprint_page_number, page_count)
                    page_start: int = (blueprint_page_number - 1) * _SERVER_RENDERED_LIST_PAGE_SIZE
                    visible_blueprints: tuple[NodeBlueprintEntry, ...] = filtered_blueprints[
                        page_start : page_start + _SERVER_RENDERED_LIST_PAGE_SIZE
                    ]
                    with ui.element("div").classes("mod-save-grid w-full"):
                        for blueprint in visible_blueprints:
                            self._render_blueprint_tile(ui=ui, model=model, user=user, blueprint=blueprint)
                    if page_count > 1:
                        def select_blueprint_page(event: ModWebValueContainer) -> None:
                            nonlocal blueprint_page_number
                            blueprint_page_number = int(_value_as_text(event))
                            _blueprint_tile_grid.refresh(current_search_query)

                        ui.pagination(
                            1,
                            page_count,
                            value=blueprint_page_number,
                            direction_links=True,
                            on_change=select_blueprint_page,
                        ).classes("mod-list-pagination")

                with ui.row().classes("mod-tab-toolbar mod-tab-toolbar-surface w-full"):
                    if show_search:
                        search_input: Input = (
                            ui.input(placeholder="Search blueprints", value=current_search_query)
                            .props("filled square dense clearable hide-bottom-space color=accent")
                            .classes("mod-config-search mod-settings-search")
                        )

                        def _submit_blueprint_search() -> None:
                            nonlocal blueprint_page_number, current_search_query
                            current_search_query = _value_as_text(search_input)
                            blueprint_page_number = 1
                            self._replace_browser_search_query(ui=ui, search_query=current_search_query)
                            _blueprint_tile_grid.refresh(current_search_query)

                        search_input.on("keydown.enter", _submit_blueprint_search)
                    if show_sort:

                        def _sort_blueprint_tiles(event: ModWebValueContainer) -> None:
                            nonlocal blueprint_page_number, current_sort_order
                            current_sort_order = ModWebFileSortOrder(_value_as_text(event))
                            blueprint_page_number = 1
                            _blueprint_tile_grid.refresh(current_search_query)

                        self._render_file_sort_select(
                            ui=ui,
                            default_order=ModWebFileSortOrder.NAME_ASCENDING,
                            on_sort=_sort_blueprint_tiles,
                        )
                    with ui.row().classes("mod-tab-toolbar-actions"):
                        upload_button = ui.button("Upload Blueprint", on_click=upload_dialog.open).classes(
                            "mod-list-button"
                        )
                        if not can_upload_blueprints:
                            upload_button.props("disable")

                if not blueprints.blueprints:
                    ui.label("No blueprint files are currently available for this app.").classes(
                        "mod-subtitle text-sm mod-tab-empty-detail"
                    )
                    if can_upload_blueprints:
                        ui.label("Upload a `.sbp` blueprint and optional matching `.sbpcfg` config to seed this app.").classes(
                            "mod-subtitle text-sm mod-tab-empty-detail"
                        )
                    else:
                        ui.label("Blueprint uploads will unlock here once the current session name is available.").classes(
                            "mod-subtitle text-sm mod-tab-empty-detail"
                        )
                    return
                _blueprint_tile_grid(current_search_query)

    def _render_blueprint_tile(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        blueprint: NodeBlueprintEntry,
    ) -> None:
        delete_dialog: Dialog | None = None
        config_dialog: Dialog | None = None
        config_delete_dialog: Dialog | None = None
        config_file: NodeBlueprintFileEntry | None = blueprint.config_file

        async def delete_selected(*, blueprint_id: str, dialogs_to_close: tuple[Dialog | None, ...]) -> None:
            try:
                result: NodeBlueprintMutationResult = await self._delete_blueprint(
                    model=model,
                    blueprint_id=blueprint_id,
                    user=user,
                )
            except Exception as xcp:
                ui.notify(f"Blueprint delete failed: {xcp}", type="negative")
                return
            for dialog in dialogs_to_close:
                if dialog is not None:
                    dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        async def delete_blueprint_selected() -> None:
            await delete_selected(blueprint_id=blueprint.id, dialogs_to_close=(delete_dialog,))

        async def delete_config_selected() -> None:
            if config_file is None:
                raise ValueError("Blueprint config is not available.")
            await delete_selected(
                blueprint_id=config_file.id,
                dialogs_to_close=(config_delete_dialog, config_dialog),
            )

        def open_delete_dialog() -> None:
            nonlocal delete_dialog
            if delete_dialog is None:
                with ui.dialog() as created_dialog:
                    delete_dialog = created_dialog
                    with ui.card().classes("mod-card mod-dialog-card"):
                        with ui.column().classes("w-full gap-4 p-5"):
                            with ui.column().classes("gap-1"):
                                ui.label("Delete Blueprint").classes("text-xl font-black mod-title-small")
                                detail = (
                                    f"Delete {blueprint.relative_path} from {model.app_friendly}?"
                                    if config_file is None
                                    else (
                                        f"Delete {blueprint.relative_path} and its matching config "
                                        f"from {model.app_friendly}?"
                                    )
                                )
                                ui.label(detail).classes("mod-subtitle text-sm")
                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("Cancel", on_click=created_dialog.close).classes(
                                    "mod-list-button secondary"
                                )
                                ui.button("Delete", on_click=delete_blueprint_selected).classes(
                                    "mod-list-button danger"
                                )
            delete_dialog.open()

        def open_config_delete_dialog() -> None:
            nonlocal config_delete_dialog
            if config_file is None:
                raise ValueError("Blueprint config is not available.")
            if config_delete_dialog is None:
                with ui.dialog() as created_dialog:
                    config_delete_dialog = created_dialog
                    with ui.card().classes("mod-card mod-dialog-card"):
                        with ui.column().classes("w-full gap-4 p-5"):
                            with ui.column().classes("gap-1"):
                                ui.label("Delete Blueprint Config").classes(
                                    "text-xl font-black mod-title-small"
                                )
                                ui.label(
                                    f"Delete {config_file.relative_path} from {model.app_friendly}?"
                                ).classes("mod-subtitle text-sm")
                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("Cancel", on_click=created_dialog.close).classes(
                                    "mod-list-button secondary"
                                )
                                ui.button("Delete", on_click=delete_config_selected).classes(
                                    "mod-list-button danger"
                                )
            config_delete_dialog.open()

        def open_config_dialog() -> None:
            nonlocal config_dialog
            if config_file is None:
                raise ValueError("Blueprint config is not available.")
            if config_dialog is None:
                with ui.dialog() as created_dialog:
                    config_dialog = created_dialog
                    with ui.card().classes("mod-card mod-dialog-card"):
                        with ui.column().classes("w-full gap-4 p-5"):
                            with ui.column().classes("gap-1 w-full"):
                                ui.label(self._normalise_blueprint_title(config_file.label)).classes(
                                    "text-xl font-black mod-title-small break-all"
                                )
                                ui.label(config_file.relative_path).classes(
                                    "mod-subtitle text-sm break-all mod-save-card-path"
                                )
                            with ui.row().classes("gap-2 flex-wrap"):
                                self._badge(ui=ui, text=blueprint.session_name, tone="grey")
                                self._badge(ui=ui, text=f"Modified {config_file.modified_at}", tone="purple")
                                if config_file.uploaded_by_display_name is not None:
                                    self._badge(
                                        ui=ui,
                                        text=f"By {config_file.uploaded_by_display_name}",
                                        tone="grey",
                                    )
                                else:
                                    self._badge(ui=ui, text="Owner unknown", tone="grey")
                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("Close", on_click=created_dialog.close).classes(
                                    "mod-list-button secondary"
                                )
                                if config_file.can_delete:
                                    ui.button("Delete Config", on_click=open_config_delete_dialog).classes(
                                        "mod-list-button danger"
                                    )
            config_dialog.open()

        with ui.card().classes("mod-save-card"):
            with ui.column().classes("w-full gap-3 p-4"):
                with ui.column().classes("gap-1 w-full"):
                    ui.label(self._blueprint_card_title(blueprint)).classes("text-base font-black mod-title-small break-all")
                    ui.label(blueprint.relative_path).classes("mod-subtitle text-sm break-all mod-save-card-path")
                with ui.row().classes("gap-2 flex-wrap"):
                    self._badge(ui=ui, text=blueprint.session_name, tone="grey")
                    self._badge(ui=ui, text=f"Modified {blueprint.modified_at}", tone="purple")
                    if blueprint.uploaded_by_display_name is not None:
                        self._badge(ui=ui, text=f"By {blueprint.uploaded_by_display_name}", tone="grey")
                    else:
                        self._badge(ui=ui, text="Owner unknown", tone="grey")
                    if config_file is not None:
                        config_badge = self._badge(
                            ui=ui,
                            text="Config",
                            tone="grey",
                            extra_classes="cursor-pointer",
                        )
                        config_badge.on("click", open_config_dialog)
                if blueprint.can_delete:
                    with ui.row().classes("mod-save-card-actions mod-save-card-actions-single"):
                        ui.button("Delete", on_click=open_delete_dialog).classes(
                            "mod-list-button danger mod-save-card-button"
                        )

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
        delete_dialog: Dialog | None = None

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

        async def delete_selected() -> None:
            try:
                result: NodeSaveMutationResult = await self._delete_save(
                    model=model,
                    save_id=save.id,
                    user=user,
                )
            except Exception as xcp:
                ui.notify(f"Save delete failed: {xcp}", type="negative")
                return
            if delete_dialog is not None:
                delete_dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        def open_rename_dialog() -> None:
            nonlocal rename_dialog, rename_input
            if rename_dialog is None:
                with ui.dialog() as created_dialog:
                    rename_dialog = created_dialog
                    with ui.card().classes("mod-card mod-dialog-card"):
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
                                ui.button("Cancel", on_click=created_dialog.close).classes(
                                    "mod-list-button secondary"
                                )
                                ui.button("Rename", on_click=rename_selected).classes("mod-list-button")
            rename_dialog.open()

        def open_delete_dialog() -> None:
            nonlocal delete_dialog
            if delete_dialog is None:
                with ui.dialog() as created_dialog:
                    delete_dialog = created_dialog
                    with ui.card().classes("mod-card mod-dialog-card"):
                        with ui.column().classes("w-full gap-4 p-5"):
                            with ui.column().classes("gap-1"):
                                ui.label("Delete Save").classes("text-xl font-black mod-title-small")
                                ui.label(f"Delete {save.label} from {model.app_friendly}?").classes(
                                    "mod-subtitle text-sm"
                                )
                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("Cancel", on_click=created_dialog.close).classes(
                                    "mod-list-button secondary"
                                )
                                ui.button("Delete", on_click=delete_selected).classes("mod-list-button danger")
            delete_dialog.open()

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
                action_count = 1 + int(model.supports_save_rename) + int(save.can_delete)
                action_classes = (
                    "mod-save-card-actions mod-save-card-actions-split"
                    if action_count > 1
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
                        rename_button = ui.button("Rename", on_click=open_rename_dialog).classes(
                            "mod-list-button secondary mod-save-card-button"
                        )
                        if not can_write:
                            rename_button.disable()
                    if save.can_delete:
                        delete_button = ui.button("Delete", on_click=open_delete_dialog).classes(
                            "mod-list-button danger mod-save-card-button"
                        )
                        if not can_write:
                            delete_button.disable()

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
        search_query_text: str = model.search_query
        setting_page_number = 1
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
            previous_value: bool | str = self._setting_effective_control_value(
                setting=setting, draft_values=draft_values
            )
            self._set_setting_draft_value(
                setting=setting,
                value=value,
                draft_values=draft_values,
                force_draft=force_draft,
            )
            if self._apply_linked_setting_drafts(
                settings=settings.settings,
                setting=setting,
                previous_value=previous_value,
                next_value=value,
                draft_values=draft_values,
            ):
                _setting_card_list.refresh(search_query_text)
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
            nonlocal setting_page_number
            filtered_settings: tuple[NodeSettingEntry, ...] = self._filter_setting_entries(
                settings=settings.settings,
                options=setting_options,
                search_query=search_query,
            )
            if not filtered_settings:
                with ui.card().classes("mod-setting-card locked w-full"):
                    ui.label("No settings match that search.").classes("mod-subtitle text-sm")
                return

            page_count: int = max(
                1,
                (len(filtered_settings) + _SERVER_RENDERED_LIST_PAGE_SIZE - 1)
                // _SERVER_RENDERED_LIST_PAGE_SIZE,
            )
            setting_page_number = min(setting_page_number, page_count)
            page_start: int = (setting_page_number - 1) * _SERVER_RENDERED_LIST_PAGE_SIZE
            visible_settings: tuple[NodeSettingEntry, ...] = filtered_settings[
                page_start : page_start + _SERVER_RENDERED_LIST_PAGE_SIZE
            ]

            with ui.column().classes("mod-settings-grid w-full"):
                for setting in visible_settings:
                    self._render_setting_card(
                        ui=ui,
                        setting=setting,
                        draft_value=draft_values.get(setting.key, self._setting_current_control_value(setting)),
                        draft_values=draft_values,
                        set_draft_value=set_draft_value,
                        set_setting_validity=set_setting_validity,
                    )
            if page_count > 1:
                def select_setting_page(event: ModWebValueContainer) -> None:
                    nonlocal setting_page_number
                    setting_page_number = int(_value_as_text(event))
                    _setting_card_list.refresh(search_query_text)

                ui.pagination(
                    1,
                    page_count,
                    value=setting_page_number,
                    direction_links=True,
                    on_change=select_setting_page,
                ).classes("mod-list-pagination")

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Settings",
                    description=self._settings_card_description(),
                )
                with ui.row().classes("mod-tab-toolbar mod-tab-toolbar-surface mod-inline-toolbar w-full"):
                    search_input = (
                        ui.input(placeholder="Search settings", value=search_query_text)
                        .props("filled square dense clearable hide-bottom-space color=accent")
                        .classes("mod-config-search mod-settings-search")
                    )

                    def _submit_setting_search() -> None:
                        nonlocal search_query_text, setting_page_number
                        search_query_text = _value_as_text(search_input)
                        setting_page_number = 1
                        self._replace_browser_search_query(ui=ui, search_query=search_query_text)
                        _setting_card_list.refresh(search_query_text)

                    search_input.on("keydown.enter", _submit_setting_search)
                    with ui.row().classes("mod-tab-toolbar-actions mod-inline-toolbar-actions"):
                        reload_button = ui.button("Reload", on_click=reload_settings).classes(
                            "mod-list-button secondary"
                        )
                        save_button = ui.button("Save", on_click=save_settings).classes("mod-list-button")
                        save_button.disable()
                _setting_card_list(search_query_text)
                refresh_save_button()

    def _render_setting_card(
        self,
        *,
        ui: ModWebUi,
        setting: NodeSettingEntry,
        draft_value: bool | str,
        draft_values: Mapping[str, bool | str],
        set_draft_value: Callable[[NodeSettingEntry, bool | str, bool], None],
        set_setting_validity: Callable[[NodeSettingEntry, bool], None],
    ) -> None:
        control_kind: ModWebSettingControlKind = self._setting_control_kind(setting)
        choice_select: Select | None = None
        value_input: Input | Textarea | None = None
        invalid_feedback: Label | None = None

        card_classes = "mod-setting-card w-full"
        if not setting.can_edit:
            card_classes: LiteralString = f"{card_classes} locked"

        control_classes = "mod-setting-control gap-1"
        control_surface_classes = self._setting_control_surface_classes(can_edit=setting.can_edit)
        if setting.paragraph:
            control_classes = f"{control_classes} mod-setting-control-paragraph"
            control_surface_classes = f"{control_surface_classes} mod-setting-control-surface-paragraph"

        with ui.card().classes(card_classes):
            with ui.row().classes("mod-setting-shell w-full"):
                with ui.column().classes(control_classes):
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

                        with ui.column().classes(control_surface_classes):
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

                        def sync_input_value(
                            force_draft: bool = False,
                            event: ModWebValueContainer | None = None,
                        ) -> None:
                            if value_input is None:
                                raise ValueError("Text setting input control is not available.")
                            next_value: str = self._setting_text_input_value(
                                input_control=value_input,
                                event=event,
                            )
                            set_draft_value(setting, next_value, force_draft)
                            validation_message: str | None = self._setting_text_draft_validation_message(
                                setting=setting,
                                value=next_value,
                                draft_values=draft_values,
                            )
                            set_setting_validity(setting, validation_message is None)
                            self._update_setting_text_input_feedback(
                                input_control=value_input,
                                feedback_label=invalid_feedback,
                                message=validation_message,
                            )

                        def _sync_hidden_input(_event: object | None = None) -> None:
                            sync_input_value(setting.value_is_hidden)

                        with ui.column().classes(control_surface_classes):
                            if setting.paragraph:
                                value_input = (
                                    ui.textarea(
                                        value=draft_value,
                                        placeholder=f"Enter {setting.label}" if setting.can_edit else "Restricted",
                                        on_change=(_sync_hidden_input if setting.can_edit else None),
                                    )
                                    .props(self._setting_paragraph_props(setting))
                                    .classes("mod-setting-field mod-setting-field-primary mod-setting-field-paragraph")
                                )
                            else:
                                value_input = (
                                    ui.input(
                                        value=draft_value,
                                        placeholder=f"Enter {setting.label}" if setting.can_edit else "Restricted",
                                        on_change=(_sync_hidden_input if setting.can_edit else None),
                                    )
                                    .props(self._setting_text_input_props(setting))
                                    .classes("mod-setting-field mod-setting-field-primary")
                                )
                            value_input_control: Input | Textarea = value_input
                            if not setting.can_edit:
                                value_input_control.disable()
                                set_setting_validity(setting, True)
                            else:
                                value_input_control.on(
                                    "update:model-value",
                                    _sync_hidden_input,
                                )
                            if self._should_initialise_text_setting_draft(setting):
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
                user=user,
                model=model,
                url=self._config_root_download_url(model=model, root_id=root_id, user=user),
                message=f"Preparing download for config root {root_entries[0].root_label} from {model.app_friendly}.",
                filenames=(f"{root_entries[0].root_label}.zip",),
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
        return (
            f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{quote(model.node_name, safe='')}"
            f"/apps/{quote(model.app_name, safe='')}/mods/download"
        )

    def _save_download_url(self, *, model: ModWebBasePageModel, save: NodeSaveEntry, user: ModWebUser) -> str:
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
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_config_content_async(node, model.app_name, config_id, user)

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
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_config_write_async(node, model.app_name, config_id, content, user)

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
            user=user,
            model=model,
            url=self._save_download_url(model=model, save=save, user=user),
            message=f"Preparing download for save {save.label} from {model.app_friendly}.",
            filenames=(save.label,),
        )

    async def _persist_uploaded_file_for_transfer(
        self,
        *,
        upload_file: "FileUpload",
        transfer_id: int,
        active_detail_text: str,
        max_progress_percent: float = _UPLOAD_RECEIVE_PROGRESS_PERCENT,
    ) -> Path:
        suffix: str = Path(upload_file.name).suffix
        with tempfile.NamedTemporaryFile(prefix="yukibot-save-web-", suffix=suffix, delete=False) as handle:
            temp_path: Path = Path(handle.name)
        total_size_bytes: int = max(upload_file.size(), 1)
        written_bytes: int = 0
        try:
            with temp_path.open("wb") as output_handle:
                async for chunk in upload_file.iterate(chunk_size=_UPLOAD_PROGRESS_CHUNK_BYTES):
                    output_handle.write(chunk)
                    written_bytes += len(chunk)
                    progress_percent: float = min(
                        max_progress_percent,
                        (written_bytes / total_size_bytes) * max_progress_percent,
                    )
                    self._backend.update_transfer_progress(
                        transfer_id=transfer_id,
                        progress_percent=progress_percent,
                        detail_text=active_detail_text,
                    )
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path

    def _mark_transfers_applying(
        self,
        *,
        transfer_ids: tuple[int, ...],
        detail_text: str,
        progress_percent: float = _UPLOAD_APPLY_PROGRESS_PERCENT,
    ) -> None:
        for transfer_id in transfer_ids:
            self._backend.update_transfer_progress(
                transfer_id=transfer_id,
                progress_percent=progress_percent,
                detail_text=detail_text,
            )

    async def _wait_for_upload_transfer_capacity(
        self,
        *,
        user_id: int,
        requested_slots: int,
    ) -> int:
        transfer_limit: int = self._backend.transfer_limit()
        if requested_slots <= 0:
            raise ValueError("requested_slots must be positive.")
        allowed_slots: int = min(requested_slots, transfer_limit)
        while True:
            active_slots: int = self._backend.user_active_transfer_slots(user_id=user_id)
            available_slots: int = max(0, transfer_limit - active_slots)
            if available_slots > 0:
                return min(allowed_slots, available_slots)
            await asyncio.sleep(_TRANSFER_CAPACITY_WAIT_SECONDS)

    async def _upload_mod_batch(
        self,
        *,
        model: ModWebPageModel,
        upload_files: tuple["FileUpload", ...],
        user: ModWebUser,
    ) -> NodeModUploadBatchResult:
        transfer_ids = self._backend.start_upload_transfers(
            user_id=user.discord_id,
            filenames=tuple(upload_file.name for upload_file in upload_files),
            detail_text=f"Staging mods for {model.app_friendly}.",
            node_color_hex=self._node_role_color_hex(node_name=model.node_name),
            app_color_hex=model.app_color_hex,
        )
        resolved_uploads: list[tuple[str, Path]] = []
        try:
            for upload_file, transfer_id in zip(upload_files, transfer_ids, strict=True):
                resolved_uploads.append(
                    (
                        upload_file.name,
                        await self._persist_uploaded_file_for_transfer(
                            upload_file=upload_file,
                            transfer_id=transfer_id,
                            active_detail_text=f"Receiving mods for {model.app_friendly}.",
                        ),
                    )
                )
            self._mark_transfers_applying(
                transfer_ids=transfer_ids,
                detail_text=f"Installing mods for {model.app_friendly}.",
            )
            node = self._remote_node_link(model.node_name)
            result = await asyncio.to_thread(
                self._remote_mod_uploads,
                node,
                model.app_name,
                tuple(resolved_uploads),
                user,
            )
        except Exception as xcp:
            for transfer_id in transfer_ids:
                self._backend.fail_transfer(transfer_id=transfer_id, detail_text=f"Mod upload failed: {xcp}")
            raise
        else:
            for transfer_id in transfer_ids:
                self._backend.complete_transfer(transfer_id=transfer_id, detail_text=f"Installed for {model.app_friendly}.")
            return result
        finally:
            for _, temp_path in resolved_uploads:
                temp_path.unlink(missing_ok=True)

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
        transfer_ids = self._backend.start_upload_transfers(
            user_id=user.discord_id,
            filenames=(upload_file.name,),
            detail_text=f"Staging save for {model.app_friendly}.",
            node_color_hex=self._node_role_color_hex(node_name=model.node_name),
            app_color_hex=model.app_color_hex,
        )
        temp_path: Path = await self._persist_uploaded_file_for_transfer(
            upload_file=upload_file,
            transfer_id=transfer_ids[0],
            active_detail_text=f"Receiving save for {model.app_friendly}.",
        )
        try:
            self._mark_transfers_applying(
                transfer_ids=transfer_ids,
                detail_text=f"Applying save to {model.app_friendly}.",
            )
            node = self._remote_node_link(model.node_name)
            result = await asyncio.to_thread(
                self._remote_save_upload,
                node,
                model.app_name,
                root_id,
                temp_path,
                upload_file.name,
                user,
            )
        except Exception as xcp:
            for transfer_id in transfer_ids:
                self._backend.fail_transfer(transfer_id=transfer_id, detail_text=f"Save upload failed: {xcp}")
            raise
        else:
            for transfer_id in transfer_ids:
                self._backend.complete_transfer(transfer_id=transfer_id, detail_text=f"Saved to {model.app_friendly}.")
            return result
        finally:
            temp_path.unlink(missing_ok=True)

    async def _upload_blueprints(
        self,
        *,
        model: ModWebBasePageModel,
        session_name: str,
        upload_files: tuple["FileUpload", ...],
        user: ModWebUser,
    ) -> NodeBlueprintMutationResult:
        if not self._user_has_level(user, Power_Level.user):
            raise PermissionError(f"User access is required to upload blueprints for {model.app_friendly}.")
        transfer_ids = self._backend.start_upload_transfers(
            user_id=user.discord_id,
            filenames=tuple(upload_file.name for upload_file in upload_files),
            detail_text=f"Staging blueprint files for {model.app_friendly}.",
            node_color_hex=self._node_role_color_hex(node_name=model.node_name),
            app_color_hex=model.app_color_hex,
        )
        upload_pair: BlueprintUploadPair = classify_blueprint_upload_filenames(
            [upload_file.name for upload_file in upload_files]
        )
        temp_paths: dict[str, Path] = {}
        try:
            for upload_file, transfer_id in zip(upload_files, transfer_ids, strict=True):
                temp_paths[upload_file.name] = await self._persist_uploaded_file_for_transfer(
                    upload_file=upload_file,
                    transfer_id=transfer_id,
                    active_detail_text=f"Receiving blueprint files for {model.app_friendly}.",
                )
            self._mark_transfers_applying(
                transfer_ids=transfer_ids,
                detail_text=f"Applying blueprint files to {model.app_friendly}.",
            )
            node = self._remote_node_link(model.node_name)
            result = await asyncio.to_thread(
                self._remote_blueprint_upload,
                node,
                model.app_name,
                session_name,
                tuple[tuple[str, Path], ...](
                    (filename, temp_paths[filename])
                    for filename in (
                        (upload_pair.module_filename,)
                        if upload_pair.config_filename is None
                        else (upload_pair.module_filename, upload_pair.config_filename)
                    )
                ),
                user,
            )
        except Exception as xcp:
            for transfer_id in transfer_ids:
                self._backend.fail_transfer(transfer_id=transfer_id, detail_text=f"Blueprint upload failed: {xcp}")
            raise
        else:
            for transfer_id in transfer_ids:
                self._backend.complete_transfer(
                    transfer_id=transfer_id,
                    detail_text=f"Uploaded to {model.app_friendly}.",
                )
            return result
        finally:
            for temp_path in temp_paths.values():
                temp_path.unlink(missing_ok=True)

    async def _delete_blueprint(
        self,
        *,
        model: ModWebBasePageModel,
        blueprint_id: str,
        user: ModWebUser,
    ) -> NodeBlueprintMutationResult:
        if not self._user_has_level(user, Power_Level.user):
            raise PermissionError(f"User access is required to delete blueprints for {model.app_friendly}.")
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_blueprint_delete_async(node, model.app_name, blueprint_id, user)

    async def _upload_mods(
        self,
        *,
        model: ModWebPageModel,
        upload_files: tuple["FileUpload", ...],
        user: ModWebUser,
    ) -> NodeModUploadBatchResult:
        if not self._user_has_level(user, Power_Level.user):
            raise PermissionError(f"User access is required to upload mods for {model.app_friendly}.")
        if not upload_files:
            raise ValueError("At least one mod file is required.")
        pending_uploads: list["FileUpload"] = list(upload_files)
        batch_results: list[NodeModUploadBatchResult] = []
        while pending_uploads:
            batch_size = await self._wait_for_upload_transfer_capacity(
                user_id=user.discord_id,
                requested_slots=len(pending_uploads),
            )
            current_batch: tuple["FileUpload", ...] = tuple(pending_uploads[:batch_size])
            try:
                batch_result = await self._upload_mod_batch(model=model, upload_files=current_batch, user=user)
            except RuntimeError as xcp:
                if "Transfer limit reached" in str(xcp):
                    await asyncio.sleep(_TRANSFER_CAPACITY_WAIT_SECONDS)
                    continue
                raise
            del pending_uploads[:batch_size]
            batch_results.append(batch_result)

        uploaded_mods: tuple[NodeModEntry, ...] = tuple(
            mod
            for batch_result in batch_results
            for mod in batch_result.mods
        )
        if not uploaded_mods:
            raise RuntimeError("Mod upload completed without uploaded mods.")
        message = (
            batch_results[0].message
            if len(uploaded_mods) == 1
            else f"Uploaded {len(uploaded_mods)} mods for {model.app_friendly}."
        )
        return NodeModUploadBatchResult(
            app_name=model.app_name,
            app_friendly=model.app_friendly,
            node=batch_results[-1].node,
            message=message,
            mods=uploaded_mods,
        )

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
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_save_rename_async(node, model.app_name, save_id, new_name, user)

    async def _delete_save(
        self,
        *,
        model: ModWebBasePageModel,
        save_id: str,
        user: ModWebUser,
    ) -> NodeSaveMutationResult:
        if not self._user_has_level(user, model.save_write_level):
            raise PermissionError(
                f"{model.save_write_level.name.title()} access is required to delete saves for {model.app_friendly}."
            )
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_save_delete_async(node, model.app_name, save_id, user)

    async def _write_setting_value(
        self,
        *,
        model: ModWebBasePageModel,
        setting_key: str,
        value: str,
        user: ModWebUser,
    ) -> NodeSettingMutationResult:
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_setting_write_async(node, model.app_name, setting_key, value, user)

    async def _save_settings(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> NodeSettingsActionResult:
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_settings_save_async(node, model.app_name, user)

    async def _reload_settings(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> NodeSettingsActionResult:
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await self._remote_settings_reload_async(node, model.app_name, user)

    async def _read_console_action_list(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> NodeConsoleActionList | None:
        if model.console_actions is None:
            return None
        node = self._remote_node_link(model.node_name)
        return await self._remote_console_action_list_async(node, model.app_name, user)

    async def _execute_console_action(
        self,
        *,
        model: ModWebBasePageModel,
        action_key: str,
        raw_value: str | None,
        user: ModWebUser,
    ) -> NodeConsoleActionExecutionResult:
        node = self._remote_node_link(model.node_name)
        return await self._remote_execute_console_action_async(
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
        return model.config_read_level

    @staticmethod
    def _hex_color_to_rgba(color_hex: str, *, alpha: float) -> str:
        if len(color_hex) != 7 or not color_hex.startswith("#"):
            raise ValueError(f"Expected #rrggbb color, got {color_hex!r}.")
        if alpha < 0 or alpha > 1:
            raise ValueError(f"Alpha must be between 0 and 1, got {alpha!r}.")
        try:
            red = int(color_hex[1:3], 16)
            green = int(color_hex[3:5], 16)
            blue = int(color_hex[5:7], 16)
        except ValueError as xcp:
            raise ValueError(f"Expected #rrggbb color, got {color_hex!r}.") from xcp
        return f"rgba({red}, {green}, {blue}, {alpha:.2f})"

    @staticmethod
    def _hero_card_style(color_hex: str | None) -> str:
        if color_hex is None:
            return ""
        return (
            f"--mod-hero-border: {color_hex}; "
            f"--mod-hero-border-glow: {ModWebEditorsMixin._hex_color_to_rgba(color_hex, alpha=0.18)}; "
            "--mod-hero-border-fade: var(--mod-border);"
        )

    def _primary_guild_bot_role_color_hex(self) -> str | None:
        return self._node_role_color_hex(node_name=config.MOD_WEB_SERVER.node_name)

    def _node_role_color_hex(self, *, node_name: str) -> str | None:
        bot: GatewayBot | None = self._mod_web_bot()
        if bot is not None:
            target_user_id: int | None = self._node_bot_user_id(node_name=node_name, bot=bot)
            if target_user_id is not None:
                live_color = color_int_to_hex(
                    cached_member_role_color(bot, guild_id=config.DISCORD_GUILD, user_id=target_user_id)
                )
                if live_color is not None:
                    return live_color
        snapshot = self._known_bot_snapshot_for_node(node_name=node_name)
        if snapshot is None or snapshot.features.presentation is None:
            return None
        return snapshot.features.presentation.accent_color_hex

    def _node_bot_user_id(self, *, node_name: str, bot: GatewayBot) -> int | None:
        if node_name.casefold() == config.MOD_WEB_SERVER.node_name.casefold():
            me: OwnUser | None = bot.get_me()
            return int(me.id) if me is not None else None
        snapshot = self._known_bot_snapshot_for_node(node_name=node_name)
        if snapshot is not None:
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
    def _blueprint_option_label(entry: NodeBlueprintEntry) -> str:
        return entry.relative_path

    @staticmethod
    def _blueprint_card_title(entry: NodeBlueprintEntry) -> str:
        return ModWebEditorsMixin._normalise_blueprint_title(entry.label)

    @staticmethod
    def _normalise_blueprint_title(label: str) -> str:
        for suffix in (".sbpcfg", ".sbp"):
            if label.casefold().endswith(suffix):
                return label[: -len(suffix)]
        return label

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
    def _blueprint_card_description(*, blueprint_count: int) -> str:
        if blueprint_count == 0:
            return "No blueprints are currently available. Upload one to seed this app."
        return "Browse blueprint modules, inspect optional config files, and delete entries you own."

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

    @classmethod
    def _blueprint_options(cls, blueprints: tuple[NodeBlueprintEntry, ...]) -> tuple[ModWebSearchOption, ...]:
        return tuple[ModWebSearchOption, ...](
            ModWebSearchOption(
                option_id=entry.id,
                label=cls._blueprint_option_label(entry),
                search_text=" ".join(
                    filter(
                        None,
                        (
                            entry.session_name,
                            entry.relative_path,
                            entry.label,
                            entry.uploaded_by_display_name,
                            entry.config_file.relative_path if entry.config_file is not None else None,
                            entry.config_file.label if entry.config_file is not None else None,
                            entry.config_file.uploaded_by_display_name if entry.config_file is not None else None,
                            "config" if entry.config_file is not None else None,
                        ),
                    )
                ).casefold(),
            )
            for entry in blueprints
        )

    @staticmethod
    def _mod_option_label(entry: NodeModEntry) -> str:
        return entry.friendly

    @staticmethod
    def _mod_search_text(entry: NodeModEntry) -> str:
        terms: tuple[str, ...] = tuple(
            filter(
                None,
                (
                    entry.friendly,
                    entry.name,
                    entry.mod_type.value,
                    entry.placement.value,
                    entry.placement.label,
                    "coremod" if entry.coremod else "",
                    (
                        "client only"
                        if entry.placement is ModPlacement.CLIENT_ONLY
                        else "enabled" if entry.enabled else "disabled"
                    ),
                    "downloadable" if entry.downloadable else "blocked",
                    entry.origin,
                    entry.version,
                    entry.size_text,
                    entry.download_block_label,
                    entry.download_block_reason,
                    entry.client_pack.policy.value,
                    entry.client_pack.policy.label,
                    entry.client_pack.choice_group,
                ),
            )
        )
        return " ".join(terms).casefold()

    @classmethod
    def _mod_options(cls, mods: tuple[NodeModEntry, ...]) -> tuple[ModWebSearchOption, ...]:
        return tuple[ModWebSearchOption, ...](
            ModWebSearchOption(
                option_id=entry.name,
                label=cls._mod_option_label(entry),
                search_text=cls._mod_search_text(entry),
            )
            for entry in mods
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

    @classmethod
    def _filter_blueprint_entries(
        cls,
        *,
        blueprints: tuple[NodeBlueprintEntry, ...],
        options: tuple[ModWebSearchOption, ...],
        search_query: str,
    ) -> tuple[NodeBlueprintEntry, ...]:
        matching_ids: set[str] = {
            option.option_id for option in cls._matching_search_options(options=options, search_query=search_query)
        }
        if not matching_ids and cls._search_query_tokens(search_query):
            return ()
        return tuple[NodeBlueprintEntry, ...](
            blueprint for blueprint in blueprints if blueprint.id in matching_ids or not matching_ids
        )

    @staticmethod
    def _sort_file_entries(
        entries: tuple[_SortableFileEntry, ...],
        order: ModWebFileSortOrder,
    ) -> tuple[_SortableFileEntry, ...]:
        alphabetic: list[_SortableFileEntry] = sorted(
            entries,
            key=lambda entry: (entry.label.casefold(), entry.relative_path.casefold()),
        )
        if order is ModWebFileSortOrder.NAME_ASCENDING:
            return tuple(alphabetic)
        if order is ModWebFileSortOrder.NAME_DESCENDING:
            return tuple(reversed(alphabetic))
        if order is ModWebFileSortOrder.LATEST_MODIFIED:
            return tuple(sorted(alphabetic, key=lambda entry: entry.modified_at, reverse=True))
        if order is ModWebFileSortOrder.OLDEST_MODIFIED:
            return tuple(sorted(alphabetic, key=lambda entry: entry.modified_at))
        if order is ModWebFileSortOrder.SIZE_DESCENDING:
            return tuple(sorted(alphabetic, key=lambda entry: entry.size_bytes, reverse=True))
        if order is ModWebFileSortOrder.SIZE_ASCENDING:
            return tuple(sorted(alphabetic, key=lambda entry: entry.size_bytes))
        raise ValueError(f"Unsupported file sort order: {order!r}")

    @staticmethod
    def _render_file_sort_select(
        *,
        ui: ModWebUi,
        default_order: ModWebFileSortOrder,
        on_sort: Callable[[ModWebValueContainer], None],
    ) -> Select:
        return (
            ui.select(
                {order.value: order.label for order in ModWebFileSortOrder},
                value=default_order.value,
                on_change=on_sort,
            )
            .props("filled square dense hide-bottom-space color=accent options-dark")
            .classes("mod-config-select mod-mods-toolbar-sort")
        )

    @classmethod
    def _filter_mod_entries(
        cls,
        *,
        mods: tuple[NodeModEntry, ...],
        options: tuple[ModWebSearchOption, ...],
        search_query: str,
    ) -> tuple[NodeModEntry, ...]:
        matching_ids: set[str] = {
            option.option_id for option in cls._matching_search_options(options=options, search_query=search_query)
        }
        if not matching_ids and cls._search_query_tokens(search_query):
            return ()
        return tuple[NodeModEntry, ...](mod for mod in mods if mod.name in matching_ids or not matching_ids)

    @staticmethod
    def _sort_mod_entries(
        mods: tuple[NodeModEntry, ...],
        order: ModWebModSortOrder,
    ) -> tuple[NodeModEntry, ...]:
        alphabetic: list[NodeModEntry] = sorted(
            mods,
            key=lambda entry: (entry.friendly.casefold(), entry.name.casefold()),
        )
        if order is ModWebModSortOrder.NAME_ASCENDING:
            return tuple(alphabetic)
        if order is ModWebModSortOrder.NAME_DESCENDING:
            return tuple(reversed(alphabetic))
        if order is ModWebModSortOrder.NEWEST:
            return tuple(sorted(alphabetic, key=lambda entry: entry.added_at, reverse=True))
        if order is ModWebModSortOrder.OLDEST:
            return tuple(sorted(alphabetic, key=lambda entry: entry.added_at))
        if order is ModWebModSortOrder.SIZE_DESCENDING:
            return tuple(sorted(alphabetic, key=lambda entry: entry.size_bytes, reverse=True))
        if order is ModWebModSortOrder.SIZE_ASCENDING:
            return tuple(sorted(alphabetic, key=lambda entry: entry.size_bytes))
        if order is ModWebModSortOrder.TYPE:
            type_order: dict[ModType, int] = {
                ModType.REGULAR: 0,
                ModType.CLIENT: 1,
                ModType.SERVER: 2,
                ModType.COREMOD: 3,
                ModType.BUILTIN: 4,
            }
            return tuple(sorted(alphabetic, key=lambda entry: type_order[entry.mod_type]))
        raise ValueError(f"Unsupported mod sort order: {order!r}")

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
        if app_stats.runtime_fault is not None:
            return _ModWebBadgeSpec(text="Crashed", tone="red")
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
            return f"Requires {action.power_level_label} access."
        if not action.requires_running:
            return "Ready."
        if app_stats is None:
            return "Runtime status unavailable. Refresh and try again."
        if app_stats.transition_state is NodeAppTransitionState.STARTING:
            return f"{app_friendly} is starting."
        if app_stats.transition_state is NodeAppTransitionState.STOPPING:
            return f"{app_friendly} is stopping."
        if app_stats.running:
            return "Ready."
        if not app_stats.enabled:
            return f"{app_friendly} is disabled."
        if app_stats.runtime_fault is not None:
            return f"{app_friendly} crashed. Restart it before using this action."
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
        if result.source.value == "api":
            return "purple"
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
    def _setting_paragraph_props(setting: NodeSettingEntry) -> str:
        if setting.value_is_hidden:
            return (
                "filled square hide-bottom-space color=accent "
                "type=password autocomplete=off spellcheck=false autocorrect=off autocapitalize=off "
                "rows=2 input-style=height:100%;min-height:100%;max-height:100%;resize:none"
            )
        return (
            "filled square hide-bottom-space color=accent "
            "spellcheck=false autocorrect=off autocapitalize=off "
            "rows=2 input-style=height:100%;min-height:100%;max-height:100%;resize:none"
        )

    @staticmethod
    def _should_initialise_text_setting_draft(setting: NodeSettingEntry) -> bool:
        return setting.can_edit and not setting.value_is_hidden

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
    def _register_element_cleanup(*, element: object, cleanup: Callable[[], None]) -> None:
        handle_delete_candidate: object | None = getattr(element, "_handle_delete", None)
        if not callable(handle_delete_candidate):
            return

        cleanup_attr = "_mod_web_delete_cleanups"
        wrapped_attr = "_mod_web_delete_cleanup_wrapped"
        cleanups_candidate: object | None = getattr(element, cleanup_attr, None)
        if isinstance(cleanups_candidate, list):
            cleanups = cast(list[Callable[[], None]], cleanups_candidate)
        else:
            cleanups = []
            setattr(element, cleanup_attr, cleanups)
        cleanups.append(cleanup)
        if bool(getattr(element, wrapped_attr, False)):
            return

        original_handle_delete = cast(Callable[[], None], handle_delete_candidate)

        def _handle_delete_with_cleanup() -> None:
            registered_cleanups = tuple(cast(list[Callable[[], None]], getattr(element, cleanup_attr, [])))
            setattr(element, cleanup_attr, [])
            for registered_cleanup in registered_cleanups:
                try:
                    registered_cleanup()
                except Exception as xcp:
                    log.warning("Mod web element cleanup failed: element=%s error=%s", type(element).__name__, xcp)
            original_handle_delete()

        setattr(element, "_handle_delete", _handle_delete_with_cleanup)
        setattr(element, wrapped_attr, True)

    @staticmethod
    def _register_timer_cleanup(*, ui: ModWebUi, timer: object) -> None:
        cancel = getattr(timer, "cancel", None)
        if not callable(cancel):
            return

        def _mark_timer_deleted() -> None:
            deleted_candidate: object | None = getattr(timer, "_deleted", None)
            if isinstance(deleted_candidate, bool):
                setattr(timer, "_deleted", True)

        def _cancel_timer() -> None:
            _mark_timer_deleted()
            cancel(with_current_invocation=True)

        get_context_candidate: object | None = getattr(timer, "_get_context", None)
        if callable(get_context_candidate) and not bool(getattr(timer, "_mod_web_safe_timer_context_wrapped", False)):
            original_get_context = cast(Callable[[], object], get_context_candidate)

            def _safe_get_context() -> object:
                try:
                    return original_get_context()
                except RuntimeError as xcp:
                    if str(xcp) != "The parent slot of the element has been deleted.":
                        raise
                    _cancel_timer()
                    return nullcontext()

            setattr(timer, "_get_context", _safe_get_context)
            setattr(timer, "_mod_web_safe_timer_context_wrapped", True)

        try:
            parent_slot: object | None = getattr(timer, "parent_slot", None)
        except RuntimeError as xcp:
            if str(xcp) == "The parent slot of the element has been deleted.":
                parent_slot = None
            else:
                raise
        parent_element: object | None = None
        if parent_slot is not None:
            try:
                parent_element = getattr(parent_slot, "parent", None)
            except RuntimeError as xcp:
                if str(xcp) != "The parent element this slot belongs to has been deleted.":
                    raise
        if parent_element is not None:
            ModWebEditorsMixin._register_element_cleanup(element=parent_element, cleanup=_cancel_timer)

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

    @classmethod
    def _setting_text_draft_validation_message(
        cls,
        *,
        setting: NodeSettingEntry,
        value: str,
        draft_values: Mapping[str, bool | str],
    ) -> str | None:
        if setting.key not in draft_values:
            return None
        return cls._setting_text_validation_message(setting, value)

    @staticmethod
    def _setting_text_input_value(
        *,
        input_control: ModWebValueContainer,
        event: ModWebValueContainer | None = None,
    ) -> str:
        if event is not None:
            return _value_as_text(event)
        return _value_as_text(input_control)

    @staticmethod
    def _setting_effective_control_value(
        *,
        setting: NodeSettingEntry,
        draft_values: Mapping[str, bool | str],
    ) -> bool | str:
        return draft_values.get(setting.key, ModWebEditorsMixin._setting_current_control_value(setting))

    @classmethod
    def _set_setting_draft_value(
        cls,
        *,
        setting: NodeSettingEntry,
        value: bool | str,
        draft_values: dict[str, bool | str],
        force_draft: bool = False,
    ) -> None:
        current_value: bool | str = cls._setting_current_control_value(setting)
        if not force_draft and value == current_value:
            draft_values.pop(setting.key, None)
            return
        draft_values[setting.key] = value

    @classmethod
    def _apply_linked_setting_drafts(
        cls,
        *,
        settings: tuple[NodeSettingEntry, ...],
        setting: NodeSettingEntry,
        previous_value: bool | str,
        next_value: bool | str,
        draft_values: dict[str, bool | str],
    ) -> bool:
        if (
            setting.key not in _SEVENDAYS_TRADER_BIOME_SETTING_KEYS
            or not isinstance(previous_value, str)
            or not isinstance(next_value, str)
        ):
            return False
        for other_setting in settings:
            if other_setting.key == setting.key or other_setting.key not in _SEVENDAYS_TRADER_BIOME_SETTING_KEYS:
                continue
            other_value: bool | str = cls._setting_effective_control_value(
                setting=other_setting, draft_values=draft_values
            )
            if other_value != next_value:
                continue
            cls._set_setting_draft_value(
                setting=other_setting,
                value=previous_value,
                draft_values=draft_values,
                force_draft=other_setting.value_is_hidden,
            )
            return True
        return False

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
        reveal_classes: str = "mod-setting-meta-secret-reveal"
        if setting.is_sensitive:
            reveal_classes += " mod-setting-meta-secret-reveal-token"
        return f'<span class="{reveal_classes}">{escape(setting.revealed_value_text)}</span>'

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
        input_control: Input | Textarea,
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
