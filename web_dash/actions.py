from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import (
    _DOWNLOAD_FEEDBACK_DELAY_SECONDS,
    log,
)
from .nicegui_protocols import ModWebUi
from .runtime_imports import (
    Awaitable,
    Callable,
    Checkbox,
    ManagedApp,
    ModType,
    ModWebUser,
    NodeCapacityMutationResult,
    NodeFontSourceSettingsMutationResult,
    NodeApiScope,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeModEntry,
    NodeModMutationAction,
    NodeModMutationResult,
    Power_Level,
    assert_never,
    asyncio,
    config,
    quote,
    required_app_mutation_level,
    required_app_mutation_scope,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModDownloadKind,
    ModWebBasePageModel,
    ModWebNodeLink,
    ModWebPageModel,
    _ModWebKillControlState,
    _ModWebStartStopControlState,
)

if TYPE_CHECKING:
    from nicegui.elements.dialog import Dialog
    from nicegui.events import ValueChangeEventArguments


class ModWebActionsMixin(ModWebServiceSupport):
    def _remote_app_mutation(
        self,
        node: ModWebNodeLink,
        app_name: str,
        action: NodeAppMutationAction,
        user: ModWebUser,
        friendly_name: str | None = None,
        title_font_preset: str | None = None,
        notes: str | None = None,
        lifecycle_notice_started: bool | None = None,
        lifecycle_notice_stopped: bool | None = None,
        lifecycle_notice_crashed: bool | None = None,
        running_cpu_points: int | None = None,
        running_ram_points: int | None = None,
        startup_cpu_points: int | None = None,
        startup_ram_points: int | None = None,
    ) -> NodeAppMutationResult:
        json_payload: dict[str, object] = {"action": action.value}
        if friendly_name is not None:
            json_payload["friendly_name"] = friendly_name
        if title_font_preset is not None:
            json_payload["title_font_preset"] = title_font_preset
        if notes is not None:
            json_payload["notes"] = notes
        if lifecycle_notice_started is not None:
            json_payload["lifecycle_notice_started"] = lifecycle_notice_started
        if lifecycle_notice_stopped is not None:
            json_payload["lifecycle_notice_stopped"] = lifecycle_notice_stopped
        if lifecycle_notice_crashed is not None:
            json_payload["lifecycle_notice_crashed"] = lifecycle_notice_crashed
        if action is NodeAppMutationAction.UPDATE_DETAILS:
            json_payload["running_cpu_points"] = running_cpu_points
            json_payload["running_ram_points"] = running_ram_points
            json_payload["startup_cpu_points"] = startup_cpu_points
            json_payload["startup_ram_points"] = startup_ram_points
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/mutate",
            scopes=(required_app_mutation_scope(action),),
            user=user,
            method="POST",
            json_payload=json_payload,
        )
        return NodeAppMutationResult.from_mapping(payload)

    def _remote_node_capacity(self, node: ModWebNodeLink, user: ModWebUser) -> config.NodeCapacityProfile:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=None,
            path="/node-capacity",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
        )
        return config.NodeCapacityProfile.model_validate(payload)

    def _remote_update_node_capacity(
        self,
        node: ModWebNodeLink,
        capacity: config.NodeCapacityProfile,
        user: ModWebUser,
    ) -> NodeCapacityMutationResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=None,
            path="/node-capacity",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
            method="POST",
            json_payload=capacity.model_dump(mode="json"),
        )
        return NodeCapacityMutationResult.from_mapping(payload)

    def _remote_node_font_sources(self, node: ModWebNodeLink, user: ModWebUser) -> config.NodeFontSourceSettings:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=None,
            path="/node-font-sources",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
        )
        return config.NodeFontSourceSettings.model_validate(payload)

    def _remote_update_node_font_sources(
        self,
        node: ModWebNodeLink,
        settings: config.NodeFontSourceSettings,
        user: ModWebUser,
    ) -> NodeFontSourceSettingsMutationResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=None,
            path="/node-font-sources",
            scopes=(NodeApiScope.NODE_MANAGE,),
            user=user,
            method="POST",
            json_payload=settings.model_dump(mode="json"),
        )
        return NodeFontSourceSettingsMutationResult.from_mapping(payload)

    async def _mutate_mod(
        self,
        *,
        model: ModWebPageModel,
        mod_name: str,
        action: NodeModMutationAction,
        user: ModWebUser,
    ) -> NodeModMutationResult:
        if not self._user_has_level(user, Power_Level.sudo):
            raise PermissionError("Sudo access is required for mod actions.")
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app = self._resolve_app(model.app_name)
            return await self._node_api.mutate_mod(
                app=app,
                mod_name=mod_name,
                action=action,
                actor_user_id=user.discord_id,
            )
        node = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(self._remote_mod_mutation, node, model.app_name, mod_name, action, user)

    def _remote_mod_mutation(
        self,
        node: ModWebNodeLink,
        app_name: str,
        mod_name: str,
        action: NodeModMutationAction,
        user: ModWebUser,
    ) -> NodeModMutationResult:
        payload: dict[str, object] = self._remote_json(
            node=node,
            app_name=app_name,
            path=f"/apps/{quote(app_name, safe='')}/mods/{quote(mod_name, safe='')}/mutate",
            scopes=(NodeApiScope.MODS_WRITE,),
            user=user,
            method="POST",
            json_payload={"action": action.value},
        )
        return NodeModMutationResult.from_mapping(payload)

    @staticmethod
    def _mod_action_label(action: NodeModMutationAction, entry: NodeModEntry) -> str:
        match action:
            case NodeModMutationAction.ENABLE:
                return "Enable"
            case NodeModMutationAction.DISABLE:
                return "Disable"
            case NodeModMutationAction.TOGGLE_COREMOD:
                return "Coremod"
            case NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK:
                return "Unblock" if not entry.downloadable else "Block"
            case NodeModMutationAction.DELETE:
                return "Delete"
            case _:
                assert_never(action)

    @staticmethod
    def _mod_action_button_classes(action: NodeModMutationAction, entry: NodeModEntry) -> str:
        match action:
            case NodeModMutationAction.ENABLE:
                return "mod-list-button state-disabled"
            case NodeModMutationAction.DISABLE:
                return "mod-list-button state-enabled"
            case NodeModMutationAction.TOGGLE_COREMOD:
                return "mod-list-button state-core-on" if entry.coremod else "mod-list-button state-core-off"
            case NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK:
                return "mod-list-button state-blocked" if not entry.downloadable else "mod-list-button state-open"
            case NodeModMutationAction.DELETE:
                return "mod-list-button danger"
            case _:
                assert_never(action)

    @staticmethod
    def _is_builtin_mod(entry: NodeModEntry) -> bool:
        return entry.mod_type is ModType.BUILTIN

    @staticmethod
    def _selection_toggle_label(*, selected_count: int) -> str:
        return "Clear" if selected_count > 0 else "Select All"

    @staticmethod
    def _download_selection_label(*, selected_count: int, downloadable_count: int) -> str:
        if downloadable_count <= 0:
            return "Download 0/0"
        if selected_count <= 0 or selected_count >= downloadable_count:
            current = "All"
        else:
            current: str = str(selected_count)
        return f"Download {current}/{downloadable_count}"

    async def _mutate_app(
        self,
        *,
        model: ModWebBasePageModel,
        action: NodeAppMutationAction,
        user: ModWebUser,
        friendly_name: str | None = None,
        title_font_preset: str | None = None,
        notes: str | None = None,
        lifecycle_notice_started: bool | None = None,
        lifecycle_notice_stopped: bool | None = None,
        lifecycle_notice_crashed: bool | None = None,
        running_cpu_points: int | None = None,
        running_ram_points: int | None = None,
        startup_cpu_points: int | None = None,
        startup_ram_points: int | None = None,
    ) -> NodeAppMutationResult:
        required_level: Power_Level = required_app_mutation_level(action)
        if not self._user_has_level(user, required_level):
            raise PermissionError(f"{required_level.name.title()} access is required for this app action.")
        if model.node_name == config.MOD_WEB_SERVER.node_name:
            app: ManagedApp = self._resolve_app(model.app_name)
            return await self._node_api.mutate_app(
                app=app,
                action=action,
                actor_user_id=user.discord_id,
                friendly_name=friendly_name,
                title_font_preset=title_font_preset,
                notes=notes,
                lifecycle_notice_started=lifecycle_notice_started,
                lifecycle_notice_stopped=lifecycle_notice_stopped,
                lifecycle_notice_crashed=lifecycle_notice_crashed,
                running_cpu_points=running_cpu_points,
                running_ram_points=running_ram_points,
                startup_cpu_points=startup_cpu_points,
                startup_ram_points=startup_ram_points,
            )
        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        return await asyncio.to_thread(
            self._remote_app_mutation,
            node,
            model.app_name,
            action,
            user,
            friendly_name,
            title_font_preset,
            notes,
            lifecycle_notice_started,
            lifecycle_notice_stopped,
            lifecycle_notice_crashed,
            running_cpu_points,
            running_ram_points,
            startup_cpu_points,
            startup_ram_points,
        )

    async def _node_capacity(self, *, node_name: str, user: ModWebUser) -> config.NodeCapacityProfile:
        self._require_user_level(user=user, required_level=Power_Level.root)
        if node_name == config.MOD_WEB_SERVER.node_name:
            manager = self._manager
            if manager is None:
                raise RuntimeError("App manager is not available yet.")
            return manager.node_capacity()
        node = self._remote_node_link(node_name)
        return await asyncio.to_thread(self._remote_node_capacity, node, user)

    async def _node_font_sources(self, *, node_name: str, user: ModWebUser) -> config.NodeFontSourceSettings:
        self._require_user_level(user=user, required_level=Power_Level.root)
        if node_name == config.MOD_WEB_SERVER.node_name:
            manager = self._manager
            if manager is None:
                raise RuntimeError("App manager is not available yet.")
            return manager.node_font_sources()
        node = self._remote_node_link(node_name)
        return await asyncio.to_thread(self._remote_node_font_sources, node, user)

    async def _update_node_capacity(
        self,
        *,
        node_name: str,
        user: ModWebUser,
        capacity: config.NodeCapacityProfile,
    ) -> NodeCapacityMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.root)
        if node_name == config.MOD_WEB_SERVER.node_name:
            result = await self._node_api.mutate_node_capacity(capacity=capacity, actor_user_id=user.discord_id)
            return result
        node = self._remote_node_link(node_name)
        return await asyncio.to_thread(self._remote_update_node_capacity, node, capacity, user)

    async def _update_node_font_sources(
        self,
        *,
        node_name: str,
        user: ModWebUser,
        settings: config.NodeFontSourceSettings,
    ) -> NodeFontSourceSettingsMutationResult:
        self._require_user_level(user=user, required_level=Power_Level.root)
        if node_name == config.MOD_WEB_SERVER.node_name:
            return await self._node_api.mutate_node_font_sources(settings=settings, actor_user_id=user.discord_id)
        node = self._remote_node_link(node_name)
        return await asyncio.to_thread(self._remote_update_node_font_sources, node, settings, user)

    @staticmethod
    def _app_start_stop_action(model: ModWebBasePageModel) -> NodeAppMutationAction | None:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.transition_state is not NodeAppTransitionState.NONE:
            return None
        if app_stats is not None and app_stats.running:
            return NodeAppMutationAction.STOP
        if model.app_start_blocked:
            return None
        return NodeAppMutationAction.START

    @staticmethod
    def _app_start_stop_label(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None:
            if app_stats.transition_state is NodeAppTransitionState.STARTING:
                return "Starting"
            if app_stats.transition_state is NodeAppTransitionState.STOPPING:
                return "Stopping"
        if app_stats is not None and app_stats.running:
            return "Stop"
        if model.app_start_blocked:
            return "Blocked"
        return "Start"

    @staticmethod
    def _app_start_stop_button_classes(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None:
            if app_stats.transition_state is NodeAppTransitionState.STARTING:
                return "mod-list-button state-open"
            if app_stats.transition_state is NodeAppTransitionState.STOPPING:
                return "mod-list-button danger"
        if app_stats is not None and app_stats.running:
            return "mod-list-button danger"
        if model.app_start_blocked:
            return "mod-list-button state-blocked"
        return "mod-list-button state-enabled"

    @staticmethod
    def _app_start_stop_disabled(model: ModWebBasePageModel) -> bool:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.transition_state is not NodeAppTransitionState.NONE:
            return True
        if app_stats is not None and app_stats.running:
            return False
        if model.app_start_blocked:
            return True
        return app_stats is not None and not app_stats.enabled

    @staticmethod
    def _app_kill_disabled(model: ModWebBasePageModel) -> bool:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.running:
            return False
        if app_stats is not None and app_stats.transition_state is NodeAppTransitionState.STARTING:
            return False
        return True

    @staticmethod
    def _app_action_transition_state(action: NodeAppMutationAction) -> NodeAppTransitionState:
        if action is NodeAppMutationAction.START:
            return NodeAppTransitionState.STARTING
        if action in {NodeAppMutationAction.STOP, NodeAppMutationAction.KILL}:
            return NodeAppTransitionState.STOPPING
        return NodeAppTransitionState.NONE

    @classmethod
    def _start_stop_control_state(cls, model: ModWebBasePageModel) -> _ModWebStartStopControlState:
        return _ModWebStartStopControlState(
            label=cls._app_start_stop_label(model),
            button_classes=f"{cls._app_start_stop_button_classes(model)} mod-toolbar-button",
            disabled=cls._app_start_stop_disabled(model),
            action=cls._app_start_stop_action(model),
        )

    @staticmethod
    def _kill_control_state(model: ModWebBasePageModel) -> _ModWebKillControlState:
        return _ModWebKillControlState(
            label="Kill",
            disabled=ModWebActionsMixin._app_kill_disabled(model),
        )

    @classmethod
    def _app_action_pending_label(cls, action: NodeAppMutationAction) -> str | None:
        if action is NodeAppMutationAction.START:
            return "Starting..."
        if action is NodeAppMutationAction.STOP:
            return "Stopping..."
        if action is NodeAppMutationAction.KILL:
            return "Killing..."
        return None

    @classmethod
    def _app_action_pending_message(cls, action: NodeAppMutationAction, app_friendly: str) -> str | None:
        if action is NodeAppMutationAction.START:
            return f"Start requested for {app_friendly}."
        if action is NodeAppMutationAction.STOP:
            return f"Stop requested for {app_friendly}."
        if action is NodeAppMutationAction.KILL:
            return f"Kill requested for {app_friendly}."
        return None

    @staticmethod
    def _app_enable_disable_action(model: ModWebBasePageModel) -> NodeAppMutationAction:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.enabled:
            return NodeAppMutationAction.DISABLE
        return NodeAppMutationAction.ENABLE

    @staticmethod
    def _app_enable_disable_label(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.enabled:
            return "Disable"
        return "Enable"

    @staticmethod
    def _app_enable_disable_button_classes(model: ModWebBasePageModel) -> str:
        app_stats: NodeAppRuntimeSummary | None = model.app_stats
        if app_stats is not None and app_stats.enabled:
            return "mod-list-button state-enabled"
        return "mod-list-button state-disabled"

    def _render_mod_info_dialog(
        self,
        *,
        ui: ModWebUi,
        entry: NodeModEntry,
        model: ModWebPageModel,
        user: ModWebUser,
    ) -> "Dialog":
        status_text = "Enabled" if entry.enabled else "Disabled"
        downloadable_text = "Yes" if entry.downloadable else "No"
        version_text: str = entry.version or "Unknown"
        block_text: str = entry.download_block_label or entry.download_block_reason or "None"
        can_manage: bool = self._user_has_level(user, Power_Level.sudo) and not self._is_builtin_mod(entry)

        async def run_mod_action(action: NodeModMutationAction) -> None:
            try:
                result: NodeModMutationResult = await self._mutate_mod(
                    model=model, mod_name=entry.name, action=action, user=user
                )
            except Exception as xcp:
                log.warning(
                    "Mod mutation failed: node=%s app=%s mod=%s action=%s error=%s",
                    model.node_name,
                    model.app_name,
                    entry.name,
                    action.value,
                    xcp,
                )
                ui.notify(f"Mod action failed: {xcp}", type="negative")
                return
            dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        async def confirm_delete() -> None:
            delete_confirm_dialog.close()
            await run_mod_action(NodeModMutationAction.DELETE)

        def _create_mod_action_handler(
            action: NodeModMutationAction,
        ) -> Callable[[object | None], Awaitable[None]]:
            async def _handle_mod_action(_: object | None = None) -> None:
                await run_mod_action(action)

            return _handle_mod_action

        with ui.dialog() as delete_confirm_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    ui.label("Delete mod?").classes("text-xl font-black mod-title-small")
                    ui.label(f"{entry.friendly} will be removed from the server.").classes("mod-subtitle text-sm")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=delete_confirm_dialog.close).classes("mod-list-button secondary")
                        ui.button("Delete", on_click=confirm_delete).classes("mod-list-button danger")

        with ui.dialog() as dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label(entry.friendly).classes("text-xl font-black mod-title-small")
                        ui.label(entry.name).classes("mod-subtitle text-sm break-all")
                    with ui.grid(columns=2).classes("mod-detail-grid"):
                        self._render_mod_detail_item(ui=ui, label="Status", value=status_text)
                        self._render_mod_detail_item(ui=ui, label="Size", value=entry.size_text)
                        self._render_mod_detail_item(ui=ui, label="Downloadable", value=downloadable_text)
                        self._render_mod_detail_item(ui=ui, label="Coremod", value="Yes" if entry.coremod else "No")
                        self._render_mod_detail_item(ui=ui, label="Origin", value=entry.origin)
                        self._render_mod_detail_item(ui=ui, label="Version", value=version_text)
                        self._render_mod_detail_item(ui=ui, label="Added", value=entry.added)
                        self._render_mod_detail_item(ui=ui, label="Blocked", value=block_text)
                    if can_manage:
                        with ui.column().classes("gap-2"):
                            ui.label("Privileged Actions").classes("mod-stat-label")
                            with ui.row().classes("w-full gap-2 flex-wrap"):
                                for action in (
                                    NodeModMutationAction.DISABLE if entry.enabled else NodeModMutationAction.ENABLE,
                                    NodeModMutationAction.TOGGLE_COREMOD,
                                    NodeModMutationAction.TOGGLE_DOWNLOAD_BLOCK,
                                    NodeModMutationAction.DELETE,
                                ):
                                    on_click = (
                                        delete_confirm_dialog.open
                                        if action is NodeModMutationAction.DELETE
                                        else _create_mod_action_handler(action)
                                    )
                                    ui.button(
                                        self._mod_action_label(action, entry),
                                        on_click=on_click,
                                    ).classes(self._mod_action_button_classes(action, entry))
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Close", on_click=dialog.close).classes("mod-list-button secondary")
        return dialog

    @staticmethod
    def _render_mod_detail_item(*, ui: ModWebUi, label: str, value: str) -> None:
        with ui.column().classes("mod-detail-item gap-1"):
            ui.label(label).classes("mod-stat-label")
            ui.label(value).classes("mod-stat-value break-words")

    def _render_mod_download_row(
        self,
        *,
        ui: ModWebUi,
        entry: NodeModEntry,
        download_url: str | None,
        on_change: Callable[["ValueChangeEventArguments"], None],
        app_friendly: str,
        model: ModWebPageModel,
        user: ModWebUser,
    ) -> Checkbox | None:
        row_classes = ["mod-row", "w-full"]
        if not entry.downloadable:
            row_classes.append("blocked")
        elif not entry.enabled:
            row_classes.append("mod-row-disabled")
        dialog = self._render_mod_info_dialog(ui=ui, entry=entry, model=model, user=user)
        row = ui.row().classes(" ".join((*row_classes, "mod-row-clickable")))
        row.on("click", lambda _: dialog.open())
        with row:
            if entry.downloadable:
                checkbox = ui.checkbox(value=False, on_change=on_change).props("dense")
                checkbox.on("click", js_handler="(event) => event.stopPropagation()")
            else:
                checkbox = ui.checkbox(value=False).props("dense")
                checkbox.disable()
                checkbox.on("click", js_handler="(event) => event.stopPropagation()")
            with ui.column().classes("mod-row-main gap-0"):
                ui.label(entry.friendly).classes("mod-row-title")
                ui.label(entry.name).classes("mod-row-file")
            with ui.row().classes("mod-row-meta"):
                ui.label(entry.size_text).classes("mod-pill size")
                if entry.coremod:
                    ui.label("Coremod").classes("mod-pill core")
                if not entry.downloadable:
                    ui.label(entry.download_block_label or "Not downloadable").classes("mod-pill blocked")
            if download_url is None:
                ui.label("Blocked").classes("mod-row-download blocked")
            else:

                async def download_single() -> None:
                    await self._start_download(
                        ui=ui,
                        url=download_url,
                        message=self._download_feedback_message(
                            kind=ModDownloadKind.SINGLE,
                            app_friendly=app_friendly,
                            mod_friendly=entry.friendly,
                        ),
                    )

                ui.button("Download", on_click=download_single).props("flat dense no-caps").classes(
                    "mod-row-download"
                ).on("click", js_handler="(event) => event.stopPropagation()")
        return checkbox if entry.downloadable else None

    async def _start_download(self, *, ui: ModWebUi, url: str, message: str) -> None:
        ui.notify(message, type="info")
        await asyncio.sleep(_DOWNLOAD_FEEDBACK_DELAY_SECONDS)
        ui.navigate.to(url)

    @staticmethod
    def _download_feedback_message(
        *,
        kind: ModDownloadKind,
        app_friendly: str,
        mod_friendly: str | None = None,
        selected_count: int | None = None,
    ) -> str:
        match kind:
            case ModDownloadKind.ENABLED:
                return f"Preparing enabled mod download for {app_friendly}."
            case ModDownloadKind.ALL:
                return f"Preparing full mod download for {app_friendly}."
            case ModDownloadKind.SELECTED:
                if selected_count is None or selected_count < 1:
                    raise ValueError("Selected downloads require a positive selected_count.")
                mod_label = "mod" if selected_count == 1 else "mods"
                return f"Preparing download for {selected_count} selected {mod_label} from {app_friendly}."
            case ModDownloadKind.SINGLE:
                if mod_friendly is None or not mod_friendly.strip():
                    raise ValueError("Single downloads require a mod_friendly value.")
                return f"Preparing download for {mod_friendly} from {app_friendly}."
            case _:
                assert_never(kind)
