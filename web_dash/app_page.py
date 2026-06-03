from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import (
    _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
    _APP_SECTION_QUERY_PARAM,
    _DOWNLOAD_FEEDBACK_DELAY_SECONDS,
    log,
)
from .nicegui_protocols import (
    AsyncRefresh,
    ModWebUi,
    ModWebValueContainer,
    _value_as_object,
    _value_as_text,
)
from .runtime_imports import (
    AbstractEventLoop,
    App,
    Awaitable,
    Button,
    Callable,
    Checkbox,
    Html,
    Label,
    Literal,
    LiteralString,
    ModWebUser,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeConsoleActionList,
    NodeAppTransitionState,
    NodeModSummary,
    NodeModUploadResult,
    NodeSaveList,
    NodeSettingList,
    NodeSystemSummary,
    Power_Level,
    Timer,
    Upload,
    assert_never,
    asyncio,
    json,
    parse_qsl,
    replace,
    required_app_mutation_level,
    urlencode,
    urlsplit,
)
from .service_base import ModWebServiceSupport
from .types import (
    ModDownloadKind,
    ModWebAppSectionKind,
    ModWebAppTabDefinition,
    ModWebBasePageModel,
    ModWebOverviewPageModel,
    ModWebPageModel,
    _ModWebAppHeroRuntimeDetails,
    _ModWebBadgeSpec,
    _ModWebChatSurfaceConfig,
    _ModWebKillControlState,
    _ModWebModToolbarBindings,
    _ModWebRuntimeToolbarBindings,
    _ModWebStartStopControlState,
    _ModWebTabActionSpec,
)
from .ui_helpers import ModWebUiHelpersMixin

if TYPE_CHECKING:
    from nicegui.element import Element
    from nicegui.elements.tabs import Tab
    from nicegui.elements.tooltip import Tooltip
    from nicegui.events import UploadEventArguments

