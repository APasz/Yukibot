from __future__ import annotations

from .constants import (
    _REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
    log,
)
from .nicegui_protocols import ModWebUi
from .links import mod_web_node_system_path
from .runtime_imports import (
    Awaitable,
    Callable,
    ModWebUser,
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeBlueprintList,
    NodeConfigList,
    NodeConsoleActionList,
    NodeModList,
    NodeModSummary,
    NodeDiskManagementState,
    NodeRestartScheduleState,
    NodeSaveList,
    NodeSettingList,
    NodeStateStreamEvent,
    NodeSystemHistory,
    NodeSystemSummary,
    Power_Level,
    Request,
    TypeVar,
    aiohttp,
    app_scope_from_name,
    asyncio,
    cast,
    config,
    replace,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModWebAppLink,
    ModWebBasePageModel,
    ModWebMinecraftItemRegistrySummary,
    ModWebMinecraftRecipeBookSummary,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebNodeStatus,
    ModWebPageLoadWarning,
    ModWebSevenDaysSandboxOptionsSummary,
)


PageSection = TypeVar("PageSection")
_NODE_RECONNECT_PROBE_INTERVAL_SECONDS = 2.0


class ModWebPageHandlersMixin(ModWebServiceSupport):
    @staticmethod
    def _requested_app_tab_id(*, request: Request, app_entry: NodeAppEntry, can_manage_app: bool) -> str:
        available_tab_ids: list[str] = []
        if app_entry.supports_mods:
            available_tab_ids.append("mods")
        if app_entry.supports_updates:
            available_tab_ids.append("update")
        if app_entry.supports_configs:
            available_tab_ids.append("configs")
        if app_entry.supports_settings and can_manage_app:
            available_tab_ids.append("settings")
        if app_entry.map_url is not None:
            available_tab_ids.append("map")
        if app_entry.supports_saves and can_manage_app:
            available_tab_ids.append("saves")
        if app_entry.supports_blueprints and can_manage_app:
            available_tab_ids.append("blueprints")
        if app_entry.supports_console_actions and can_manage_app:
            available_tab_ids.append("console")
        if app_entry.supports_chat:
            available_tab_ids.append("chat")

        query_params = getattr(request, "query_params", None)
        get_query_value = getattr(query_params, "get", None)
        raw_requested_tab_id = get_query_value("tab") if callable(get_query_value) else None
        requested_tab_id = raw_requested_tab_id.strip().casefold() if isinstance(raw_requested_tab_id, str) else ""
        app_scope = (app_entry.scope or app_scope_from_name(app_entry.name) or "").casefold()
        if requested_tab_id == "recipes" and app_scope == "minecraft":
            return requested_tab_id
        if (
            requested_tab_id == "sandbox"
            and app_scope == "sevendays"
            and app_entry.supports_sevendays_sandbox_options
        ):
            return requested_tab_id
        if requested_tab_id in available_tab_ids:
            return requested_tab_id
        return available_tab_ids[0] if available_tab_ids else ""

    @staticmethod
    def _empty_remote_mod_list(
        *,
        app_entry: NodeAppEntry,
        node: ModWebNodeLink,
        app_stats: NodeAppRuntimeSummary | None = None,
    ) -> NodeModList:
        return NodeModList(
            app_name=app_entry.name,
            app_friendly=app_entry.friendly,
            node=node.node_name,
            summary=NodeModSummary(
                total_count=0,
                enabled_count=0,
                disabled_count=0,
                coremod_count=0,
                downloadable_count=0,
                non_downloadable_count=0,
            ),
            mods=(),
            app_stats=app_stats,
        )

    @staticmethod
    def _empty_remote_save_list(*, app_entry: NodeAppEntry, node: ModWebNodeLink) -> NodeSaveList:
        return NodeSaveList(
            app_name=app_entry.name,
            app_friendly=app_entry.friendly,
            node=node.node_name,
            roots=(),
            saves=(),
        )

    @staticmethod
    def _empty_remote_blueprint_list(*, app_entry: NodeAppEntry, node: ModWebNodeLink) -> NodeBlueprintList:
        return NodeBlueprintList(
            app_name=app_entry.name,
            app_friendly=app_entry.friendly,
            node=node.node_name,
            blueprints=(),
        )

    @staticmethod
    def _empty_remote_setting_list(*, app_entry: NodeAppEntry, node: ModWebNodeLink) -> NodeSettingList:
        return NodeSettingList(
            app_name=app_entry.name,
            app_friendly=app_entry.friendly,
            node=node.node_name,
            editable_count=0,
            restricted_count=0,
            has_pending_changes=False,
            pending_change_count=0,
            required_save_level_name=Power_Level.user.name,
            required_reload_level_name=Power_Level.user.name,
            settings=(),
        )

    @staticmethod
    def _empty_remote_console_action_list(
        *, app_entry: NodeAppEntry, node: ModWebNodeLink
    ) -> NodeConsoleActionList:
        return NodeConsoleActionList(
            app_name=app_entry.name,
            app_friendly=app_entry.friendly,
            node=node.node_name,
            actions=(),
        )

    async def _safe_remote_optional_page_section(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        section_label: str,
        fallback: PageSection,
        load_warnings: list[ModWebPageLoadWarning],
        operation: Callable[[], Awaitable[PageSection]],
    ) -> PageSection:
        try:
            return await operation()
        except Exception as xcp:
            self._warn_page_section_load_failure(
                context=f"Remote mod web app page: node={node.node_name} app={app_name} section={section_label}",
                section_label=section_label,
                error=xcp,
                load_warnings=load_warnings,
            )
            return fallback

    def _on_startup(self) -> None:
        self._backend.start_background_tasks()
        self._startup_signal.set()
        log.info("Mod web startup event received")

    async def _on_shutdown(self) -> None:
        await asyncio.gather(
            self._remote_node_state_broker.close(),
            self._remote_app_state_broker.close(),
            self._remote_chat_broker.close(),
            self._console_stdout_broker.close(),
        )
        await self._close_remote_http_client()

    async def _app_links(self, user: ModWebUser) -> tuple[ModWebAppLink, ...]:
        current_node = self._current_node_link()
        return await self._remote_app_links(current_node, user)

    async def _remote_app_links(self, node: ModWebNodeLink, user: ModWebUser) -> tuple[ModWebAppLink, ...]:
        entries = await self._remote_apps_async(node, user)
        return tuple(self._app_link_from_entry(entry=entry, user=user, node_name=node.node_name) for entry in entries)

    async def _home_app_sections(
        self,
        user: ModWebUser,
        *,
        simulated_down_node_names: tuple[str, ...] = (),
    ) -> tuple[ModWebNodeAppSection, ...]:
        sections: list[ModWebNodeAppSection | None] = [None] * len(self._node_links())
        remote_nodes: list[tuple[int, ModWebNodeLink]] = []
        simulated_down_keys: set[str] = {node_name.casefold() for node_name in simulated_down_node_names}
        for index, node in enumerate(self._node_links()):
            if node.node_name.casefold() in simulated_down_keys:
                sections[index] = self._simulated_remote_node_section(node)
                continue
            remote_nodes.append((index, node))

        async def _remote_section(index: int, node: ModWebNodeLink) -> tuple[int, ModWebNodeAppSection]:
            if node.node_name.casefold() in simulated_down_keys:
                return index, self._simulated_remote_node_section(node)
            try:
                return index, ModWebNodeAppSection(node=node, app_links=await self._remote_app_links(node, user))
            except Exception as xcp:
                if not (self._shutting_down or config.IS_SHUTTINGDOWN):
                    log.warning("Remote mod web home node unavailable: node=%s error=%s", node.node_name, xcp)
                return index, ModWebNodeAppSection(
                    node=node,
                    app_links=(),
                    error=self._friendly_remote_node_error_text(xcp),
                )

        for index, section in await asyncio.gather(*(_remote_section(index, node) for index, node in remote_nodes)):
            sections[index] = section
        return tuple(cast(tuple[ModWebNodeAppSection, ...], tuple(sections)))

    async def _login_node_statuses_async(
        self,
        *,
        simulated_down_node_names: tuple[str, ...] = (),
    ) -> tuple[ModWebNodeStatus, ...]:
        nodes = self._node_links()
        simulated_down_keys: set[str] = {node_name.casefold() for node_name in simulated_down_node_names}

        async def _status(node: ModWebNodeLink) -> ModWebNodeStatus:
            if node.node_name.casefold() in simulated_down_keys:
                return self._simulated_remote_node_status(node)
            return await self._probe_node_status_async(node)

        return tuple(await asyncio.gather(*(_status(node) for node in nodes)))

    async def _probe_node_status_async(
        self,
        node: ModWebNodeLink,
        *,
        log_failures: bool = True,
    ) -> ModWebNodeStatus:
        url = node.latency_probe_url or f"{self._absolute_node_api_base_url(node.api_base_url).rstrip('/')}/ping"
        try:
            session = await self._remote_http_client()
            async with session.get(
                url,
                timeout=self._aiohttp_client_timeout(_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT),
            ) as response:
                status_code = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as xcp:
            if log_failures and not (self._shutting_down or config.IS_SHUTTINGDOWN):
                log.warning("Remote mod web login status probe failed: node=%s error=%s", node.node_name, xcp)
            return ModWebNodeStatus(node=node, alive=False, detail=str(xcp))
        return ModWebNodeStatus(node=node, alive=True, detail=f"HTTP {status_code}")

    @staticmethod
    def _simulated_remote_node_error_text() -> str:
        return "This node is being simulated as unavailable in dev mode."

    @classmethod
    def _simulated_remote_node_section(cls, node: ModWebNodeLink) -> ModWebNodeAppSection:
        return ModWebNodeAppSection(
            node=node,
            app_links=(),
            error=cls._simulated_remote_node_error_text(),
            is_simulated_down=True,
        )

    @classmethod
    def _simulated_remote_node_status(cls, node: ModWebNodeLink) -> ModWebNodeStatus:
        return ModWebNodeStatus(
            node=node,
            alive=False,
            detail=cls._simulated_remote_node_error_text(),
            is_simulated_down=True,
        )

    async def _render_mods_page(self, *, ui: ModWebUi, app_name: str, request: Request) -> None:
        await self._render_node_mods_page(
            ui=ui,
            node_name=self._default_mod_web_node_name(),
            app_name=app_name,
            request=request,
        )

    async def _render_node_system_page(self, *, ui: ModWebUi, node_name: str, request: Request) -> None:
        user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.sudo)
        if user is None:
            return

        async def _load_system_history(node: ModWebNodeLink) -> NodeSystemHistory:
            try:
                return await self._remote_node_system_history_async(node, user)
            except Exception as xcp:
                log_method = log.info if self._remote_node_error_is_transient(xcp) else log.warning
                log_method("Remote mod web node system history unavailable: node=%s error=%s", node.node_name, xcp)
                return NodeSystemHistory.empty()

        async def _load_restart_schedules(node: ModWebNodeLink) -> NodeRestartScheduleState | None:
            try:
                return await self._remote_restart_schedules_async(node, user)
            except Exception as xcp:
                log_method = log.info if self._remote_node_error_is_transient(xcp) else log.warning
                log_method("Remote restart schedules unavailable: node=%s error=%s", node.node_name, xcp)
                return None

        async def _load_node_capacity(node: ModWebNodeLink) -> config.NodeCapacityProfile | None:
            if not self._user_has_level(user, Power_Level.root):
                return None
            try:
                return await self._node_capacity(node_name=node.node_name, user=user)
            except Exception as xcp:
                log.warning("Remote node capacity unavailable: node=%s error=%s", node.node_name, xcp)
                return None

        async def _load_node_font_sources(node: ModWebNodeLink) -> config.NodeFontSourceSettings | None:
            if not self._user_has_level(user, Power_Level.sudo):
                return None
            try:
                return await self._node_font_sources(node_name=node.node_name, user=user)
            except Exception as xcp:
                log.warning("Remote node font sources unavailable: node=%s error=%s", node.node_name, xcp)
                return None

        async def _load_node_disk_settings(node: ModWebNodeLink) -> NodeDiskManagementState | None:
            if not self._user_has_level(user, Power_Level.root):
                return None
            try:
                return await self._node_disk_settings(node_name=node.node_name, user=user)
            except Exception as xcp:
                log.warning("Remote node disk settings unavailable: node=%s error=%s", node.node_name, xcp)
                return None

        node: ModWebNodeLink | None = None
        try:
            node = self._remote_node_link(node_name)
            (
                system_summary,
                system_history,
                app_entries,
                restart_schedules,
                node_capacity,
                node_font_sources,
                node_disk_settings,
            ) = await asyncio.gather(
                self._remote_node_system_summary_async(node, user),
                _load_system_history(node),
                self._remote_apps_async(node, user),
                _load_restart_schedules(node),
                _load_node_capacity(node),
                _load_node_font_sources(node),
                _load_node_disk_settings(node),
            )
        except Exception as xcp:
            if not self._remote_node_error_is_transient(xcp):
                log.exception("Remote mod web node system page render failed: node=%s", node_name)
                self._render_remote_node_unavailable_page(ui=ui, node_name=node_name, exception=xcp)
                return
            log.info("Remote mod web node temporarily unavailable: node=%s error=%s", node_name, xcp)
            retry_url = mod_web_node_system_path(node_name)
            self._render_remote_node_unavailable_page(
                ui=ui,
                node_name=node_name,
                exception=xcp,
                retry_url=retry_url,
            )
            if node is None:
                return
            retry_node = node
            reconnect_in_progress = False

            async def _reconnect_when_available() -> None:
                nonlocal reconnect_in_progress
                if reconnect_in_progress:
                    return
                reconnect_in_progress = True
                try:
                    status = await self._probe_node_status_async(retry_node, log_failures=False)
                    if status.alive:
                        ui.navigate.to(retry_url)
                finally:
                    reconnect_in_progress = False

            reconnect_timer = ui.timer(
                _NODE_RECONNECT_PROBE_INTERVAL_SECONDS,
                _reconnect_when_available,
            )
            self._register_timer_cleanup(ui=ui, timer=reconnect_timer)
            return

        if node is None:
            raise RuntimeError("Resolved system node unexpectedly missing after successful page load.")

        def subscribe_node_state_updates(
            on_update: Callable[[NodeStateStreamEvent], None],
        ) -> Callable[[], None]:
            return self._create_remote_node_state_subscription(
                node=node,
                user=user,
                on_update=on_update,
            )

        self._render_node_system_dashboard(
            ui=ui,
            node=node,
            user=user,
            initial_system_summary=system_summary,
            initial_system_history=system_history,
            initial_app_entries=app_entries,
            initial_restart_schedules=restart_schedules,
            initial_node_capacity=node_capacity,
            initial_node_font_sources=node_font_sources,
            initial_node_disk_settings=node_disk_settings,
            current_url=self._request_path(request),
            simulated_down_node_names=self._simulated_down_node_names(request),
            subscribe_node_state_updates=subscribe_node_state_updates,
        )

    async def _render_node_mods_page(self, *, ui: ModWebUi, node_name: str, app_name: str, request: Request) -> None:
        user = await self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
        if user is None:
            return
        try:
            node = self._remote_node_link(node_name)
        except Exception as xcp:
            log.exception("Remote mod web mods page render failed: node=%s app=%s", node_name, app_name)
            self._render_error_page(ui=ui, title="Page unavailable", detail=str(xcp), app_name=app_name)
            return
        try:
            app_entry = await self._remote_app_entry_async(node, app_name, user)
            resolved_app_color_hex = self._resolved_app_color_hex(
                app_name=app_entry.name,
                scope=app_entry.scope,
                color_hex=app_entry.color_hex,
            )
            can_manage_app = self._user_has_level(user, Power_Level.user)
            can_read_configs = app_entry.supports_configs and self._user_has_level(user, app_entry.config_read_level)
            active_tab_id = self._requested_app_tab_id(
                request=request,
                app_entry=app_entry,
                can_manage_app=can_manage_app,
            )
            resolved_app_scope = (
                app_entry.scope.casefold()
                if isinstance(app_entry.scope, str) and app_entry.scope.strip()
                else app_scope_from_name(app_entry.name)
            )
            load_warnings: list[ModWebPageLoadWarning] = []
            empty_configs = self._empty_config_list(
                app_name=app_entry.name,
                app_friendly=app_entry.friendly,
                node_name=node.node_name,
            )
            configs_job = (
                self._safe_remote_optional_page_section(
                    node=node,
                    app_name=app_name,
                    section_label="Configs",
                    fallback=empty_configs,
                    load_warnings=load_warnings,
                    operation=lambda: self._remote_config_list_async(node, app_name, user),
                )
                if can_read_configs and active_tab_id == "configs"
                else asyncio.sleep(0, result=empty_configs)
            )
            if app_entry.supports_mods:
                empty_mods = self._empty_remote_mod_list(app_entry=app_entry, node=node)
                load_mods = active_tab_id in {"mods", "recipes"} or resolved_app_scope == "minecraft"
                mods_job = (
                    self._remote_mod_list_async(node, app_name, user)
                    if load_mods
                    else asyncio.sleep(0, result=empty_mods)
                )
                runtime_job = (
                    asyncio.sleep(0, result=None)
                    if load_mods
                    else self._remote_app_runtime_summary_async(node, app_name, user)
                )
                empty_saves = (
                    self._empty_remote_save_list(app_entry=app_entry, node=node)
                    if app_entry.supports_saves and can_manage_app
                    else None
                )
                empty_blueprints = (
                    self._empty_remote_blueprint_list(app_entry=app_entry, node=node)
                    if app_entry.supports_blueprints and can_manage_app
                    else None
                )
                empty_settings = (
                    self._empty_remote_setting_list(app_entry=app_entry, node=node)
                    if app_entry.supports_settings and can_manage_app
                    else None
                )
                empty_console_actions = (
                    self._empty_remote_console_action_list(app_entry=app_entry, node=node)
                    if app_entry.supports_console_actions and can_manage_app
                    else None
                )
                saves_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Saves",
                        fallback=empty_saves,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_save_list_async(node, app_name, user),
                    )
                    if empty_saves is not None and active_tab_id == "saves"
                    else asyncio.sleep(0, result=empty_saves)
                )
                blueprints_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Blueprints",
                        fallback=empty_blueprints,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_blueprint_list_async(node, app_name, user),
                    )
                    if empty_blueprints is not None and active_tab_id == "blueprints"
                    else asyncio.sleep(0, result=empty_blueprints)
                )
                settings_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Settings",
                        fallback=empty_settings,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_setting_list_async(node, app_name, user),
                    )
                    if empty_settings is not None and active_tab_id == "settings"
                    else asyncio.sleep(0, result=empty_settings)
                )
                console_actions_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Console",
                        fallback=empty_console_actions,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_console_action_list_async(node, app_name, user),
                    )
                    if empty_console_actions is not None and active_tab_id == "console"
                    else asyncio.sleep(0, result=empty_console_actions)
                )
                remote_results = await asyncio.gather(
                    mods_job,
                    configs_job,
                    saves_job,
                    blueprints_job,
                    settings_job,
                    console_actions_job,
                    self._remote_node_system_summary_or_none_async(
                        node,
                        user,
                        error_context="Remote mod web app system summary failed",
                    ),
                    runtime_job,
                )
                mods = cast(NodeModList, remote_results[0])
                configs = cast(NodeConfigList, remote_results[1])
                saves = cast(NodeSaveList | None, remote_results[2])
                blueprints = cast(NodeBlueprintList | None, remote_results[3])
                settings = cast(NodeSettingList | None, remote_results[4])
                console_actions = cast(NodeConsoleActionList | None, remote_results[5])
                system_summary = cast(NodeSystemSummary | None, remote_results[6])
                if mods.app_stats is None and remote_results[7] is not None:
                    mods = replace(mods, app_stats=cast(NodeAppRuntimeSummary, remote_results[7]))
                minecraft_recipes: ModWebMinecraftRecipeBookSummary | None = None
                minecraft_item_registry: ModWebMinecraftItemRegistrySummary | None = None
                if resolved_app_scope == "minecraft" and active_tab_id == "recipes":
                    minecraft_recipes, minecraft_item_registry = await self._remote_minecraft_recipe_summaries_async(
                        node,
                        app_name,
                        user,
                    )
                sevendays_sandbox_options: ModWebSevenDaysSandboxOptionsSummary | None = None
                if resolved_app_scope == "sevendays" and active_tab_id == "sandbox":
                    sevendays_sandbox_options = await self._remote_sevendays_sandbox_options_summary_async(
                        node,
                        app_name,
                        user,
                    )
                elif app_entry.supports_sevendays_sandbox_options:
                    sevendays_sandbox_options = ModWebSevenDaysSandboxOptionsSummary(
                        data_path=".yukibot/sandbox_options.json",
                        file_exists=False,
                        app_version=mods.app_stats.version if mods.app_stats is not None else None,
                    )
                model = self._remote_page_model(
                    node=node,
                    app_scope=app_entry.scope,
                    mods=mods,
                    supports_configs=app_entry.supports_configs,
                    config_read_level=app_entry.config_read_level,
                    config_write_level=app_entry.config_write_level,
                    supports_save_uploads=app_entry.supports_save_uploads,
                    supports_save_rename=app_entry.supports_save_rename,
                    save_write_level=app_entry.save_write_level,
                    configs=configs,
                    saves=saves,
                    blueprints=blueprints,
                    settings=settings,
                    console_actions=console_actions,
                    map_url=app_entry.map_url,
                    can_write_map_annotations=can_manage_app and app_entry.map_url is not None,
                    supports_chat=app_entry.supports_chat,
                    supports_updates=app_entry.supports_updates,
                    chat_url=(
                        self.node_app_chat_path(node.node_name, app_entry.name) if app_entry.supports_chat else None
                    ),
                    update_info=app_entry.update_info,
                    update_status=app_entry.update_status,
                    app_color_hex=resolved_app_color_hex,
                    resource_points=app_entry.resource_points,
                    app_title_font_preset=app_entry.title_font_preset,
                    app_notes=app_entry.notes,
                    join_address=app_entry.join_address,
                    join_direct_ip_address=app_entry.join_direct_ip_address,
                    lifecycle_notice_started=app_entry.lifecycle_notice_started,
                    lifecycle_notice_stopped=app_entry.lifecycle_notice_stopped,
                    lifecycle_notice_crashed=app_entry.lifecycle_notice_crashed,
                    relay_notice_player_session=app_entry.relay_notice_player_session,
                    relay_notice_player_death=app_entry.relay_notice_player_death,
                    relay_notice_progress=app_entry.relay_notice_progress,
                    relay_notice_progress_label=app_entry.relay_notice_progress_label,
                    relay_advancements_enabled=app_entry.relay_advancements_enabled,
                    relay_advancement_term=app_entry.relay_advancement_term,
                    activity_providers=app_entry.activity_providers,
                    load_warnings=tuple(load_warnings),
                    minecraft_recipes=minecraft_recipes,
                    minecraft_item_registry=minecraft_item_registry,
                    sevendays_sandbox_options=sevendays_sandbox_options,
                    app_start_blocked=self._app_start_blocked_remote(
                        app_name=mods.app_name,
                        app_stats=mods.app_stats,
                        start_blocked_app_ids=() if system_summary is None else system_summary.start_blocked_app_ids,
                    ),
                )
                chat_surface = (
                    await self._remote_chat_surface_config(
                        node=node,
                        app_name=app_name,
                        request=request,
                        user=user,
                        app_entry=app_entry,
                        app_stats=model.app_stats,
                        include_runtime_updates=False,
                    )
                    if model.supports_chat and active_tab_id == "chat"
                    else None
                )

                async def _refresh_app_stats() -> NodeAppRuntimeSummary | None:
                    return await self._remote_app_runtime_summary_async(node, app_name, user)

                async def _refresh_runtime_model() -> ModWebBasePageModel:
                    return await self._refresh_runtime_model(model=model, user=user)

                def _subscribe_app_state(
                    on_update: Callable[[NodeAppStateStreamEvent], None],
                ) -> Callable[[], None]:
                    return self._create_remote_app_state_subscription(
                        node=node,
                        app_name=app_name,
                        user=user,
                        on_update=on_update,
                    )

                self._render_page(
                    ui=ui,
                    model=model,
                    user=user,
                    current_url=self._request_path(request),
                    refresh_async_app_stats=_refresh_app_stats,
                    refresh_async_runtime_model=_refresh_runtime_model,
                    subscribe_app_state_updates=_subscribe_app_state,
                    initial_system_summary=system_summary,
                    chat_surface=chat_surface,
                )
            else:
                empty_saves = (
                    self._empty_remote_save_list(app_entry=app_entry, node=node)
                    if app_entry.supports_saves and can_manage_app
                    else None
                )
                empty_blueprints = (
                    self._empty_remote_blueprint_list(app_entry=app_entry, node=node)
                    if app_entry.supports_blueprints and can_manage_app
                    else None
                )
                empty_settings = (
                    self._empty_remote_setting_list(app_entry=app_entry, node=node)
                    if app_entry.supports_settings and can_manage_app
                    else None
                )
                empty_console_actions = (
                    self._empty_remote_console_action_list(app_entry=app_entry, node=node)
                    if app_entry.supports_console_actions and can_manage_app
                    else None
                )
                saves_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Saves",
                        fallback=empty_saves,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_save_list_async(node, app_name, user),
                    )
                    if empty_saves is not None and active_tab_id == "saves"
                    else asyncio.sleep(0, result=empty_saves)
                )
                blueprints_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Blueprints",
                        fallback=empty_blueprints,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_blueprint_list_async(node, app_name, user),
                    )
                    if empty_blueprints is not None and active_tab_id == "blueprints"
                    else asyncio.sleep(0, result=empty_blueprints)
                )
                settings_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Settings",
                        fallback=empty_settings,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_setting_list_async(node, app_name, user),
                    )
                    if empty_settings is not None and active_tab_id == "settings"
                    else asyncio.sleep(0, result=empty_settings)
                )
                console_actions_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Console",
                        fallback=empty_console_actions,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_console_action_list_async(node, app_name, user),
                    )
                    if empty_console_actions is not None and active_tab_id == "console"
                    else asyncio.sleep(0, result=empty_console_actions)
                )
                remote_results = await asyncio.gather(
                    configs_job,
                    saves_job,
                    blueprints_job,
                    settings_job,
                    console_actions_job,
                    self._remote_app_runtime_summary_async(node, app_name, user),
                    self._remote_node_system_summary_or_none_async(
                        node,
                        user,
                        error_context="Remote mod web overview system summary failed",
                    ),
                )
                configs = cast(NodeConfigList, remote_results[0])
                saves = cast(NodeSaveList | None, remote_results[1])
                blueprints = cast(NodeBlueprintList | None, remote_results[2])
                settings = cast(NodeSettingList | None, remote_results[3])
                console_actions = cast(NodeConsoleActionList | None, remote_results[4])
                app_stats = cast(NodeAppRuntimeSummary | None, remote_results[5])
                system_summary = cast(NodeSystemSummary | None, remote_results[6])
                model = self._remote_overview_page_model(
                    node=node,
                    app_name=app_entry.name,
                    app_friendly=app_entry.friendly,
                    app_scope=app_entry.scope,
                    app_color_hex=resolved_app_color_hex,
                    supports_configs=app_entry.supports_configs,
                    config_read_level=app_entry.config_read_level,
                    config_write_level=app_entry.config_write_level,
                    supports_save_uploads=app_entry.supports_save_uploads,
                    supports_save_rename=app_entry.supports_save_rename,
                    save_write_level=app_entry.save_write_level,
                    configs=configs,
                    saves=saves,
                    blueprints=blueprints,
                    settings=settings,
                    console_actions=console_actions,
                    map_url=app_entry.map_url,
                    can_write_map_annotations=can_manage_app and app_entry.map_url is not None,
                    supports_chat=app_entry.supports_chat,
                    supports_updates=app_entry.supports_updates,
                    chat_url=(
                        self.node_app_chat_path(node.node_name, app_entry.name) if app_entry.supports_chat else None
                    ),
                    update_info=app_entry.update_info,
                    update_status=app_entry.update_status,
                    app_stats=app_stats,
                    resource_points=app_entry.resource_points,
                    app_title_font_preset=app_entry.title_font_preset,
                    app_notes=app_entry.notes,
                    join_address=app_entry.join_address,
                    join_direct_ip_address=app_entry.join_direct_ip_address,
                    lifecycle_notice_started=app_entry.lifecycle_notice_started,
                    lifecycle_notice_stopped=app_entry.lifecycle_notice_stopped,
                    lifecycle_notice_crashed=app_entry.lifecycle_notice_crashed,
                    relay_notice_player_session=app_entry.relay_notice_player_session,
                    relay_notice_player_death=app_entry.relay_notice_player_death,
                    relay_notice_progress=app_entry.relay_notice_progress,
                    relay_notice_progress_label=app_entry.relay_notice_progress_label,
                    relay_advancements_enabled=app_entry.relay_advancements_enabled,
                    relay_advancement_term=app_entry.relay_advancement_term,
                    activity_providers=app_entry.activity_providers,
                    load_warnings=tuple(load_warnings),
                    app_start_blocked=self._app_start_blocked_remote(
                        app_name=app_entry.name,
                        app_stats=app_stats,
                        start_blocked_app_ids=() if system_summary is None else system_summary.start_blocked_app_ids,
                    ),
                )
                chat_surface = (
                    await self._remote_chat_surface_config(
                        node=node,
                        app_name=app_name,
                        request=request,
                        user=user,
                        app_entry=app_entry,
                        app_stats=model.app_stats,
                        include_runtime_updates=False,
                    )
                    if model.supports_chat and active_tab_id == "chat"
                    else None
                )

                async def _refresh_app_stats() -> NodeAppRuntimeSummary | None:
                    return await self._remote_app_runtime_summary_async(node, app_name, user)

                async def _refresh_runtime_model() -> ModWebBasePageModel:
                    return await self._refresh_runtime_model(model=model, user=user)

                def _subscribe_app_state(
                    on_update: Callable[[NodeAppStateStreamEvent], None],
                ) -> Callable[[], None]:
                    return self._create_remote_app_state_subscription(
                        node=node,
                        app_name=app_name,
                        user=user,
                        on_update=on_update,
                    )

                self._render_overview_page(
                    ui=ui,
                    model=model,
                    user=user,
                    current_url=self._request_path(request),
                    refresh_async_app_stats=_refresh_app_stats,
                    refresh_async_runtime_model=_refresh_runtime_model,
                    subscribe_app_state_updates=_subscribe_app_state,
                    initial_system_summary=system_summary,
                    chat_surface=chat_surface,
                )
        except Exception as xcp:
            log.exception("Remote mod web app page render failed: node=%s app=%s", node_name, app_name)
            self._render_error_page(ui=ui, title="Page unavailable", detail=str(xcp), app_name=app_name)
