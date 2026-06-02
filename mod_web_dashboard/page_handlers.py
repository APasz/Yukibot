from __future__ import annotations

from .runtime_imports import (
    Callable,
    ModWebUser,
    NodeAppStateStreamEvent,
    NodeStateStreamEvent,
    Power_Level,
    Request,
    asyncio,
    config,
    requests,
)
from .constants import (
    _REMOTE_NODE_PRESENCE_REQUEST_TIMEOUT,
    log,
)
from .nicegui_protocols import ModWebUi
from .types import (
    ModWebAppLink,
    ModWebBasePageModel,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebNodeStatus,
    ModWebTitleStat,
)

from .service_base import ModWebServiceSupport

class ModWebPageHandlersMixin(ModWebServiceSupport):
    def _on_startup(self) -> None:
        self._startup_signal.set()
        log.info("Mod web startup event received")

    async def _app_links(self, user: ModWebUser) -> tuple[ModWebAppLink, ...]:
        entries = await self._node_api.list_apps()
        return tuple(
            self._app_link_from_entry(
                entry=entry,
                user=user,
                node_name=config.MOD_WEB_SERVER.node_name,
            )
            for entry in entries
        )

    async def _remote_app_links(self, node: ModWebNodeLink, user: ModWebUser) -> tuple[ModWebAppLink, ...]:
        entries = await asyncio.to_thread(self._remote_apps, node, user)
        return tuple(self._app_link_from_entry(entry=entry, user=user, node_name=node.node_name) for entry in entries)

    async def _home_app_sections(
        self,
        user: ModWebUser,
        *,
        simulated_down_node_names: tuple[str, ...] = (),
    ) -> tuple[ModWebNodeAppSection, ...]:
        sections: list[ModWebNodeAppSection] = []
        remote_nodes: list[ModWebNodeLink] = []
        simulated_down_keys: set[str] = {node_name.casefold() for node_name in simulated_down_node_names}
        for node in self._node_links():
            if node.is_current:
                sections.append(ModWebNodeAppSection(node=node, app_links=await self._app_links(user)))
            else:
                remote_nodes.append(node)

        async def _remote_section(node: ModWebNodeLink) -> ModWebNodeAppSection:
            if node.node_name.casefold() in simulated_down_keys:
                return self._simulated_remote_node_section(node)
            try:
                return ModWebNodeAppSection(node=node, app_links=await self._remote_app_links(node, user))
            except Exception as xcp:
                if not (self._shutting_down or config.IS_SHUTTINGDOWN):
                    log.warning("Remote mod web home node unavailable: node=%s error=%s", node.node_name, xcp)
                return ModWebNodeAppSection(
                    node=node,
                    app_links=(),
                    error=self._friendly_remote_node_error_text(xcp),
                )

        sections.extend(await asyncio.gather(*(_remote_section(node) for node in remote_nodes)))
        return tuple(sections)

    def _login_node_statuses(self, *, simulated_down_node_names: tuple[str, ...] = ()) -> tuple[ModWebNodeStatus, ...]:
        statuses: list[ModWebNodeStatus] = []
        simulated_down_keys: set[str] = {node_name.casefold() for node_name in simulated_down_node_names}
        for node in self._node_links():
            if node.is_current:
                statuses.append(ModWebNodeStatus(node=node, alive=True))
                continue
            if node.node_name.casefold() in simulated_down_keys:
                statuses.append(self._simulated_remote_node_status(node))
                continue
            statuses.append(self._probe_node_status(node))
        return tuple(statuses)

    def _probe_node_status(self, node: ModWebNodeLink) -> ModWebNodeStatus:
        url = f"{node.api_base_url.rstrip('/')}/apps"
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
        user = self._authorised_page_user(ui=ui, request=request, required_level=Power_Level.visitor)
        if user is None:
            return
        try:
            app = self._resolve_app(app_name)
        except Exception as xcp:
            log.exception("Mod web page render failed: app=%s", app_name)
            self._render_error_page(ui=ui, title="Page unavailable", detail=str(xcp), app_name=app_name)
            return
        try:
            if app.mods is not None:
                model = await self._build_page_model(app, user=user)

                async def _refresh_title_stats() -> tuple[ModWebTitleStat, ...]:
                    return self._build_app_title_stats(await self._node_api.build_app_runtime_summary(app))

                async def _refresh_runtime_model() -> ModWebBasePageModel:
                    return await self._refresh_runtime_model(model=model, user=user)

                def _subscribe_app_state(
                    on_update: Callable[[NodeAppStateStreamEvent], None],
                ) -> Callable[[], None]:
                    return self._subscribe_local_app_state(app=app, on_update=on_update)

                self._render_page(
                    ui=ui,
                    model=model,
                    user=user,
                    current_url=self._request_path(request),
                    refresh_async_title_stats=_refresh_title_stats,
                    refresh_async_runtime_model=_refresh_runtime_model,
                    subscribe_app_state_updates=_subscribe_app_state,
                    local_app=app,
                )
            else:
                model = await self._build_overview_page_model(app, user=user)

                async def _refresh_title_stats() -> tuple[ModWebTitleStat, ...]:
                    return self._build_app_title_stats(await self._node_api.build_app_runtime_summary(app))

                async def _refresh_runtime_model() -> ModWebBasePageModel:
                    return await self._refresh_runtime_model(model=model, user=user)

                def _subscribe_app_state(
                    on_update: Callable[[NodeAppStateStreamEvent], None],
                ) -> Callable[[], None]:
                    return self._subscribe_local_app_state(app=app, on_update=on_update)

                self._render_overview_page(
                    ui=ui,
                    model=model,
                    user=user,
                    current_url=self._request_path(request),
                    refresh_async_title_stats=_refresh_title_stats,
                    refresh_async_runtime_model=_refresh_runtime_model,
                    subscribe_app_state_updates=_subscribe_app_state,
                    local_app=app,
                )
        except Exception as xcp:
            log.exception("Mod web app page render failed: app=%s", app_name)
            self._render_error_page(ui=ui, title="Page unavailable", detail=str(xcp), app_name=app_name)

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
        try:
            system_summary = await asyncio.to_thread(self._remote_node_system_summary, node, user)
        except Exception as xcp:
            log.warning("Remote mod web node system summary failed: node=%s error=%s", node_name, xcp)
            system_summary = None

        async def _refresh_title_stats() -> tuple[ModWebTitleStat, ...]:
            latest = await asyncio.to_thread(self._remote_node_system_summary, node, user)
            return self._build_system_title_stats(latest)

        subscribe_node_state_updates: Callable[[Callable[[NodeStateStreamEvent], None]], Callable[[], None]] | None
        if node.is_current:
            subscribe_node_state_updates = self._node_api.subscribe_local_node_state
        else:
            subscribe_node_state_updates = lambda on_update: self._create_remote_node_state_subscription(
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
            app_entry = await asyncio.to_thread(self._remote_app_entry, node, app_name, user)
            can_manage_app = self._user_has_level(user, Power_Level.user)
            can_read_configs = app_entry.supports_configs and self._user_has_level(user, app_entry.config_read_level)
            configs_job = (
                asyncio.to_thread(self._remote_config_list, node, app_name, user)
                if can_read_configs
                else asyncio.sleep(
                    0,
                    result=self._empty_config_list(
                        app_name=app_entry.name, app_friendly=app_entry.friendly, node_name=node.node_name
                    ),
                )
            )
            if app_entry.supports_mods:
                saves_job = (
                    asyncio.to_thread(self._remote_save_list, node, app_name, user)
                    if app_entry.supports_saves and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                settings_job = (
                    asyncio.to_thread(self._remote_setting_list, node, app_name, user)
                    if app_entry.supports_settings and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                console_actions_job = (
                    asyncio.to_thread(self._remote_console_action_list, node, app_name, user)
                    if app_entry.supports_console_actions and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                mods, configs, saves, settings, console_actions, system_summary = await asyncio.gather(
                    asyncio.to_thread(self._remote_mod_list, node, app_name, user),
                    configs_job,
                    saves_job,
                    settings_job,
                    console_actions_job,
                    asyncio.to_thread(self._remote_node_system_summary, node, user),
                )
                model = self._remote_page_model(
                    node=node,
                    mods=mods,
                    supports_configs=app_entry.supports_configs,
                    config_read_level=app_entry.config_read_level,
                    config_write_level=app_entry.config_write_level,
                    supports_save_uploads=app_entry.supports_save_uploads,
                    supports_save_rename=app_entry.supports_save_rename,
                    save_write_level=app_entry.save_write_level,
                    configs=configs,
                    saves=saves,
                    settings=settings,
                    console_actions=console_actions,
                    supports_chat=app_entry.supports_chat,
                    chat_url=(
                        self.node_app_chat_path(node.node_name, app_entry.name) if app_entry.supports_chat else None
                    ),
                    app_color_hex=app_entry.color_hex,
                    app_start_blocked=self._app_start_blocked_remote(
                        app_friendly=mods.app_friendly,
                        app_stats=mods.app_stats,
                        running_names=system_summary.running_names,
                    ),
                )

                async def _refresh_title_stats() -> tuple[ModWebTitleStat, ...]:
                    latest = await asyncio.to_thread(self._remote_app_runtime_summary, node, app_name, user)
                    return self._build_app_title_stats(latest)

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
                    refresh_async_title_stats=_refresh_title_stats,
                    refresh_async_runtime_model=_refresh_runtime_model,
                    subscribe_app_state_updates=_subscribe_app_state,
                    initial_system_summary=system_summary,
                )
            else:
                saves_job = (
                    asyncio.to_thread(self._remote_save_list, node, app_name, user)
                    if app_entry.supports_saves and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                settings_job = (
                    asyncio.to_thread(self._remote_setting_list, node, app_name, user)
                    if app_entry.supports_settings and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                console_actions_job = (
                    asyncio.to_thread(self._remote_console_action_list, node, app_name, user)
                    if app_entry.supports_console_actions and can_manage_app
                    else asyncio.sleep(0, result=None)
                )
                configs, saves, settings, console_actions, app_stats, system_summary = await asyncio.gather(
                    configs_job,
                    saves_job,
                    settings_job,
                    console_actions_job,
                    asyncio.to_thread(self._remote_app_runtime_summary, node, app_name, user),
                    asyncio.to_thread(self._remote_node_system_summary, node, user),
                )
                model = self._remote_overview_page_model(
                    node=node,
                    app_name=app_entry.name,
                    app_friendly=app_entry.friendly,
                    app_color_hex=app_entry.color_hex,
                    supports_configs=app_entry.supports_configs,
                    config_read_level=app_entry.config_read_level,
                    config_write_level=app_entry.config_write_level,
                    supports_save_uploads=app_entry.supports_save_uploads,
                    supports_save_rename=app_entry.supports_save_rename,
                    save_write_level=app_entry.save_write_level,
                    configs=configs,
                    saves=saves,
                    settings=settings,
                    console_actions=console_actions,
                    supports_chat=app_entry.supports_chat,
                    chat_url=(
                        self.node_app_chat_path(node.node_name, app_entry.name) if app_entry.supports_chat else None
                    ),
                    app_stats=app_stats,
                    app_start_blocked=self._app_start_blocked_remote(
                        app_friendly=app_entry.friendly,
                        app_stats=app_stats,
                        running_names=system_summary.running_names,
                    ),
                )

                async def _refresh_title_stats() -> tuple[ModWebTitleStat, ...]:
                    latest = await asyncio.to_thread(self._remote_app_runtime_summary, node, app_name, user)
                    return self._build_app_title_stats(latest)

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
                    refresh_async_title_stats=_refresh_title_stats,
                    refresh_async_runtime_model=_refresh_runtime_model,
                    subscribe_app_state_updates=_subscribe_app_state,
                    initial_system_summary=system_summary,
                )
        except Exception as xcp:
            log.exception("Remote mod web app page render failed: node=%s app=%s", node_name, app_name)
            self._render_error_page(ui=ui, title="Page unavailable", detail=str(xcp), app_name=app_name)