class ModWebAppPageMixin(ModWebServiceSupport):
    def _apply_live_app_state_update(
        self,
        *,
        model: ModWebBasePageModel,
        event: NodeAppStateStreamEvent,
        local_app: App | None,
        last_system_summary: NodeSystemSummary | None,
    ) -> tuple[ModWebBasePageModel, NodeSystemSummary | None]:
        next_app_stats: NodeAppRuntimeSummary | None = self._merged_runtime_summary(
            previous=model.app_stats, updated=event.app_stats
        )
        next_system_summary: NodeSystemSummary | None = (
            event.system_summary if event.system_summary is not None else last_system_summary
        )
        if local_app is not None:
            app_start_blocked = self._app_start_blocked_local(local_app)
        else:
            app_start_blocked: bool = self._app_start_blocked_remote(
                app_friendly=model.app_friendly,
                app_stats=next_app_stats,
                running_names=() if next_system_summary is None else next_system_summary.running_names,
            )
        return (
            self._model_with_runtime_state(
                model,
                app_stats=next_app_stats,
                app_start_blocked=app_start_blocked,
            ),
            next_system_summary,
        )

    @staticmethod
    def _merged_runtime_summary(
        *,
        previous: NodeAppRuntimeSummary | None,
        updated: NodeAppRuntimeSummary | None,
    ) -> NodeAppRuntimeSummary | None:
        if updated is None:
            return previous
        if previous is None:
            return updated
        return replace(
            updated,
            storage_percent=updated.storage_percent
            if updated.storage_percent is not None
            else previous.storage_percent,
            storage_free_bytes=(
                updated.storage_free_bytes if updated.storage_free_bytes is not None else previous.storage_free_bytes
            ),
            storage_total_bytes=(
                updated.storage_total_bytes if updated.storage_total_bytes is not None else previous.storage_total_bytes
            ),
            footprint_bytes=updated.footprint_bytes
            if updated.footprint_bytes is not None
            else previous.footprint_bytes,
        )

    def _render_page(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
        current_url: str,
        refresh_async_app_stats: Callable[[], Awaitable[NodeAppRuntimeSummary | None]] | None = None,
        refresh_async_runtime_model: Callable[[], Awaitable[ModWebBasePageModel]] | None = None,
        subscribe_app_state_updates: Callable[
            [Callable[[NodeAppStateStreamEvent], None]],
            Callable[[], None],
        ]
        | None = None,
        local_app: App | None = None,
        initial_system_summary: NodeSystemSummary | None = None,
        chat_surface: _ModWebChatSurfaceConfig | None = None,
    ) -> None:
        if model.supports_chat != (chat_surface is not None):
            raise ValueError("App page chat support and chat surface configuration are out of sync.")
        self._apply_theme(ui=ui)
        current_model: ModWebBasePageModel = model
        last_system_summary: NodeSystemSummary | None = initial_system_summary
        with ui.column().classes(self._app_page_classes()):
            self._render_user_header(ui=ui, user=user)
            with ui.card().classes(self._hero_card_classes()).style(self._hero_card_style(model.app_color_hex)):
                self._render_app_node_badge(ui=ui, node_name=model.node_name)
                with ui.column().classes(self._app_page_hero_shell_classes()):
                    apply_app_hero_runtime: Callable[[NodeAppRuntimeSummary | None], None] = (
                        self._render_live_app_hero_runtime(
                            ui=ui,
                            title=model.app_friendly,
                            static_badges=self._app_page_hero_badges(model),
                            initial_app_stats=model.app_stats,
                            refresh_async_app_stats=refresh_async_app_stats,
                        )
                    )
                    toolbar_bindings: _ModWebRuntimeToolbarBindings = self._render_global_app_toolbar(
                        ui=ui,
                        model=model,
                        user=user,
                        refresh_async_runtime_model=refresh_async_runtime_model,
                    )
            tabs: tuple[ModWebAppTabDefinition, ...] = self._page_tabs(model)
            apply_section_runtime_model: Callable[[ModWebBasePageModel], None] | None = (
                self._render_tabbed_page_sections(
                    ui=ui,
                    model=model,
                    user=user,
                    current_url=current_url,
                    tabs=tabs,
                    chat_surface=chat_surface,
                )
            )
            if subscribe_app_state_updates is not None:
                page_closed = False
                loop: AbstractEventLoop = asyncio.get_running_loop()

                def _apply_update(event: NodeAppStateStreamEvent) -> None:
                    nonlocal current_model, last_system_summary
                    if page_closed:
                        return
                    current_model, last_system_summary = self._apply_live_app_state_update(
                        model=current_model,
                        event=event,
                        local_app=local_app,
                        last_system_summary=last_system_summary,
                    )
                    apply_app_hero_runtime(current_model.app_stats)
                    if toolbar_bindings.apply_runtime_model is not None:
                        toolbar_bindings.apply_runtime_model(current_model)
                    if apply_section_runtime_model is not None:
                        apply_section_runtime_model(current_model)

                def _handle_update(event: NodeAppStateStreamEvent) -> None:
                    loop.call_soon_threadsafe(lambda: _apply_update(event))

                unsubscribe: Callable[[], None] = subscribe_app_state_updates(_handle_update)

                def _cleanup_live_updates() -> None:
                    nonlocal page_closed
                    page_closed = True
                    unsubscribe()

                self._register_client_cleanup(ui=ui, cleanup=_cleanup_live_updates)

    def _render_overview_page(
        self,
        *,
        ui: ModWebUi,
        model: ModWebOverviewPageModel,
        user: ModWebUser,
        current_url: str,
        refresh_async_app_stats: Callable[[], Awaitable[NodeAppRuntimeSummary | None]] | None = None,
        refresh_async_runtime_model: Callable[[], Awaitable[ModWebBasePageModel]] | None = None,
        subscribe_app_state_updates: Callable[
            [Callable[[NodeAppStateStreamEvent], None]],
            Callable[[], None],
        ]
        | None = None,
        local_app: App | None = None,
        initial_system_summary: NodeSystemSummary | None = None,
        chat_surface: _ModWebChatSurfaceConfig | None = None,
    ) -> None:
        if model.supports_chat != (chat_surface is not None):
            raise ValueError("Overview page chat support and chat surface configuration are out of sync.")
        self._apply_theme(ui=ui)
        current_model: ModWebBasePageModel = model
        last_system_summary: NodeSystemSummary | None = initial_system_summary
        with ui.column().classes(self._app_page_classes()):
            self._render_user_header(ui=ui, user=user)
            with ui.card().classes(self._hero_card_classes()).style(self._hero_card_style(model.app_color_hex)):
                self._render_app_node_badge(ui=ui, node_name=model.node_name)
                with ui.column().classes(self._app_page_hero_shell_classes()):
                    apply_app_hero_runtime: Callable[[NodeAppRuntimeSummary | None], None] = (
                        self._render_live_app_hero_runtime(
                            ui=ui,
                            title=model.app_friendly,
                            static_badges=self._app_page_hero_badges(model),
                            initial_app_stats=model.app_stats,
                            refresh_async_app_stats=refresh_async_app_stats,
                        )
                    )
                    toolbar_bindings: _ModWebRuntimeToolbarBindings = self._render_global_app_toolbar(
                        ui=ui,
                        model=model,
                        user=user,
                        refresh_async_runtime_model=refresh_async_runtime_model,
                    )
            tabs: tuple[ModWebAppTabDefinition, ...] = self._page_tabs(model)
            if not tabs:
                apply_section_runtime_model = None
            else:
                apply_section_runtime_model: Callable[[ModWebBasePageModel], None] | None = (
                    self._render_tabbed_page_sections(
                        ui=ui,
                        model=model,
                        user=user,
                        current_url=current_url,
                        tabs=tabs,
                        chat_surface=chat_surface,
                    )
                )
            if subscribe_app_state_updates is not None:
                page_closed = False
                loop: AbstractEventLoop = asyncio.get_running_loop()

                def _apply_update(event: NodeAppStateStreamEvent) -> None:
                    nonlocal current_model, last_system_summary
                    if page_closed:
                        return
                    current_model, last_system_summary = self._apply_live_app_state_update(
                        model=current_model,
                        event=event,
                        local_app=local_app,
                        last_system_summary=last_system_summary,
                    )
                    apply_app_hero_runtime(current_model.app_stats)
                    if toolbar_bindings.apply_runtime_model is not None:
                        toolbar_bindings.apply_runtime_model(current_model)
                    if apply_section_runtime_model is not None:
                        apply_section_runtime_model(current_model)

                def _handle_update(event: NodeAppStateStreamEvent) -> None:
                    loop.call_soon_threadsafe(lambda: _apply_update(event))

                unsubscribe: Callable[[], None] = subscribe_app_state_updates(_handle_update)

                def _cleanup_live_updates() -> None:
                    nonlocal page_closed
                    page_closed = True
                    unsubscribe()

                self._register_client_cleanup(ui=ui, cleanup=_cleanup_live_updates)
            if not tabs:
                with ui.card().classes("mod-card mod-card-empty w-full"):
                    ui.label(
                        "This app does not currently expose mods, chat, saves, settings, console actions, or config files through mod web."
                    ).classes("p-8 text-lg mod-subtitle")
                return

    @classmethod
    def _app_hero_runtime_details(cls, app_stats: NodeAppRuntimeSummary | None) -> _ModWebAppHeroRuntimeDetails:
        if app_stats is None:
            relay_badge = _ModWebBadgeSpec(text="Unknown", tone="grey")
            version_badge = _ModWebBadgeSpec(text="Unknown", tone="black")
            storage_badge = _ModWebBadgeSpec(text="Unavailable", tone="grey")
            return _ModWebAppHeroRuntimeDetails(
                status_text="Unknown",
                status_tone="grey",
                badges=(relay_badge, version_badge, storage_badge),
            )

        if app_stats.transition_state is NodeAppTransitionState.STOPPING:
            status_text = "Stopping"
            status_tone: Literal["warn"] | Literal["purple"] | Literal["grey"] | Literal["red"] = "warn"
        elif app_stats.transition_state is NodeAppTransitionState.STARTING:
            status_text = "Starting"
            status_tone = "purple"
        elif app_stats.running:
            status_text = "Running"
            status_tone = "purple"
        elif app_stats.enabled:
            status_text = "Stopped"
            status_tone = "warn"
        else:
            status_text = "Disabled"
            status_tone = "red"

        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(text=f"{app_stats.relay_support.display_value}", tone="grey"),
            _ModWebBadgeSpec(text=f"{app_stats.version or 'Unknown'}", tone="black"),
            _ModWebBadgeSpec(
                text=(
                    f"{cls._app_footprint_value(app_stats.footprint_bytes)}"
                    if app_stats.footprint_bytes is not None
                    else "Unavailable"
                ),
                tone="grey",
            ),
        ]
        if app_stats.player_count is not None and app_stats.player_capacity is not None:
            player_tone: Literal["purple"] | Literal["grey"] = "purple" if app_stats.player_count > 0 else "grey"
            badges.append(
                _ModWebBadgeSpec(
                    text=f"{app_stats.player_count} / {app_stats.player_capacity}",
                    tone=player_tone,
                )
            )
        return _ModWebAppHeroRuntimeDetails(
            status_text=status_text,
            status_tone=status_tone,
            badges=tuple[_ModWebBadgeSpec, ...](badges),
        )

    def _app_page_hero_badges(self, model: ModWebBasePageModel) -> tuple[_ModWebBadgeSpec, ...]:
        badges: list[_ModWebBadgeSpec] = []
        if isinstance(model, ModWebPageModel):
            badges.append(self._app_page_hero_mod_badge(model.mods.summary))
            if model.supports_configs:
                badges.append(_ModWebBadgeSpec(text=f"{len(model.configs.configs)} configs", tone="grey"))
        else:
            badges.append(_ModWebBadgeSpec(text="No mod index", tone="grey"))
            if model.supports_configs:
                badges.append(_ModWebBadgeSpec(text=f"{len(model.configs.configs)} configs", tone="black"))
        if model.saves is not None:
            badges.append(_ModWebBadgeSpec(text=f"{len(model.saves.saves)} saves", tone="black"))
        if model.settings is not None:
            badges.append(_ModWebBadgeSpec(text=f"{len(model.settings.settings)} settings", tone="black"))
        if model.console_actions is not None and model.console_actions.actions:
            badges.append(
                _ModWebBadgeSpec(
                    text=self._console_action_count_badge_text(action_count=len(model.console_actions.actions)),
                    tone="black",
                )
            )
        return tuple(badges)

    @staticmethod
    def _app_page_hero_mod_badge(summary: NodeModSummary) -> _ModWebBadgeSpec:
        total_count: int = summary.total_count
        enabled_count: int = summary.enabled_count
        if total_count == 1 and enabled_count == total_count:
            text = "1 Mod"
        elif enabled_count != total_count:
            text = f"{enabled_count}/{total_count} Mods"
        elif total_count == 1:
            text = "1 Mod"
        else:
            text = f"{total_count} Mods"
        return _ModWebBadgeSpec(text=text, tone="black")

    def _render_live_app_hero_runtime(
        self,
        *,
        ui: ModWebUi,
        title: str,
        static_badges: tuple[_ModWebBadgeSpec, ...],
        initial_app_stats: NodeAppRuntimeSummary | None,
        refresh_async_app_stats: Callable[[], Awaitable[NodeAppRuntimeSummary | None]] | None = None,
    ) -> Callable[[NodeAppRuntimeSummary | None], None]:
        @ui.refreshable
        def _render_runtime(app_stats: NodeAppRuntimeSummary | None) -> None:
            runtime_details = self._app_hero_runtime_details(app_stats)
            with ui.column().classes("w-full gap-3"):
                with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                    with ui.column().classes(self._hero_header_main_classes()):
                        ui.label(title).classes(self._hero_title_classes())
                    with ui.column().classes("mod-app-hero-status gap-1"):
                        ui.label("Status").classes("mod-app-hero-status-label")
                        ui.label(runtime_details.status_text).classes(
                            f"mod-app-hero-status-value mod-app-hero-status-value-{runtime_details.status_tone}"
                        )
                with ui.row().classes(f"{self._hero_badge_row_classes(fill=True)} w-full"):
                    for badge in runtime_details.badges:
                        self._badge(ui=ui, text=badge.text, tone=badge.tone)
                    for badge in static_badges:
                        self._badge(ui=ui, text=badge.text, tone=badge.tone)

        _render_runtime(initial_app_stats)
        if refresh_async_app_stats is None:
            return _render_runtime.refresh
        refresh_async: AsyncRefresh = self._build_async_refreshable_updater(
            refresh_async_value=refresh_async_app_stats,
            apply_value=_render_runtime.refresh,
            error_context="Mod web app hero runtime",
        )
        refresh_timer: Timer = ui.timer(
            _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
            lambda: asyncio.create_task(refresh_async()),
        )
        self._register_timer_cleanup(ui=ui, timer=refresh_timer)
        return _render_runtime.refresh

    def _page_section_badges(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        if tab.builtin_kind is None:
            if tab.badge_handler_name is None:
                return ()
            badge_handler = getattr(self, tab.badge_handler_name, None)
            if badge_handler is None:
                raise ValueError(f"Unknown app tab badge handler: {tab.badge_handler_name}")
            return badge_handler(model=model, user=user, tab=tab)

        match tab.builtin_kind:
            case ModWebAppSectionKind.MODS:
                if not isinstance(model, ModWebPageModel):
                    raise TypeError("The Mods section requires a full mod page model.")
                return self._mods_header_badges(model.mods.summary)
            case ModWebAppSectionKind.CONFIGS:
                return self._config_section_badges(model=model, user=user)
            case ModWebAppSectionKind.SETTINGS:
                return self._settings_section_badges(model=model)
            case ModWebAppSectionKind.SAVES:
                return self._save_section_badges(model=model, user=user)
            case ModWebAppSectionKind.CONSOLE:
                return self._console_section_badges(model=model)
            case ModWebAppSectionKind.CHAT:
                return ()
            case _:
                assert_never(tab.builtin_kind)

    def _page_tab_actions(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
        chat_surface: _ModWebChatSurfaceConfig | None,
    ) -> tuple[_ModWebTabActionSpec, ...]:
        if tab.builtin_kind is ModWebAppSectionKind.CHAT:
            if chat_surface is None or chat_surface.popout_url is None:
                return ()
            return (
                _ModWebTabActionSpec(
                    label="Pop Out",
                    url=chat_surface.popout_url,
                    new_tab=True,
                    extra_classes="mod-action-border-accent",
                ),
            )
        if tab.action_handler_name is None:
            return ()
        action_handler = getattr(self, tab.action_handler_name, None)
        if action_handler is None:
            raise ValueError(f"Unknown app tab action handler: {tab.action_handler_name}")
        return action_handler(model=model, user=user, tab=tab)

    def _config_section_badges(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        can_read: bool = self._user_has_level(user, model.config_read_level)
        can_write: bool = self._user_has_level(user, model.config_write_level)
        if not can_read:
            return (_ModWebBadgeSpec(text="Locked", tone="grey"),)
        return (
            _ModWebBadgeSpec(text=f"{len(model.configs.configs)} files", tone="grey"),
            _ModWebBadgeSpec(
                text=f"{model.config_write_level.name.title()} write" if can_write else "Read only",
                tone="red" if can_write else "grey",
            ),
        )

    @staticmethod
    def _settings_section_badges(*, model: ModWebBasePageModel) -> tuple[_ModWebBadgeSpec, ...]:
        settings: NodeSettingList | None = model.settings
        if settings is None:
            return ()
        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(
                text=f"{len(settings.settings)} settings",
                tone="black" if settings.settings else "grey",
            ),
        ]
        if settings.settings:
            badges.append(_ModWebBadgeSpec(text=f"{settings.editable_count} editable", tone="purple"))
        if settings.pending_change_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{settings.pending_change_count} drafts", tone="grey"))
        if settings.restricted_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{settings.restricted_count} restricted", tone="warn"))
        return tuple[_ModWebBadgeSpec, ...](badges)

    def _save_section_badges(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        saves: NodeSaveList | None = model.saves
        if saves is None:
            return ()
        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(
                text=f"{len(saves.saves)} saves",
                tone="black" if saves.saves else "grey",
            ),
        ]
        directory_count: int = sum(1 for save in saves.saves if save.kind == "directory")
        if directory_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{directory_count} folders", tone="grey"))
        if model.supports_save_uploads:
            badges.append(_ModWebBadgeSpec(text="Upload", tone="purple"))
        if model.supports_save_rename:
            badges.append(_ModWebBadgeSpec(text="Rename", tone="grey"))
        if (model.supports_save_uploads or model.supports_save_rename) and not self._user_has_level(
            user, model.save_write_level
        ):
            badges.append(_ModWebBadgeSpec(text=f"{model.save_write_level.name.title()} write", tone="warn"))
        return tuple[_ModWebBadgeSpec, ...](badges)

    @staticmethod
    def _console_section_badges(*, model: ModWebBasePageModel) -> tuple[_ModWebBadgeSpec, ...]:
        console_actions: NodeConsoleActionList | None = model.console_actions
        if console_actions is None:
            return ()
        action_count: int = len(console_actions.actions)
        return (
            _ModWebBadgeSpec(
                text=f"{action_count} actions",
                tone="black" if action_count > 0 else "grey",
            ),
        )

    @staticmethod
    def _section_badge_columns(
        badges: tuple[_ModWebBadgeSpec, ...],
        *,
        rows_per_column: int = 2,
    ) -> tuple[tuple[_ModWebBadgeSpec, ...], ...]:
        if rows_per_column < 1:
            raise ValueError("Section badge columns require at least one row per column.")
        return tuple(
            tuple[_ModWebBadgeSpec, ...](badges[index : index + rows_per_column])
            for index in range(0, len(badges), rows_per_column)
        )

    @classmethod
    def _initial_page_tab_id(
        cls,
        *,
        current_url: str,
        tabs: tuple[ModWebAppTabDefinition, ...],
    ) -> str:
        if not tabs:
            raise ValueError("Page sections are required to resolve an initial app tab.")

        query_by_key: dict[str, str] = {
            key: value for key, value in parse_qsl(urlsplit(current_url).query, keep_blank_values=True)
        }
        raw_tab_id: str | None = query_by_key.get(_APP_SECTION_QUERY_PARAM)
        if raw_tab_id is not None:
            requested_tab_id: str = raw_tab_id.strip().casefold()
            for tab in tabs:
                if tab.tab_id.casefold() == requested_tab_id:
                    return tab.tab_id
        return tabs[0].tab_id

    @staticmethod
    def _page_tab_url(current_url: str, *, tab_id: str) -> str:
        return ModWebUiHelpersMixin._request_url_with_query_values(
            current_url,
            param_name=_APP_SECTION_QUERY_PARAM,
            values=(tab_id,),
        )

    @staticmethod
    def _replace_browser_url(*, ui: ModWebUi, target_url: str) -> None:
        encoded_url: str = json.dumps(target_url)
        ui.run_javascript(f"window.history.replaceState(window.history.state, '', {encoded_url});")

    def _render_tabbed_page_sections(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        current_url: str,
        tabs: tuple[ModWebAppTabDefinition, ...],
        chat_surface: _ModWebChatSurfaceConfig | None,
    ) -> Callable[[ModWebBasePageModel], None] | None:
        if not tabs:
            return None

        initial_tab_id: str = self._initial_page_tab_id(current_url=current_url, tabs=tabs)
        current_section_url: str = self._page_tab_url(current_url, tab_id=initial_tab_id)
        tab_by_id: dict[str, Tab] = {}
        section_chrome_by_tab_id: dict[str, "Element"] = {}
        section_runtime_appliers: list[Callable[[ModWebBasePageModel], None]] = []
        chat_endpoint_count_label: Label | None = None
        chat_endpoint_tooltip: Tooltip | None = None
        chat_endpoint_tooltip_content: Html | None = None

        def set_section_chrome_visibility(tab_id: str) -> None:
            for chrome_tab_id, chrome in section_chrome_by_tab_id.items():
                if chrome_tab_id == tab_id:
                    chrome.style(remove="display: none;")
                else:
                    chrome.style(add="display: none;")

        def sync_section_url(event: ModWebValueContainer) -> None:
            nonlocal current_section_url
            next_tab_id: str = _value_as_text(event).strip()
            if next_tab_id not in tab_by_id:
                return
            current_section_url = self._page_tab_url(current_section_url, tab_id=next_tab_id)
            self._replace_browser_url(ui=ui, target_url=current_section_url)
            set_section_chrome_visibility(next_tab_id)

        with ui.column().classes("w-full gap-3"):
            with ui.row().classes("mod-section-strip w-full items-start justify-between gap-3 flex-wrap"):
                with ui.element("div").classes("mod-section-tabs-shell"):
                    with ui.tabs(value=initial_tab_id, on_change=sync_section_url).classes("mod-section-tabs") as section_tabs:
                        for tab in tabs:
                            tab_by_id[tab.tab_id] = ui.tab(tab.tab_id, label=tab.label)
                with ui.row().classes("mod-section-chrome items-start justify-end gap-3 flex-wrap"):
                    for tab in tabs:
                        section_actions: tuple[_ModWebTabActionSpec, ...] = self._page_tab_actions(
                            model=model,
                            user=user,
                            tab=tab,
                            chat_surface=chat_surface,
                        )
                        if tab.builtin_kind is ModWebAppSectionKind.CHAT:
                            if chat_surface is None:
                                raise ValueError("The Chat tab requires a chat surface configuration.")
                            with (
                                ui.row()
                                .classes(
                                    "mod-section-chrome-panel mod-section-chrome-chat items-start justify-end gap-3 flex-wrap"
                                )
                            ) as chat_section_chrome:
                                section_chrome_by_tab_id[tab.tab_id] = chat_section_chrome
                                with ui.column().classes("mod-section-chrome-badge-stack items-end gap-1"):
                                    with ui.row().classes("mod-section-chrome-badge-row items-start justify-end gap-2"):
                                        with ui.column().classes("mod-section-chrome-badge-column items-end gap-1"):
                                            (
                                                chat_endpoint_count_label,
                                                chat_endpoint_tooltip,
                                                chat_endpoint_tooltip_content,
                                            ) = self._render_chat_endpoint_badge(
                                                ui=ui,
                                                snapshot=chat_surface.panel.initial_snapshot,
                                            )
                                if section_actions:
                                    with ui.row().classes("mod-section-chrome-actions items-center justify-end gap-2"):
                                        for action in section_actions:
                                            self._action_link(
                                                ui=ui,
                                                label=action.label,
                                                url=action.url,
                                                compact=True,
                                                extra_classes=action.extra_classes,
                                                new_tab=action.new_tab,
                                            )
                            continue
                        section_badges: tuple[_ModWebBadgeSpec, ...] = self._page_section_badges(
                            model=model,
                            user=user,
                            tab=tab,
                        )
                        if not section_badges and not section_actions:
                            continue
                        with ui.row().classes(
                            "mod-section-chrome-panel items-start justify-end gap-3 flex-wrap"
                        ) as section_chrome:
                            section_chrome_by_tab_id[tab.tab_id] = section_chrome
                            if section_badges:
                                with ui.column().classes("mod-section-chrome-badge-stack items-end gap-1"):
                                    with ui.row().classes("mod-section-chrome-badge-row items-start justify-end gap-2"):
                                        for badge_column in self._section_badge_columns(section_badges):
                                            with ui.column().classes("mod-section-chrome-badge-column items-end gap-1"):
                                                for badge in badge_column:
                                                    self._badge(ui=ui, text=badge.text, tone=badge.tone)
                            if section_actions:
                                with ui.row().classes("mod-section-chrome-actions items-center justify-end gap-2"):
                                    for action in section_actions:
                                        self._action_link(
                                            ui=ui,
                                            label=action.label,
                                            url=action.url,
                                            compact=True,
                                            extra_classes=action.extra_classes,
                                            new_tab=action.new_tab,
                                        )
            with ui.tab_panels(
                section_tabs,
                value=initial_tab_id,
                animated=False,
            ).classes("mod-section-panels w-full"):
                for tab in tabs:
                    with ui.tab_panel(tab_by_id[tab.tab_id]).classes("mod-section-panel w-full"):
                        section_runtime_model = self._render_page_section(
                            ui=ui,
                            model=model,
                            user=user,
                            tab=tab,
                            chat_surface=chat_surface,
                            chat_endpoint_count_label=chat_endpoint_count_label,
                            chat_endpoint_tooltip=chat_endpoint_tooltip,
                            chat_endpoint_tooltip_content=chat_endpoint_tooltip_content,
                        )
                        if section_runtime_model is not None:
                            section_runtime_appliers.append(section_runtime_model)
            if section_chrome_by_tab_id:
                set_section_chrome_visibility(initial_tab_id)
        if current_url != current_section_url:
            self._replace_browser_url(ui=ui, target_url=current_section_url)
        if not section_runtime_appliers:
            return None

        def apply_section_runtime_model(runtime_model: ModWebBasePageModel) -> None:
            for apply_runtime_model in section_runtime_appliers:
                apply_runtime_model(runtime_model)

        return apply_section_runtime_model

    def _render_page_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
        chat_surface: _ModWebChatSurfaceConfig | None,
        chat_endpoint_count_label: Label | None = None,
        chat_endpoint_tooltip: Tooltip | None = None,
        chat_endpoint_tooltip_content: Html | None = None,
    ) -> Callable[[ModWebBasePageModel], None] | None:
        if tab.builtin_kind is None:
            if tab.render_handler_name is None:
                raise ValueError(f"Custom app tab {tab.tab_id} is missing its render handler.")
            render_handler = getattr(self, tab.render_handler_name, None)
            if render_handler is None:
                raise ValueError(f"Unknown app tab render handler: {tab.render_handler_name}")
            return render_handler(ui=ui, model=model, user=user, tab=tab)

        match tab.builtin_kind:
            case ModWebAppSectionKind.MODS:
                if not isinstance(model, ModWebPageModel):
                    raise TypeError("The Mods section requires a full mod page model.")
                self._render_mods_section(ui=ui, model=model, user=user)
                return None
            case ModWebAppSectionKind.CONFIGS:
                self._render_config_editor(ui=ui, model=model, user=user)
                return None
            case ModWebAppSectionKind.SETTINGS:
                self._render_settings_editor(ui=ui, model=model, user=user)
                return None
            case ModWebAppSectionKind.SAVES:
                self._render_saves_editor(ui=ui, model=model, user=user)
                return None
            case ModWebAppSectionKind.CONSOLE:
                return self._render_console_editor(ui=ui, model=model, user=user)
            case ModWebAppSectionKind.CHAT:
                if chat_surface is None:
                    raise ValueError("The Chat section requires a chat surface configuration.")
                return self._render_chat_section(
                    ui=ui,
                    chat_surface=chat_surface,
                    endpoint_count_label=chat_endpoint_count_label,
                    endpoint_count_tooltip=chat_endpoint_tooltip,
                    endpoint_count_tooltip_content=chat_endpoint_tooltip_content,
                )
            case _:
                assert_never(tab.builtin_kind)

    def _render_mods_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
    ) -> None:
        selected_mod_names: set[str] = set[str]()
        checkboxes: dict[str, Checkbox] = {}
        downloadable_names: tuple[str, ...] = tuple[str, ...](
            entry.name for entry in model.mods.mods if entry.downloadable
        )
        downloadable_count: int = model.mods.summary.downloadable_count
        can_upload_mod: bool = self._user_has_level(user, Power_Level.user)
        selection_button = None
        download_button = None

        def selected_mod_names_in_page_order() -> tuple[str, ...]:
            return tuple[str, ...](entry.name for entry in model.mods.mods if entry.name in selected_mod_names)

        def update_count() -> None:
            if selection_button is None or download_button is None:
                return
            selected_count: int = len(selected_mod_names)
            selection_button.set_text(self._selection_toggle_label(selected_count=selected_count))
            download_button.set_text(
                self._download_selection_label(
                    selected_count=selected_count,
                    downloadable_count=downloadable_count,
                )
            )
            can_download: bool = downloadable_count > 0 and bool(model.mods.mods)
            selection_button.set_enabled(can_download)
            download_button.set_enabled(can_download)

        def set_selected(mod_name: str, selected: bool) -> None:
            if selected:
                selected_mod_names.add(mod_name)
            else:
                selected_mod_names.discard(mod_name)
            update_count()

        def select_all() -> None:
            selected_mod_names.update(downloadable_names)
            for checkbox in checkboxes.values():
                checkbox.set_value(True)
            update_count()

        def clear_selection() -> None:
            selected_mod_names.clear()
            for checkbox in checkboxes.values():
                checkbox.set_value(False)
            update_count()

        def toggle_selection() -> None:
            if selected_mod_names:
                clear_selection()
            else:
                select_all()

        async def download_selected() -> None:
            mod_names: tuple[str, ...] = selected_mod_names_in_page_order()
            if mod_names:
                query: str = urlencode({"selected_only": "true", "mod_name": list(mod_names)}, doseq=True)
                await self._start_download(
                    ui=ui,
                    url=f"{self._download_base_url(model)}?{query}",
                    message=self._download_feedback_message(
                        kind=ModDownloadKind.SELECTED,
                        app_friendly=model.app_friendly,
                        selected_count=len(mod_names),
                    ),
                )
                return
            await self._start_download(
                ui=ui,
                url=model.download_all_url,
                message=self._download_feedback_message(
                    kind=ModDownloadKind.ALL,
                    app_friendly=model.app_friendly,
                ),
            )

        async def upload_mod(event: "UploadEventArguments") -> None:
            if upload_status_label is not None:
                upload_status_label.set_text(f"Installing {event.file.name}...")
            if upload_control is not None:
                upload_control.disable()
            ui.notify(f"Uploading {event.file.name} to {model.app_friendly}.", type="info")
            try:
                result: NodeModUploadResult = await self._upload_mod(model=model, upload_file=event.file, user=user)
            except Exception as xcp:
                if upload_status_label is not None:
                    upload_status_label.set_text("Upload failed. Choose a file to try again.")
                if upload_control is not None:
                    upload_control.enable()
                ui.notify(f"Mod upload failed: {xcp}", type="negative")
                return
            upload_dialog.close()
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        upload_control: Upload | None = None
        upload_status_label: Label | None = None
        with ui.dialog() as upload_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Upload Mod").classes("text-xl font-black mod-title-small")
                        ui.label(f"Install a mod file for {model.app_friendly}.").classes("mod-subtitle text-sm")
                    upload_control = ui.upload(
                        label="Choose Mod File",
                        auto_upload=True,
                        on_upload=upload_mod,
                    ).classes("mod-list-button")
                    upload_status_label = ui.label(
                        "The app must be stopped before mods can be changed."
                    ).classes("mod-subtitle text-sm")
                    with ui.row().classes("w-full justify-end"):
                        ui.button("Close", on_click=upload_dialog.close).classes("mod-list-button secondary")

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title="Mods",
                    description=self._mods_card_description(model.mods.summary),
                )
                toolbar_bindings: _ModWebModToolbarBindings = self._render_mod_toolbar(
                    ui=ui,
                    model=model,
                    user=user,
                    toggle_selection=toggle_selection,
                    download_selected=download_selected,
                    upload_mod=upload_dialog.open,
                )
                selection_button: Button | None = toolbar_bindings.selection_button
                download_button: Button | None = toolbar_bindings.download_button
                update_count()

                if not model.mods.mods:
                    ui.label("No mods are currently indexed for this app.").classes(
                        "mod-subtitle text-sm mod-tab-empty-detail"
                    )
                    if can_upload_mod:
                        ui.label("Upload a mod to seed this app.").classes("mod-subtitle text-sm mod-tab-empty-detail")
                    return

                with ui.column().classes("w-full mod-list"):

                    def _create_mod_selection_handler(mod_name: str) -> Callable[[ModWebValueContainer], None]:
                        def _handle_mod_selection_change(event: ModWebValueContainer) -> None:
                            set_selected(mod_name, bool(_value_as_object(event)))

                        return _handle_mod_selection_change

                    for entry in model.mods.mods:
                        checkbox: Checkbox | None = self._render_mod_download_row(
                            ui=ui,
                            entry=entry,
                            download_url=model.mod_download_urls.get(entry.name),
                            on_change=_create_mod_selection_handler(entry.name),
                            app_friendly=model.app_friendly,
                            model=model,
                            user=user,
                        )
                        if checkbox is not None:
                            checkboxes[entry.name] = checkbox
        return

    def _render_global_app_toolbar(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        refresh_async_runtime_model: Callable[[], Awaitable[ModWebBasePageModel]] | None = None,
    ) -> _ModWebRuntimeToolbarBindings:
        can_control_app_runtime: bool = self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.START)
        )
        can_kill_app_runtime: bool = self._user_has_level(user, required_app_mutation_level(NodeAppMutationAction.KILL))
        can_manage_app_state: bool = self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.ENABLE)
        )
        if not can_control_app_runtime and not can_kill_app_runtime and not can_manage_app_state:
            return _ModWebRuntimeToolbarBindings()

        start_stop_button: Button | None = None
        kill_button: Button | None = None
        current_runtime_model: ModWebBasePageModel = model
        start_stop_control_state: _ModWebStartStopControlState | None = None
        kill_control_state: _ModWebKillControlState | None = None

        def _set_button_disabled(*, button: Button, disabled: bool) -> None:
            if disabled:
                button.disable()
                return
            button.enable()

        def _apply_runtime_control_model(runtime_model: ModWebBasePageModel, *, force: bool = False) -> None:
            nonlocal current_runtime_model, start_stop_control_state, kill_control_state
            current_runtime_model = runtime_model
            if can_control_app_runtime and start_stop_button is not None:
                next_start_stop_state: _ModWebStartStopControlState = self._start_stop_control_state(runtime_model)
                if force or next_start_stop_state != start_stop_control_state:
                    start_stop_button.set_text(next_start_stop_state.label)
                    start_stop_button.classes(replace=next_start_stop_state.button_classes)
                    _set_button_disabled(button=start_stop_button, disabled=next_start_stop_state.disabled)
                    start_stop_control_state = next_start_stop_state
            if can_kill_app_runtime and kill_button is not None:
                next_kill_state: _ModWebKillControlState = self._kill_control_state(runtime_model)
                if force or next_kill_state != kill_control_state:
                    kill_button.set_text(next_kill_state.label)
                    kill_button.classes(replace="mod-list-button danger mod-toolbar-button")
                    _set_button_disabled(button=kill_button, disabled=next_kill_state.disabled)
                    kill_control_state = next_kill_state

        async def run_app_action(action: NodeAppMutationAction) -> None:
            pending_label: str | None = self._app_action_pending_label(action)
            pending_message: str | None = self._app_action_pending_message(action, model.app_friendly)
            if (
                start_stop_button is not None
                and action in {NodeAppMutationAction.START, NodeAppMutationAction.STOP}
                and pending_label is not None
            ):
                start_stop_button.set_text(pending_label)
                start_stop_button.disable()
            if kill_button is not None:
                if action is NodeAppMutationAction.START:
                    kill_button.enable()
                elif action is NodeAppMutationAction.KILL and pending_label is not None:
                    kill_button.set_text(pending_label)
                    kill_button.disable()
            if pending_message is not None:
                ui.notify(pending_message, type="info")
            try:
                result: NodeAppMutationResult = await self._mutate_app(model=model, action=action, user=user)
            except Exception as xcp:
                log.warning(
                    "App mutation failed: node=%s app=%s action=%s error=%s",
                    model.node_name,
                    model.app_name,
                    action.value,
                    xcp,
                )
                _apply_runtime_control_model(current_runtime_model, force=True)
                ui.notify(f"App action failed: {xcp}", type="negative")
                return
            ui.notify(result.message, type="positive")
            await asyncio.sleep(_DOWNLOAD_FEEDBACK_DELAY_SECONDS)
            if (
                action in {NodeAppMutationAction.START, NodeAppMutationAction.STOP, NodeAppMutationAction.KILL}
                and refresh_async_runtime_model is not None
            ):
                try:
                    _apply_runtime_control_model(await refresh_async_runtime_model(), force=True)
                except Exception as xcp:
                    log.warning(
                        "App runtime refresh failed after action: node=%s app=%s action=%s error=%s",
                        model.node_name,
                        model.app_name,
                        action.value,
                        xcp,
                    )
                return
            ui.navigate.reload()

        def _create_app_action_handler(
            action: NodeAppMutationAction,
        ) -> Callable[[object | None], Awaitable[None]]:
            async def _handle_app_action(_: object | None = None) -> None:
                await run_app_action(action)

            return _handle_app_action

        with ui.column().classes("mod-hero-toolbar w-full mod-select-form"):
            with ui.row().classes("mod-list-toolbar mod-hero-toolbar-surface w-full"):
                with ui.row().classes("mod-list-actions"):
                    if can_control_app_runtime:

                        async def _handle_start_stop_click(_: object | None = None) -> None:
                            current_action = (
                                None if start_stop_control_state is None else start_stop_control_state.action
                            )
                            if current_action is None:
                                return
                            await run_app_action(current_action)

                        start_stop_button = ui.button(
                            "",
                            on_click=_handle_start_stop_click,
                        ).classes("mod-list-button mod-toolbar-button")
                        _apply_runtime_control_model(model, force=True)
                    if can_kill_app_runtime:
                        kill_button = ui.button(
                            "",
                            on_click=_create_app_action_handler(NodeAppMutationAction.KILL),
                        ).classes("mod-list-button danger mod-toolbar-button")
                        _apply_runtime_control_model(model, force=True)
                    if can_manage_app_state:
                        enable_disable_action: NodeAppMutationAction = self._app_enable_disable_action(model)
                        ui.button(
                            self._app_enable_disable_label(model),
                            on_click=_create_app_action_handler(enable_disable_action),
                        ).classes(f"{self._app_enable_disable_button_classes(model)} mod-toolbar-button")
        if (can_control_app_runtime or can_kill_app_runtime) and refresh_async_runtime_model is not None:
            def apply_runtime_control_model(runtime_model: ModWebBasePageModel) -> None:
                _apply_runtime_control_model(runtime_model, force=False)

            refresh_runtime_control: AsyncRefresh = self._build_async_refreshable_updater(
                refresh_async_value=refresh_async_runtime_model,
                apply_value=apply_runtime_control_model,
                error_context="Mod web app runtime control",
            )
            refresh_runtime_control_timer: Timer = ui.timer(
                _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
                lambda: asyncio.create_task(refresh_runtime_control()),
            )
            self._register_timer_cleanup(ui=ui, timer=refresh_runtime_control_timer)
        return _ModWebRuntimeToolbarBindings(
            apply_runtime_model=(
                (lambda runtime_model: _apply_runtime_control_model(runtime_model, force=False))
                if (can_control_app_runtime or can_kill_app_runtime)
                else None
            ),
        )

    def _render_mod_toolbar(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
        toggle_selection: Callable[[], None],
        download_selected: Callable[[], Awaitable[None]],
        upload_mod: Callable[[], object] | None = None,
    ) -> _ModWebModToolbarBindings:
        can_upload_mod: bool = upload_mod is not None and self._user_has_level(user, Power_Level.user)
        show_bulk_mod_actions: bool = bool(model.mods.mods)
        if not can_upload_mod and not show_bulk_mod_actions:
            return _ModWebModToolbarBindings(selection_button=None, download_button=None)

        selection_button: Button | None = None
        download_button: Button | None = None

        with ui.row().classes("mod-tab-toolbar mod-mods-toolbar w-full"):
            with ui.row().classes("mod-tab-toolbar-actions mod-mods-toolbar-actions"):
                if can_upload_mod:
                    ui.button("Upload Mod", on_click=upload_mod).classes(
                        "mod-list-button mod-toolbar-button mod-toolbar-button-fill"
                    )
                if show_bulk_mod_actions:
                    selection_button = ui.button("", on_click=toggle_selection).classes(
                        "mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill"
                    )
                    download_button = ui.button("", on_click=download_selected).classes(
                        "mod-list-button mod-toolbar-button mod-toolbar-button-fill"
                    )
        return _ModWebModToolbarBindings(
            selection_button=selection_button,
            download_button=download_button,
        )

    @staticmethod
    def _flat_tab_card_classes(*, notepad: bool = False) -> str:
        classes = "mod-card mod-card-plain w-full"
        if notepad:
            classes: LiteralString = f"{classes} mod-card-notepad"
        return classes

    @staticmethod
    def _tab_section_body_classes() -> str:
        return "w-full gap-3"

    def _render_flat_tab_header(
        self,
        *,
        ui: ModWebUi,
        title: str,
        description: str | None,
        secondary_description: str | None = None,
    ) -> tuple[Label | None, Label | None]:
        del title
        with ui.column().classes("mod-tab-header w-full"):
            with ui.column().classes("mod-tab-header-main w-full"):
                description_label: Label | None = None
                secondary_label: Label | None = None
                if description is not None:
                    description_label = ui.label(description).classes("mod-subtitle text-sm w-full")
                if secondary_description is not None:
                    secondary_label = ui.label(secondary_description).classes("mod-subtitle text-xs w-full")
        return description_label, secondary_label

    def _render_flat_tab_empty_state(
        self,
        *,
        ui: ModWebUi,
        title: str,
        description: str,
        detail_text: str | None = None,
        secondary_description: str | None = None,
        notepad: bool = False,
    ) -> None:
        with ui.card().classes(self._flat_tab_card_classes(notepad=notepad)):
            with ui.column().classes(self._tab_section_body_classes()):
                self._render_flat_tab_header(
                    ui=ui,
                    title=title,
                    description=description,
                    secondary_description=secondary_description,
                )
                if detail_text is not None:
                    ui.label(detail_text).classes("mod-subtitle text-sm mod-tab-empty-detail")

    @staticmethod
    def _settings_card_description() -> str:
        return "Review, edit, reload, and save app settings."

    @staticmethod
    def _config_card_description() -> str:
        return "Browse, edit, reload, and download app config files."

    @staticmethod
    def _mods_card_description(summary: NodeModSummary) -> str:
        if summary.total_count == 0:
            return "No mods are currently indexed for this app."
        if summary.downloadable_count == 0:
            return "Browse the indexed mod inventory and inspect file details."
        if summary.non_downloadable_count == 0:
            return "Browse the indexed mod inventory, inspect details, and download any file."
        return "Browse the indexed mod inventory, inspect details, and download available files."

    @staticmethod
    def _mods_header_badges(summary: NodeModSummary) -> tuple[_ModWebBadgeSpec, ...]:
        mod_label: Literal["mod", "mods"] = "mod" if summary.total_count == 1 else "mods"
        coremod_label: Literal["coremod", "coremods"] = "coremod" if summary.coremod_count == 1 else "coremods"
        badges: list[_ModWebBadgeSpec] = [_ModWebBadgeSpec(text=f"{summary.total_count} {mod_label}", tone="black")]
        if summary.downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.downloadable_count} downloadable", tone="purple"))
        if summary.non_downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.non_downloadable_count} blocked", tone="warn"))
        if summary.coremod_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.coremod_count} {coremod_label}", tone="red"))
        return tuple[_ModWebBadgeSpec, ...](badges)
