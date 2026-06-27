from __future__ import annotations

from .constants import (
    _REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
    log,
)
from .nicegui_protocols import ModWebUi
from .runtime_imports import (
    Callable,
    ModWebUser,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeBlueprintList,
    NodeConfigList,
    NodeConsoleActionList,
    NodeModList,
    NodeSaveList,
    NodeSettingList,
    NodeStateStreamEvent,
    NodeSystemSummary,
    Power_Level,
    Request,
    app_scope_from_name,
    asyncio,
    cast,
    config,
    requests,
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
    ModWebTitleStat,
)


class ModWebPageHandlersMixin(ModWebServiceSupport):
    async def _safe_remote_optional_page_section(
        self,
        *,
        node: ModWebNodeLink,
        app_name: str,
        section_label: str,
        fallback: object,
        load_warnings: list[ModWebPageLoadWarning],
        operation: Callable[[], object],
    ) -> object:
        try:
            return await asyncio.to_thread(operation)
        except Exception as xcp:
            self._warn_page_section_load_failure(
                context=f"Remote mod web app page: node={node.node_name} app={app_name} section={section_label}",
                section_label=section_label,
                error=xcp,
                load_warnings=load_warnings,
            )
            return fallback

    def _on_startup(self) -> None:
        self._startup_signal.set()
        log.info("Mod web startup event received")

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

    def _login_node_statuses(self, *, simulated_down_node_names: tuple[str, ...] = ()) -> tuple[ModWebNodeStatus, ...]:
        statuses: list[ModWebNodeStatus] = []
        simulated_down_keys: set[str] = {node_name.casefold() for node_name in simulated_down_node_names}
        for node in self._node_links():
            if node.node_name.casefold() in simulated_down_keys:
                statuses.append(self._simulated_remote_node_status(node))
                continue
            statuses.append(self._probe_node_status(node))
        return tuple(statuses)

    def _probe_node_status(self, node: ModWebNodeLink) -> ModWebNodeStatus:
        url = node.latency_probe_url or f"{self._absolute_node_api_base_url(node.api_base_url).rstrip('/')}/ping"
        try:
            response = requests.get(url, timeout=_REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT)
        except requests.RequestException as xcp:
            if not (self._shutting_down or config.IS_SHUTTINGDOWN):
                log.warning("Remote mod web login status probe failed: node=%s error=%s", node.node_name, xcp)
            return ModWebNodeStatus(node=node, alive=False, detail=str(xcp))
        return ModWebNodeStatus(node=node, alive=True, detail=f"HTTP {response.status_code}")

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

    async def _render_node_page(self, *, ui: ModWebUi, node_name: str, request: Request) -> None:
        user = self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.user)
        if user is None:
            return
        try:
            node = self._remote_node_link(node_name)
            app_links = await self._remote_app_links(node, user)
        except Exception as xcp:
            log.exception("Remote mod web node page render failed: node=%s", node_name)
            self._render_remote_node_unavailable_page(ui=ui, node_name=node_name, exception=xcp)
            return
        system_summary = await self._remote_node_system_summary_or_none_async(
            node,
            user,
            error_context="Remote mod web node system summary failed",
        )

        async def _refresh_title_stats() -> tuple[ModWebTitleStat, ...]:
            latest = await self._remote_node_system_summary_async(node, user)
            return self._build_system_title_stats(latest)

        def subscribe_node_state_updates(
            on_update: Callable[[NodeStateStreamEvent], None],
        ) -> Callable[[], None]:
            return self._create_remote_node_state_subscription(
                node=node,
                user=user,
                on_update=on_update,
            )

        self._render_node_apps_page(
            ui=ui,
            node=node,
            app_links=app_links,
            user=user,
            show_api_actions=self._app_list_api_actions_enabled(request),
            initial_title_stats=self._build_system_title_stats(system_summary),
            refresh_async_title_stats=_refresh_title_stats,
            subscribe_node_state_updates=subscribe_node_state_updates,
        )

    async def _render_node_mods_page(self, *, ui: ModWebUi, node_name: str, app_name: str, request: Request) -> None:
        user = self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
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
                    operation=lambda: self._remote_config_list(node, app_name, user),
                )
                if can_read_configs
                else asyncio.sleep(0, result=empty_configs)
            )
            if app_entry.supports_mods:
                saves_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Saves",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_save_list(node, app_name, user),
                    )
                    if app_entry.supports_saves and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                blueprints_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Blueprints",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_blueprint_list(node, app_name, user),
                    )
                    if app_entry.supports_blueprints and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                settings_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Settings",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_setting_list(node, app_name, user),
                    )
                    if app_entry.supports_settings and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                console_actions_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Console",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_console_action_list(node, app_name, user),
                    )
                    if app_entry.supports_console_actions and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                remote_results = await asyncio.gather(
                    asyncio.to_thread(self._remote_mod_list, node, app_name, user),
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
                )
                mods = cast(NodeModList, remote_results[0])
                configs = cast(NodeConfigList, remote_results[1])
                saves = cast(NodeSaveList | None, remote_results[2])
                blueprints = cast(NodeBlueprintList | None, remote_results[3])
                settings = cast(NodeSettingList | None, remote_results[4])
                console_actions = cast(NodeConsoleActionList | None, remote_results[5])
                system_summary = cast(NodeSystemSummary | None, remote_results[6])
                minecraft_scope = (
                    app_entry.scope.casefold()
                    if isinstance(app_entry.scope, str) and app_entry.scope.strip()
                    else app_scope_from_name(app_entry.name)
                )
                minecraft_recipes: ModWebMinecraftRecipeBookSummary | None = None
                minecraft_item_registry: ModWebMinecraftItemRegistrySummary | None = None
                if minecraft_scope == "minecraft":
                    minecraft_recipes, minecraft_item_registry = await asyncio.to_thread(
                        self._remote_minecraft_recipe_summaries,
                        node,
                        app_name,
                        user,
                    )
                sevendays_sandbox_options: ModWebSevenDaysSandboxOptionsSummary | None = None
                if minecraft_scope == "sevendays":
                    sevendays_sandbox_options = await asyncio.to_thread(
                        self._remote_sevendays_sandbox_options_summary,
                        node,
                        app_name,
                        user,
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
                    if model.supports_chat
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
                saves_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Saves",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_save_list(node, app_name, user),
                    )
                    if app_entry.supports_saves and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                blueprints_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Blueprints",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_blueprint_list(node, app_name, user),
                    )
                    if app_entry.supports_blueprints and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                settings_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Settings",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_setting_list(node, app_name, user),
                    )
                    if app_entry.supports_settings and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                console_actions_job = (
                    self._safe_remote_optional_page_section(
                        node=node,
                        app_name=app_name,
                        section_label="Console",
                        fallback=None,
                        load_warnings=load_warnings,
                        operation=lambda: self._remote_console_action_list(node, app_name, user),
                    )
                    if app_entry.supports_console_actions and can_manage_app
                    else asyncio.sleep(0, result=None)
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
                    if model.supports_chat
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
