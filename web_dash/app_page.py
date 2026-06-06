from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from .constants import (
    _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
    _APP_SECTION_QUERY_PARAM,
    _DOWNLOAD_FEEDBACK_DELAY_SECONDS,
    log,
)
from .nicegui_protocols import (
    AsyncRefresh,
    ModWebEventArgumentsContainer,
    ModWebUi,
    ModWebValueContainer,
    _event_args_as_text,
    _value_as_object,
    _value_as_text,
)
from .runtime_imports import (
    AbstractEventLoop,
    App,
    Awaitable,
    Button,
    Callable,
    Card,
    Checkbox,
    Html,
    Input,
    Label,
    Literal,
    LiteralString,
    ModWebUser,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeConsoleActionList,
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


_LEAFLET_VENDOR_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "resources" / "vendor" / "leaflet"


@lru_cache(maxsize=1)
def _leaflet_vendor_asset(file_name: str) -> str:
    return (_LEAFLET_VENDOR_DIRECTORY / file_name).read_text(encoding="utf-8")


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
                app_name=model.app_name,
                app_stats=next_app_stats,
                running_app_ids=() if next_system_summary is None else next_system_summary.running_app_ids,
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
            hero_card: Card = ui.card().classes(self._app_hero_card_classes(model.app_stats)).style(
                self._hero_card_style(model.app_color_hex)
            )
            with hero_card:
                self._render_app_node_badge(ui=ui, node_name=model.node_name)
                with ui.column().classes(self._app_page_hero_shell_classes()):
                    apply_app_hero_runtime: Callable[[NodeAppRuntimeSummary | None], None] = (
                        self._render_live_app_hero_runtime(
                            ui=ui,
                            hero_card=hero_card,
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
            hero_card: Card = ui.card().classes(self._app_hero_card_classes(model.app_stats)).style(
                self._hero_card_style(model.app_color_hex)
            )
            with hero_card:
                self._render_app_node_badge(ui=ui, node_name=model.node_name)
                with ui.column().classes(self._app_page_hero_shell_classes()):
                    apply_app_hero_runtime: Callable[[NodeAppRuntimeSummary | None], None] = (
                        self._render_live_app_hero_runtime(
                            ui=ui,
                            hero_card=hero_card,
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

    def _app_hero_runtime_details(self, app_stats: NodeAppRuntimeSummary | None) -> _ModWebAppHeroRuntimeDetails:
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
        elif not app_stats.enabled:
            status_text = "Disabled"
            status_tone = "red"
        elif app_stats.runtime_fault is not None:
            status_text = "Crashed"
            status_tone = "red"
        elif app_stats.enabled:
            status_text = "Stopped"
            status_tone = "warn"
        else:
            status_text = "Stopped"
            status_tone = "warn"

        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(text=f"{app_stats.relay_support.display_value}", tone="grey"),
            _ModWebBadgeSpec(text=f"{app_stats.version or 'Unknown'}", tone="black"),
            _ModWebBadgeSpec(
                text=(
                    f"{self._app_footprint_value(app_stats.footprint_bytes)}"
                    if app_stats.footprint_bytes is not None
                    else "Unavailable"
                ),
                tone="grey",
            ),
        ]
        player_count_badge: _ModWebBadgeSpec | None = None
        if app_stats.player_count is not None and app_stats.player_capacity is not None:
            player_tone: Literal["purple"] | Literal["grey"] = "purple" if app_stats.player_count > 0 else "grey"
            player_count_badge = _ModWebBadgeSpec(
                text=f"{app_stats.player_count} / {app_stats.player_capacity}",
                tone=player_tone,
            )
        return _ModWebAppHeroRuntimeDetails(
            status_text=status_text,
            status_tone=status_tone,
            badges=tuple[_ModWebBadgeSpec, ...](badges),
            player_count_badge=player_count_badge,
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

    def _app_hero_card_classes(self, app_stats: NodeAppRuntimeSummary | None) -> str:
        classes = self._hero_card_classes()
        if app_stats is None:
            return classes
        runtime_state_class: str | None = self._app_runtime_state_class(
            running=app_stats.running,
            transition_state=app_stats.transition_state,
            class_prefix="mod-app-hero",
        )
        if runtime_state_class is None:
            return classes
        return f"{classes} {runtime_state_class}"

    def _render_live_app_hero_runtime(
        self,
        *,
        ui: ModWebUi,
        hero_card: Card,
        title: str,
        static_badges: tuple[_ModWebBadgeSpec, ...],
        initial_app_stats: NodeAppRuntimeSummary | None,
        refresh_async_app_stats: Callable[[], Awaitable[NodeAppRuntimeSummary | None]] | None = None,
    ) -> Callable[[NodeAppRuntimeSummary | None], None]:
        initial_runtime_details = self._app_hero_runtime_details(initial_app_stats)
        with ui.column().classes("w-full gap-3"):
            with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                with ui.column().classes(self._hero_header_main_classes()):
                    ui.label(title).classes(self._hero_title_classes())
                with ui.column().classes("mod-app-hero-status gap-1"):
                    ui.label("Status").classes("mod-app-hero-status-label")
                    status_value_label = ui.label(initial_runtime_details.status_text).classes(
                        f"mod-app-hero-status-value mod-app-hero-status-value-{initial_runtime_details.status_tone}"
                    )
            with ui.row().classes(f"{self._hero_badge_row_classes(fill=True)} w-full"):
                runtime_badge_labels: tuple[Label, ...] = tuple(
                    self._badge(ui=ui, text=badge.text, tone=badge.tone) for badge in initial_runtime_details.badges
                )
                player_badge = self._badge(
                    ui=ui,
                    text=initial_runtime_details.player_count_badge.text
                    if initial_runtime_details.player_count_badge is not None
                    else "",
                    tone=initial_runtime_details.player_count_badge.tone
                    if initial_runtime_details.player_count_badge is not None
                    else "grey",
                )
                player_badge_tooltip, player_badge_tooltip_content = self._attach_html_tooltip(
                    ui=ui,
                    target=player_badge,
                    html=(
                        self._player_count_tooltip_html(
                            player_count=initial_app_stats.player_count if initial_app_stats is not None else None,
                            player_capacity=initial_app_stats.player_capacity
                            if initial_app_stats is not None
                            else None,
                            connected_player_names=initial_app_stats.connected_player_names
                            if initial_app_stats is not None
                            else (),
                        )
                        or ""
                    ),
                )
                self._set_optional_badge_state(player_badge, initial_runtime_details.player_count_badge)
                for badge in static_badges:
                    self._badge(ui=ui, text=badge.text, tone=badge.tone)

        def _apply_runtime(app_stats: NodeAppRuntimeSummary | None) -> None:
            hero_card.classes(replace=self._app_hero_card_classes(app_stats))
            runtime_details = self._app_hero_runtime_details(app_stats)
            status_value_label.set_text(runtime_details.status_text)
            status_value_label.classes(
                replace=f"mod-app-hero-status-value mod-app-hero-status-value-{runtime_details.status_tone}"
            )
            for runtime_badge_label, runtime_badge in zip(
                runtime_badge_labels,
                runtime_details.badges,
                strict=True,
            ):
                self._set_badge_state(runtime_badge_label, runtime_badge.text, runtime_badge.tone)
            self._set_optional_badge_state(player_badge, runtime_details.player_count_badge)
            self._set_html_tooltip_state(
                player_badge_tooltip,
                player_badge_tooltip_content,
                self._player_count_tooltip_html(
                    player_count=app_stats.player_count if app_stats is not None else None,
                    player_capacity=app_stats.player_capacity if app_stats is not None else None,
                    connected_player_names=app_stats.connected_player_names if app_stats is not None else (),
                )
                or "",
            )

        hero_card.classes(replace=self._app_hero_card_classes(initial_app_stats))
        if refresh_async_app_stats is None:
            return _apply_runtime
        refresh_async: AsyncRefresh = self._build_async_refreshable_updater(
            refresh_async_value=refresh_async_app_stats,
            apply_value=_apply_runtime,
            error_context="Mod web app hero runtime",
        )
        refresh_timer: Timer = ui.timer(
            _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
            lambda: asyncio.create_task(refresh_async()),
        )
        self._register_timer_cleanup(ui=ui, timer=refresh_timer)
        return _apply_runtime

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
        if settings.pending_change_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{settings.pending_change_count} drafts", tone="grey"))
        if settings.settings:
            badges.append(_ModWebBadgeSpec(text=f"{settings.editable_count} editable", tone="purple"))
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
    def _blueprint_section_badges(
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        del user
        blueprints = model.blueprints
        if blueprints is None:
            return ()
        config_count: int = sum(1 for blueprint in blueprints.blueprints if blueprint.config_file is not None)
        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(
                text=f"{len(blueprints.blueprints)} blueprints",
                tone="black" if blueprints.blueprints else "grey",
            ),
            _ModWebBadgeSpec(text="User upload", tone="purple"),
        ]
        if config_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{config_count} with config", tone="grey"))
        return tuple(badges)

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

    def _blueprint_tab_badges(
        self,
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        del tab
        return self._blueprint_section_badges(model=model, user=user)

    @staticmethod
    def _map_tab_badges(
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        del user, tab
        badges = [
            _ModWebBadgeSpec(text="Shared plan", tone="purple"),
            _ModWebBadgeSpec(text="Live tiles", tone="black"),
        ]
        badges.append(
            _ModWebBadgeSpec(
                text="User write" if model.can_write_map_annotations else "Read only",
                tone="purple" if model.can_write_map_annotations else "grey",
            )
        )
        return tuple(badges)

    @staticmethod
    def _map_tab_actions(
        *,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> tuple[_ModWebTabActionSpec, ...]:
        del user, tab
        if model.map_url is None:
            return ()
        return (_ModWebTabActionSpec(label="Open Public", url=model.map_url, new_tab=True),)

    def _render_map_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> None:
        del user, tab
        if model.map_api_url is None:
            raise ValueError("The Map tab requires a map_api_url.")
        self._ensure_map_client_assets(ui)
        container_id = self._map_container_id(model)
        canvas_id = f"{container_id}-canvas"
        world_id = f"{container_id}-world"
        label_id = f"{container_id}-label"
        color_id = f"{container_id}-color"
        snap_id = f"{container_id}-snap"
        status_id = f"{container_id}-status"
        notice_id = f"{container_id}-notice"
        mode_id = f"{container_id}-mode"
        finish_id = f"{container_id}-finish"
        cancel_id = f"{container_id}-cancel"
        refresh_id = f"{container_id}-refresh"
        marker_id = f"{container_id}-marker"
        line_id = f"{container_id}-line"
        pan_id = f"{container_id}-pan"
        read_only_note = (
            ""
            if model.can_write_map_annotations
            else (
                '<div class="mod-card mod-card-plain mod-map-note">'
                '<div class="mod-map-readonly mod-subtitle">Sign in with a `User` level account to add shared annotations.</div>'
                "</div>"
            )
        )
        write_controls = (
            ""
            if not model.can_write_map_annotations
            else (
                f'<div class="mod-card mod-card-plain mod-map-toolset">'
                f'<input id="{label_id}" class="mod-map-input" type="text" maxlength="80" placeholder="Label" value="">'
                f'<input id="{color_id}" class="mod-map-color" type="color" value="#22C55E" aria-label="Annotation color">'
                f'<label class="mod-map-toggle" for="{snap_id}"><input id="{snap_id}" type="checkbox" checked>45°</label>'
                f'<button id="{pan_id}" type="button" class="mod-list-button secondary mod-map-button mod-map-button-active">Pan</button>'
                f'<button id="{marker_id}" type="button" class="mod-list-button secondary mod-map-button">Point</button>'
                f'<button id="{line_id}" type="button" class="mod-list-button secondary mod-map-button">Line</button>'
                f'<button id="{finish_id}" type="button" class="mod-list-button mod-map-button">Finish</button>'
                f'<button id="{cancel_id}" type="button" class="mod-list-button secondary mod-map-button">Cancel</button>'
                f"</div>"
            )
        )
        ui.html(
            f"""
            <div id="{container_id}" class="mod-map-shell">
              <div class="mod-card mod-card-plain mod-map-toolbar">
                <div class="mod-map-toolbar-main">
                  <select id="{world_id}" class="mod-map-select" aria-label="World"></select>
                  <div id="{mode_id}" class="mod-map-mode mod-subtitle">Loading map…</div>
                </div>
                <div class="mod-map-toolbar-actions">
                  <button id="{refresh_id}" type="button" class="mod-list-button secondary mod-map-button">Refresh</button>
                </div>
              </div>
              {write_controls}
              {read_only_note}
              <div class="mod-card mod-card-plain mod-map-status-panel">
                <div id="{status_id}" class="mod-map-status mod-subtitle">Loading map data…</div>
                <div id="{notice_id}" class="mod-map-notice mod-subtitle">Connecting to Squaremap…</div>
                <div class="mod-map-help mod-subtitle">Plan bases, portals, and 45 degree rail lines without leaving the dashboard.</div>
              </div>
              <div class="mod-card mod-card-plain mod-map-stage">
                <div id="{canvas_id}" class="mod-map-canvas"></div>
              </div>
            </div>
            """
        )
        ui.run_javascript(
            self._map_client_bootstrap_script(
                config_payload={
                    "containerId": container_id,
                    "canvasId": canvas_id,
                    "worldSelectId": world_id,
                    "labelInputId": label_id,
                    "colorInputId": color_id,
                    "snapToggleId": snap_id,
                    "statusId": status_id,
                    "noticeId": notice_id,
                    "modeId": mode_id,
                    "finishButtonId": finish_id,
                    "cancelButtonId": cancel_id,
                    "refreshButtonId": refresh_id,
                    "markerButtonId": marker_id,
                    "lineButtonId": line_id,
                    "panButtonId": pan_id,
                    "mapApiUrl": model.map_api_url,
                    "publicMapUrl": model.map_url,
                    "canWrite": model.can_write_map_annotations,
                }
            ),
            timeout=1.0,
        )
        return None

    @staticmethod
    def _map_container_id(model: ModWebBasePageModel) -> str:
        return (
            f"mod-map-{model.node_name}-{model.app_name}".replace("_", "-")
            .replace(" ", "-")
            .replace("/", "-")
            .casefold()
        )

    @staticmethod
    def _ensure_map_client_assets(ui: ModWebUi) -> None:
        ui.add_head_html(ModWebAppPageMixin._map_client_assets_html())

    @staticmethod
    def _map_client_assets_html() -> str:
        return """
            <style>
              __LEAFLET_CSS__
            </style>
            <script>
              __LEAFLET_JS__
            </script>
            <style>
              .mod-map-shell {
                display: flex;
                flex-direction: column;
                gap: 0.85rem;
              }
              .mod-map-toolbar,
              .mod-map-toolset,
              .mod-map-status-panel {
                display: flex;
                flex-wrap: wrap;
                align-items: center;
                justify-content: space-between;
                gap: 0.75rem;
              }
              .mod-map-toolbar,
              .mod-map-toolset,
              .mod-map-status-panel,
              .mod-map-note,
              .mod-map-stage {
                padding: 1rem 1.1rem;
              }
              .mod-map-toolbar-main,
              .mod-map-toolbar-actions {
                display: flex;
                flex-wrap: wrap;
                align-items: center;
                gap: 0.75rem;
              }
              .mod-map-toolset {
                padding: 0.85rem 1rem;
                border: 1px solid var(--mod-border);
                border-radius: 1rem;
                background:
                  linear-gradient(135deg, rgba(22, 163, 74, 0.06), rgba(15, 23, 42, 0.02)),
                  var(--mod-card-bg, rgba(255, 255, 255, 0.7));
              }
              .mod-map-status-panel {
                align-items: flex-start;
              }
              .mod-map-stage {
                padding: 0.8rem;
              }
              .mod-map-select,
              .mod-map-input,
              .mod-map-color {
                border: 1px solid var(--mod-border);
                border-radius: 999px;
                background: rgba(255, 255, 255, 0.88);
                color: var(--mod-text, #111827);
                min-height: 2.5rem;
              }
              .mod-map-select,
              .mod-map-input {
                padding: 0 0.9rem;
              }
              .mod-map-input {
                min-width: 14rem;
              }
              .mod-map-color {
                width: 2.75rem;
                padding: 0.25rem;
              }
              .mod-map-button {
                min-height: 2.5rem;
              }
              .mod-map-button-active {
                border-color: #15803d;
                box-shadow: inset 0 0 0 1px rgba(21, 128, 61, 0.2);
                background: rgba(34, 197, 94, 0.12);
              }
              .mod-map-toggle {
                display: inline-flex;
                align-items: center;
                gap: 0.4rem;
                font-size: 0.92rem;
                color: var(--mod-subtitle, #4b5563);
              }
              .mod-map-readonly,
              .mod-map-status,
              .mod-map-notice,
              .mod-map-mode,
              .mod-map-help {
                color: var(--mod-subtitle, #4b5563);
                font-size: 0.92rem;
              }
              .mod-map-status {
                font-weight: 600;
              }
              .mod-map-help {
                width: 100%;
              }
              .mod-map-notice {
                width: 100%;
              }
              .mod-map-notice:empty {
                display: none;
              }
              .mod-map-status[data-tone="error"] {
                color: #b91c1c;
              }
              .mod-map-status[data-tone="warning"] {
                color: #b45309;
              }
              .mod-map-status[data-tone="success"] {
                color: #166534;
              }
              .mod-map-notice[data-tone="error"] {
                color: #991b1b;
              }
              .mod-map-notice[data-tone="warning"] {
                color: #92400e;
              }
              .mod-map-notice[data-tone="success"] {
                color: #166534;
              }
              .mod-map-canvas {
                width: 100%;
                min-height: 34rem;
                height: min(68vh, 52rem);
                border-radius: 1.25rem;
                border: 1px solid rgba(15, 23, 42, 0.12);
                overflow: hidden;
                background:
                  radial-gradient(circle at top, rgba(56, 189, 248, 0.18), transparent 38%),
                  linear-gradient(180deg, rgba(226, 232, 240, 0.92), rgba(248, 250, 252, 0.96));
              }
              .mod-map-canvas .leaflet-container {
                width: 100%;
                height: 100%;
                background: transparent;
                font: inherit;
              }
              .mod-map-player-label,
              .mod-map-annotation-label {
                background: rgba(255, 255, 255, 0.94);
                border: 1px solid rgba(15, 23, 42, 0.12);
                border-radius: 999px;
                box-shadow: 0 8px 22px rgba(15, 23, 42, 0.12);
                color: #0f172a;
                font-size: 0.82rem;
                font-weight: 600;
                padding: 0.15rem 0.45rem;
              }
              .mod-map-annotation-popup button {
                margin-top: 0.5rem;
                border: 1px solid rgba(185, 28, 28, 0.28);
                border-radius: 999px;
                background: rgba(248, 113, 113, 0.12);
                color: #991b1b;
                cursor: pointer;
                font: inherit;
                padding: 0.3rem 0.7rem;
              }
              @media (max-width: 768px) {
                .mod-map-input {
                  min-width: 10rem;
                  flex: 1 1 100%;
                }
                .mod-map-canvas {
                  min-height: 26rem;
                  height: 58vh;
                }
              }
            </style>
            <script>
              if (window.L && !window.L.ellipse) {
                window.L.Ellipse = window.L.Path.extend({
                  initialize(latlng, radii, tilt, options) {
                    this._latlng = window.L.latLng(latlng);
                    this._radii = window.L.point(radii[0], radii[1]);
                    this._tilt = tilt || 0;
                    window.L.setOptions(this, options);
                  },
                  getLatLng() {
                    return this._latlng;
                  },
                  setLatLng(latlng) {
                    this._latlng = window.L.latLng(latlng);
                    return this.redraw();
                  },
                  _project() {
                    this._point = this._map.latLngToLayerPoint(this._latlng);
                    const centerPoint = this._map.latLngToLayerPoint(this._latlng);
                    const radiusXPoint = this._map.latLngToLayerPoint([this._latlng.lat, this._latlng.lng + this._radii.x]);
                    const radiusYPoint = this._map.latLngToLayerPoint([this._latlng.lat + this._radii.y, this._latlng.lng]);
                    this._radiusX = Math.max(Math.abs(radiusXPoint.x - centerPoint.x), 1);
                    this._radiusY = Math.max(Math.abs(radiusYPoint.y - centerPoint.y), 1);
                    this._updateBounds();
                  },
                  _updateBounds() {
                    const radius = [this._radiusX, this._radiusY];
                    this._pxBounds = new window.L.Bounds(
                      this._point.subtract(radius),
                      this._point.add(radius),
                    );
                  },
                  _update() {
                    if (!this._map) {
                      return;
                    }
                    this._updatePath();
                  },
                  _updatePath() {
                    this._renderer._updateEllipse(this);
                  },
                  _empty() {
                    return this._radiusX <= 0 || this._radiusY <= 0;
                  },
                  _containsPoint(point) {
                    if (!this._pxBounds || !this._pxBounds.contains(point)) {
                      return false;
                    }
                    const dx = (point.x - this._point.x) / this._radiusX;
                    const dy = (point.y - this._point.y) / this._radiusY;
                    return dx * dx + dy * dy <= 1;
                  },
                });
                window.L.ellipse = (latlng, radii, tilt, options) => new window.L.Ellipse(latlng, radii, tilt, options);
                if (window.L.SVG) {
                  window.L.SVG.include({
                    _updateEllipse(layer) {
                      const path = [
                        "M", layer._point.x - layer._radiusX, layer._point.y,
                        "a", layer._radiusX, layer._radiusY, 0, 1, 0, layer._radiusX * 2, 0,
                        "a", layer._radiusX, layer._radiusY, 0, 1, 0, -(layer._radiusX * 2), 0,
                      ].join(" ");
                      this._setPath(layer, path);
                    },
                  });
                }
                if (window.L.Canvas) {
                  window.L.Canvas.include({
                    _updateEllipse(layer) {
                      if (layer._empty()) {
                        return;
                      }
                      const ctx = this._ctx;
                      const point = layer._point;
                      ctx.beginPath();
                      ctx.ellipse(point.x, point.y, layer._radiusX, layer._radiusY, 0, 0, Math.PI * 2);
                      this._fillStroke(ctx, layer);
                    },
                  });
                }
              }
              if (!window.modWebMap) {
                window.modWebMap = (() => {
                  const transparentTile = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";
                  const instances = new Map();
                  const MAP_SOURCE_LIVE = "live";
                  const MAP_SOURCE_STALE = "stale";

                  const get = (id) => document.getElementById(id);
                  const escapeHtml = (value) =>
                    String(value ?? "")
                      .replaceAll("&", "&amp;")
                      .replaceAll("<", "&lt;")
                      .replaceAll(">", "&gt;")
                      .replaceAll('"', "&quot;")
                      .replaceAll("'", "&#39;");
                  const toLatLng = (world, point) => [(-point.z) * world.scale, point.x * world.scale];
                  const toPoint = (world, latlng) => ({ x: latlng.lng / world.scale, z: -latlng.lat / world.scale });
                  const convertNestedPoints = (world, points) =>
                    Array.isArray(points)
                      ? points.map((point) => (Array.isArray(point) ? convertNestedPoints(world, point) : toLatLng(world, point)))
                      : [];
                  const squaremapRadius = (world, radius) => radius * world.scale;
                  const annotationPopupHtml = (annotation, canWrite, containerId) => {
                    const label = escapeHtml(annotation.label);
                    const createdBy = annotation.created_by_name ? `<div>${escapeHtml(annotation.created_by_name)}</div>` : "";
                    const deleteButton = canWrite
                      ? `<button type="button" data-map-delete="${escapeHtml(annotation.annotation_id)}" data-map-container="${escapeHtml(containerId)}">Delete</button>`
                      : "";
                    return `<div class="mod-map-annotation-popup"><strong>${label}</strong>${createdBy}${deleteButton}</div>`;
                  };
                  const setStatus = (state, message, tone = "info") => {
                    if (!state.statusEl) {
                      return;
                    }
                    state.statusEl.textContent = message;
                    state.statusEl.dataset.tone = tone;
                  };
                  const setNotice = (state, message = "", tone = "info") => {
                    if (!state.noticeEl) {
                      return;
                    }
                    state.noticeEl.textContent = message;
                    state.noticeEl.dataset.tone = tone;
                  };
                  const setModeText = (state, message) => {
                    if (state.modeEl) {
                      state.modeEl.textContent = message;
                    }
                  };
                  const setToolState = (state, tool) => {
                    state.tool = tool;
                    for (const [name, button] of Object.entries(state.toolButtons)) {
                      if (!button) {
                        continue;
                      }
                      button.classList.toggle("mod-map-button-active", name === tool);
                    }
                    if (tool === "line") {
                      const pointCount = state.pendingLine.length;
                      setModeText(state, pointCount > 0 ? `Line tool: ${pointCount} point${pointCount === 1 ? "" : "s"}` : "Line tool");
                      return;
                    }
                    setModeText(state, tool === "marker" ? "Point tool" : "Pan tool");
                  };
                  const destroyInstance = (containerId) => {
                    const state = instances.get(containerId);
                    if (!state) {
                      return;
                    }
                    if (state.pollTimer) {
                      window.clearInterval(state.pollTimer);
                    }
                    if (state.visibilityTimer) {
                      window.clearInterval(state.visibilityTimer);
                    }
                    if (state.map) {
                      state.map.remove();
                    }
                    instances.delete(containerId);
                  };
                  const waitForLeaflet = (callback, attempts = 0) => {
                    if (window.L) {
                      callback();
                      return;
                    }
                    if (attempts > 100) {
                      return;
                    }
                    window.setTimeout(() => waitForLeaflet(callback, attempts + 1), 100);
                  };
                  const parseMapCacheUpdatedAt = (value) => {
                    const parsed = Number(value);
                    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
                  };
                  const describeCacheAge = (updatedAtUnixMs) => {
                    if (!Number.isFinite(updatedAtUnixMs)) {
                      return "an earlier session";
                    }
                    const ageSeconds = Math.max(0, Math.round((Date.now() - updatedAtUnixMs) / 1000));
                    if (ageSeconds < 10) {
                      return "just now";
                    }
                    if (ageSeconds < 60) {
                      return `${ageSeconds}s ago`;
                    }
                    if (ageSeconds < 3600) {
                      return `${Math.round(ageSeconds / 60)}m ago`;
                    }
                    if (ageSeconds < 86400) {
                      return `${Math.round(ageSeconds / 3600)}h ago`;
                    }
                    return `${Math.round(ageSeconds / 86400)}d ago`;
                  };
                  const normalizeMapError = (message, fallback = "Map data is unavailable.") => {
                    const text = String(message || "").trim();
                    if (!text) {
                      return fallback;
                    }
                    if (text.startsWith("Squaremap")) {
                      return text;
                    }
                    if (/^HTTP \\d+$/.test(text)) {
                      return text;
                    }
                    return text;
                  };
                  const responseErrorMessage = async (response) => {
                    try {
                      const payload = await response.clone().json();
                      if (payload && typeof payload.detail === "string" && payload.detail.trim()) {
                        return normalizeMapError(payload.detail, `HTTP ${response.status}`);
                      }
                    } catch {
                    }
                    return `HTTP ${response.status}`;
                  };
                  const fetchJsonDetailed = async (url, options = {}) => {
                    const response = await fetch(url, {
                      ...options,
                      headers: {
                        Accept: "application/json",
                        ...(options.headers || {}),
                      },
                    });
                    if (!response.ok) {
                      throw new Error(await responseErrorMessage(response));
                    }
                    return {
                      data: await response.json(),
                      source:
                        response.headers.get("X-Yukibot-Map-Source") === MAP_SOURCE_STALE
                          ? MAP_SOURCE_STALE
                          : MAP_SOURCE_LIVE,
                      cacheUpdatedAtUnixMs: parseMapCacheUpdatedAt(
                        response.headers.get("X-Yukibot-Map-Cache-Updated-At"),
                      ),
                    };
                  };
                  const fetchJson = async (url, options = {}) => (await fetchJsonDetailed(url, options)).data;
                  const apiUrl = (state, suffix, extraParams = {}) => {
                    const baseUrl = new URL(state.config.mapApiUrl, window.location.origin);
                    const basePath = baseUrl.pathname.replace(/\\/$/, "");
                    const searchParams = new URLSearchParams(baseUrl.search);
                    for (const [key, value] of Object.entries(extraParams)) {
                      searchParams.set(key, String(value));
                    }
                    const query = searchParams.toString();
                    return `${basePath}${suffix}${query ? `?${query}` : ""}`;
                  };
                  const tileUrlTemplate = (state, world) =>
                    apiUrl(
                      state,
                      `/worlds/${encodeURIComponent(world.name)}/tiles/{z}/{x}_{y}.png`,
                      { tile_rev: state.tileRevision },
                    );
                  const createTileLayer = (state, world) =>
                    window.L.tileLayer(tileUrlTemplate(state, world), {
                      tileSize: 512,
                      minNativeZoom: 0,
                      maxNativeZoom: world.zoom.max,
                      minZoom: 0,
                      maxZoom: world.zoom.max + world.zoom.extra,
                      noWrap: true,
                      errorTileUrl: transparentTile,
                    });
                  const populateWorldOptions = (state) => {
                    if (!state.worldSelect) {
                      return;
                    }
                    state.worldSelect.textContent = "";
                    for (const world of state.manifest?.worlds || []) {
                      state.worldByName.set(world.name, world);
                      const option = document.createElement("option");
                      option.value = world.name;
                      option.textContent = world.display_name;
                      state.worldSelect.appendChild(option);
                    }
                  };
                  const setSquaremapLiveNotice = (state) => {
                    state.squaremapSource = MAP_SOURCE_LIVE;
                    state.lastSquaremapCacheUpdatedAtUnixMs = null;
                    setNotice(
                      state,
                      "Squaremap is live. Shared dashboard annotations stay available independently.",
                      "success",
                    );
                  };
                  const setSquaremapStaleNotice = (state, cacheUpdatedAtUnixMs = null) => {
                    state.squaremapSource = MAP_SOURCE_STALE;
                    state.lastSquaremapCacheUpdatedAtUnixMs = cacheUpdatedAtUnixMs;
                    const cacheAge = describeCacheAge(cacheUpdatedAtUnixMs);
                    setNotice(
                      state,
                      `Squaremap is offline. Showing cached map metadata from ${cacheAge}. Tile updates and player positions may lag.`,
                      "warning",
                    );
                  };
                  const setSquaremapOfflineNotice = (state, { cacheUpdatedAtUnixMs = null, noCache = false } = {}) => {
                    state.squaremapSource = MAP_SOURCE_STALE;
                    state.lastSquaremapCacheUpdatedAtUnixMs = cacheUpdatedAtUnixMs;
                    if (noCache) {
                      setNotice(
                        state,
                        "Squaremap is offline and no cached map data is available yet. Start the app once, then use Refresh.",
                        "error",
                      );
                      return;
                    }
                    const cacheAge = describeCacheAge(cacheUpdatedAtUnixMs);
                    setNotice(
                      state,
                      `Squaremap is offline. Keeping the last loaded view and shared annotations. Cached data is from ${cacheAge}.`,
                      "warning",
                    );
                  };
                  const createPopupDeleteHandler = (state, layer, annotation) => {
                    layer.on("popupopen", (event) => {
                      const popupRoot = event.popup.getElement();
                      if (!popupRoot) {
                        return;
                      }
                      const button = popupRoot.querySelector("[data-map-delete]");
                      if (!button) {
                        return;
                      }
                      button.addEventListener("click", async (clickEvent) => {
                        clickEvent.preventDefault();
                        clickEvent.stopPropagation();
                        await deleteAnnotation(state.config.containerId, annotation.annotation_id);
                      });
                    });
                  };
                  const bindAnnotationLayer = (state, layer, annotation) => {
                    layer.bindPopup(annotationPopupHtml(annotation, state.config.canWrite, state.config.containerId));
                    if (annotation.shape === "marker") {
                      layer.bindTooltip(annotation.label, {
                        permanent: true,
                        direction: "top",
                        className: "mod-map-annotation-label",
                        offset: [0, -8],
                      });
                    } else {
                      layer.bindTooltip(annotation.label, {
                        sticky: true,
                        direction: "top",
                        className: "mod-map-annotation-label",
                      });
                    }
                    if (state.config.canWrite) {
                      createPopupDeleteHandler(state, layer, annotation);
                    }
                  };
                  const refreshAnnotations = async (state) => {
                    const payload = await fetchJson(apiUrl(state, "/annotations"));
                    state.annotationLayer.clearLayers();
                    for (const annotation of payload.annotations || []) {
                      if (!state.currentWorld || annotation.world_name !== state.currentWorld.name) {
                        continue;
                      }
                      if (annotation.shape === "marker") {
                        const [point] = annotation.points;
                        const layer = window.L.circleMarker(toLatLng(state.currentWorld, point), {
                          radius: 7,
                          color: annotation.color_hex,
                          fillColor: annotation.color_hex,
                          fillOpacity: 0.95,
                          weight: 2,
                        }).addTo(state.annotationLayer);
                        bindAnnotationLayer(state, layer, annotation);
                        continue;
                      }
                      if (annotation.shape === "polyline") {
                        const layer = window.L.polyline(
                          annotation.points.map((point) => toLatLng(state.currentWorld, point)),
                          {
                            color: annotation.color_hex,
                            weight: 4,
                            opacity: 0.92,
                            dashArray: "12 8",
                          },
                        ).addTo(state.annotationLayer);
                        bindAnnotationLayer(state, layer, annotation);
                      }
                    }
                  };
                  const refreshPlayers = async (state) => {
                    if (!state.currentWorld) {
                      return { source: MAP_SOURCE_LIVE, cacheUpdatedAtUnixMs: null, skipped: true };
                    }
                    if (!state.currentWorld.player_tracker?.enabled) {
                      state.playerLayer.clearLayers();
                      return { source: MAP_SOURCE_LIVE, cacheUpdatedAtUnixMs: null, skipped: true };
                    }
                    const result = await fetchJsonDetailed(apiUrl(state, "/players"));
                    const payload = result.data;
                    state.playerLayer.clearLayers();
                    const players = Array.isArray(payload?.players)
                      ? payload.players
                      : Array.isArray(payload)
                        ? payload
                        : [];
                    if (players.length === 0) {
                      return result;
                    }
                    for (const player of players) {
                      if (!player || player.world !== state.currentWorld.name) {
                        continue;
                      }
                      const marker = window.L.circleMarker(toLatLng(state.currentWorld, player), {
                        radius: 6,
                        color: "#0f172a",
                        fillColor: "#22c55e",
                        fillOpacity: 0.95,
                        weight: 2,
                      }).addTo(state.playerLayer);
                      marker.bindTooltip(player.display_name || player.name, {
                        direction: "top",
                        className: "mod-map-player-label",
                      });
                    }
                    return result;
                  };
                  const squaremapMarkerLayer = (state, markerData) => {
                    const world = state.currentWorld;
                    if (!world || !markerData || typeof markerData !== "object") {
                      return null;
                    }
                    const markerType = markerData.type;
                    if (markerType === "icon" && markerData.point && markerData.icon) {
                      const size = markerData.size || { x: 24, z: 24 };
                      const anchor = markerData.anchor || { x: Math.round(size.x / 2), z: Math.round(size.z / 2) };
                      const tooltipAnchor = markerData.tooltip_anchor || { x: 0, z: -Math.round(size.z / 2) };
                      return window.L.marker(toLatLng(world, markerData.point), {
                        icon: window.L.icon({
                          iconUrl: `${state.iconBaseUrl}/icon/registered/${encodeURIComponent(markerData.icon)}.png`,
                          iconSize: [size.x, size.z],
                          iconAnchor: [anchor.x, anchor.z],
                          popupAnchor: [tooltipAnchor.x, tooltipAnchor.z],
                          tooltipAnchor: [tooltipAnchor.x, tooltipAnchor.z],
                        }),
                      });
                    }
                    if (markerType === "polyline" && Array.isArray(markerData.points)) {
                      return window.L.polyline(convertNestedPoints(world, markerData.points), markerData);
                    }
                    if (markerType === "polygon" && Array.isArray(markerData.points)) {
                      return window.L.polygon(convertNestedPoints(world, markerData.points), markerData);
                    }
                    if (markerType === "rectangle" && Array.isArray(markerData.points) && markerData.points.length >= 2) {
                      return window.L.rectangle(
                        [toLatLng(world, markerData.points[0]), toLatLng(world, markerData.points[1])],
                        markerData,
                      );
                    }
                    if (markerType === "circle" && markerData.center && typeof markerData.radius === "number") {
                      return window.L.circle(toLatLng(world, markerData.center), {
                        ...markerData,
                        radius: squaremapRadius(world, markerData.radius),
                      });
                    }
                    if (
                      markerType === "ellipse" &&
                      markerData.center &&
                      typeof markerData.radiusX === "number" &&
                      typeof markerData.radiusZ === "number"
                    ) {
                      return window.L.ellipse(
                        toLatLng(world, markerData.center),
                        [squaremapRadius(world, markerData.radiusX), squaremapRadius(world, markerData.radiusZ)],
                        0,
                        markerData,
                      );
                    }
                    return null;
                  };
                  const refreshSquaremapMarkers = async (state) => {
                    if (!state.currentWorld) {
                      return { source: MAP_SOURCE_LIVE, cacheUpdatedAtUnixMs: null, skipped: true };
                    }
                    const result = await fetchJsonDetailed(
                      apiUrl(state, `/worlds/${encodeURIComponent(state.currentWorld.name)}/markers`),
                    );
                    const payload = result.data;
                    state.squaremapLayer.clearLayers();
                    if (!Array.isArray(payload)) {
                      return result;
                    }
                    for (const markerLayer of payload) {
                      if (!markerLayer || markerLayer.hide === true || !markerLayer.markers) {
                        continue;
                      }
                      for (const markerData of Object.values(markerLayer.markers)) {
                        const layer = squaremapMarkerLayer(state, markerData);
                        if (!layer) {
                          continue;
                        }
                        if (markerData.tooltip) {
                          layer.bindTooltip(markerData.tooltip, { sticky: true, direction: markerData.tooltip_direction || "top" });
                        }
                        if (markerData.popup) {
                          layer.bindPopup(markerData.popup);
                        }
                        layer.addTo(state.squaremapLayer);
                      }
                    }
                    return result;
                  };
                  const snapLinePoint = (anchor, point) => {
                    const deltaX = point.x - anchor.x;
                    const deltaZ = point.z - anchor.z;
                    const absX = Math.abs(deltaX);
                    const absZ = Math.abs(deltaZ);
                    if (absX === 0 && absZ === 0) {
                      return point;
                    }
                    if (absX > absZ * 2) {
                      return { x: point.x, z: anchor.z };
                    }
                    if (absZ > absX * 2) {
                      return { x: anchor.x, z: point.z };
                    }
                    const diagonal = Math.max(absX, absZ);
                    return {
                      x: anchor.x + Math.sign(deltaX || 1) * diagonal,
                      z: anchor.z + Math.sign(deltaZ || 1) * diagonal,
                    };
                  };
                  const refreshTiles = (state, { force = false } = {}) => {
                    if (!state.currentWorld || !state.tileLayer) {
                      return;
                    }
                    const now = Date.now();
                    const intervalMs = Math.max(1, state.currentWorld.tiles_update_interval || 15) * 1000;
                    if (!force && state.lastTileRefreshAt !== null && now - state.lastTileRefreshAt < intervalMs) {
                      return;
                    }
                    state.tileRevision += 1;
                    state.lastTileRefreshAt = now;
                    state.tileLayer.setUrl(tileUrlTemplate(state, state.currentWorld));
                  };
                  const currentDraftLabel = (state) => {
                    if (!state.labelInput) {
                      return "";
                    }
                    return state.labelInput.value.trim();
                  };
                  const currentDraftColor = (state) => {
                    if (!state.colorInput || !state.colorInput.value) {
                      return "#22C55E";
                    }
                    return state.colorInput.value.toUpperCase();
                  };
                  const cancelLineDraft = (state) => {
                    state.pendingLine = [];
                    if (state.previewLayer) {
                      state.previewLayer.remove();
                      state.previewLayer = null;
                    }
                    if (state.config.canWrite) {
                      setToolState(state, "pan");
                    }
                  };
                  const updatePreview = (state, previewPoint = null) => {
                    if (!state.currentWorld || state.pendingLine.length === 0) {
                      if (state.previewLayer) {
                        state.previewLayer.remove();
                        state.previewLayer = null;
                      }
                      return;
                    }
                    const previewPoints = [...state.pendingLine];
                    if (previewPoint) {
                      previewPoints.push(previewPoint);
                    }
                    const latLngs = previewPoints.map((point) => toLatLng(state.currentWorld, point));
                    if (!state.previewLayer) {
                      state.previewLayer = window.L.polyline(latLngs, {
                        color: currentDraftColor(state),
                        weight: 3,
                        opacity: 0.9,
                        dashArray: "8 6",
                      }).addTo(state.previewLayerGroup);
                    } else {
                      state.previewLayer.setStyle({ color: currentDraftColor(state) });
                      state.previewLayer.setLatLngs(latLngs);
                    }
                  };
                  const postAnnotation = async (state, payload) => {
                    const response = await fetch(apiUrl(state, "/annotations"), {
                      method: "POST",
                      headers: { "Content-Type": "application/json", Accept: "application/json" },
                      body: JSON.stringify(payload),
                    });
                    if (!response.ok) {
                      throw new Error(await responseErrorMessage(response));
                    }
                    return await response.json();
                  };
                  const createMarkerAnnotation = async (state, point) => {
                    const label = currentDraftLabel(state);
                    if (!label) {
                      setStatus(state, "Add a label before placing a point.", "error");
                      return;
                    }
                    await postAnnotation(state, {
                      world_name: state.currentWorld.name,
                      shape: "marker",
                      label,
                      color_hex: currentDraftColor(state),
                      points: [point],
                    });
                    setStatus(state, `Added point: ${label}`, "success");
                    await refreshAnnotations(state);
                  };
                  const finishLineAnnotation = async (state) => {
                    const label = currentDraftLabel(state);
                    if (!label) {
                      setStatus(state, "Add a label before saving a line.", "error");
                      return;
                    }
                    if (state.pendingLine.length < 2) {
                      setStatus(state, "A line needs at least two points.", "error");
                      return;
                    }
                    await postAnnotation(state, {
                      world_name: state.currentWorld.name,
                      shape: "polyline",
                      label,
                      color_hex: currentDraftColor(state),
                      points: state.pendingLine,
                    });
                    setStatus(state, `Added line: ${label}`, "success");
                    cancelLineDraft(state);
                    await refreshAnnotations(state);
                  };
                  const refreshAll = async (state, { announce = false, forceTiles = false } = {}) => {
                    if (!state.currentWorld) {
                      return { squaremapSource: state.squaremapSource || MAP_SOURCE_LIVE };
                    }
                    if (announce) {
                      setStatus(state, "Refreshing map data…");
                    }
                    const previousSquaremapSource = state.squaremapSource;
                    const [markerResult, playerResult, annotationResult] = await Promise.allSettled([
                      refreshSquaremapMarkers(state),
                      refreshPlayers(state),
                      refreshAnnotations(state),
                    ]);
                    if (annotationResult.status !== "fulfilled") {
                      throw annotationResult.reason;
                    }
                    const markersLoaded = markerResult.status === "fulfilled" ? markerResult.value : null;
                    const playersLoaded = playerResult.status === "fulfilled" ? playerResult.value : null;
                    const playersFailed = playerResult.status === "rejected";
                    if (markersLoaded && markersLoaded.source === MAP_SOURCE_LIVE) {
                      refreshTiles(state, { force: forceTiles });
                      state.lastSquaremapCacheUpdatedAtUnixMs = null;
                      if (playersFailed) {
                        setNotice(
                          state,
                          "Squaremap tiles and markers are live, but player positions could not be refreshed.",
                          "warning",
                        );
                        if (announce || previousSquaremapSource !== MAP_SOURCE_LIVE) {
                          setStatus(state, "Player positions are temporarily unavailable.", "warning");
                        }
                        state.squaremapSource = MAP_SOURCE_LIVE;
                        return { squaremapSource: MAP_SOURCE_LIVE };
                      }
                      setSquaremapLiveNotice(state);
                      if (announce) {
                        setStatus(state, `Updated ${state.currentWorld.display_name}.`, "success");
                      }
                      return { squaremapSource: MAP_SOURCE_LIVE };
                    }
                    if (markersLoaded && markersLoaded.source === MAP_SOURCE_STALE) {
                      const cacheUpdatedAtUnixMs = markersLoaded.cacheUpdatedAtUnixMs ?? state.lastSquaremapCacheUpdatedAtUnixMs;
                      setSquaremapStaleNotice(state, cacheUpdatedAtUnixMs);
                      if (announce || previousSquaremapSource !== MAP_SOURCE_STALE) {
                        setStatus(state, `Loaded cached ${state.currentWorld.display_name} map data.`, "warning");
                      }
                      return { squaremapSource: MAP_SOURCE_STALE };
                    }
                    const cacheUpdatedAtUnixMs = state.lastSquaremapCacheUpdatedAtUnixMs;
                    setSquaremapOfflineNotice(state, { cacheUpdatedAtUnixMs, noCache: cacheUpdatedAtUnixMs === null });
                    if (announce || previousSquaremapSource !== MAP_SOURCE_STALE) {
                      setStatus(state, "Squaremap is offline. Keeping the last loaded map view.", "warning");
                    }
                    return { squaremapSource: MAP_SOURCE_STALE };
                  };
                  const loadWorld = async (state, worldName, { preserveView = false } = {}) => {
                    const worldSummary = state.worldByName.get(worldName) || state.manifest.worlds[0];
                    const worldSettingsResult = await fetchJsonDetailed(
                      apiUrl(state, `/worlds/${encodeURIComponent(worldSummary.name)}/settings`),
                    );
                    const worldSettings = worldSettingsResult.data;
                    const nextWorld = {
                      ...worldSummary,
                      zoom: worldSettings.zoom,
                      spawn: worldSettings.spawn,
                      player_tracker: worldSettings.player_tracker,
                      marker_update_interval: worldSettings.marker_update_interval || 5,
                      tiles_update_interval: worldSettings.tiles_update_interval || 15,
                      scale: 1 / Math.pow(2, worldSettings.zoom.max),
                    };
                    state.currentWorld = nextWorld;
                    state.worldSelect.value = nextWorld.name;
                    if (state.tileLayer) {
                      state.tileLayer.remove();
                    }
                    state.tileRevision = 0;
                    state.lastTileRefreshAt = null;
                    state.tileLayer = createTileLayer(state, nextWorld).addTo(state.map);
                    if (!preserveView) {
                      state.map.setView(toLatLng(nextWorld, nextWorld.spawn), nextWorld.zoom.def);
                    }
                    cancelLineDraft(state);
                    if (worldSettingsResult.source === MAP_SOURCE_STALE) {
                      setSquaremapStaleNotice(state, worldSettingsResult.cacheUpdatedAtUnixMs);
                    }
                    const refreshResult = await refreshAll(state, { forceTiles: true });
                    state.map.setMinZoom(0);
                    state.map.setMaxZoom(nextWorld.zoom.max + nextWorld.zoom.extra);
                    window.setTimeout(() => state.map.invalidateSize(), 60);
                    if (refreshResult.squaremapSource === MAP_SOURCE_LIVE) {
                      setStatus(state, `Loaded ${nextWorld.display_name}.`, "success");
                    } else {
                      setStatus(state, `Loaded cached ${nextWorld.display_name} data.`, "warning");
                    }
                  };
                  const bootstrapMap = async (state, { announce = false, preserveView = true } = {}) => {
                    if (announce) {
                      setStatus(state, "Refreshing map data…");
                    }
                    const manifestResult = await fetchJsonDetailed(apiUrl(state, "/manifest"));
                    state.manifest = manifestResult.data;
                    state.iconBaseUrl = apiUrl(state, "/assets");
                    state.worldByName.clear();
                    populateWorldOptions(state);
                    const requestedWorldName =
                      state.currentWorld && state.worldByName.has(state.currentWorld.name)
                        ? state.currentWorld.name
                        : state.manifest.initial_world_name;
                    if (manifestResult.source === MAP_SOURCE_STALE) {
                      setSquaremapStaleNotice(state, manifestResult.cacheUpdatedAtUnixMs);
                    } else {
                      setSquaremapLiveNotice(state);
                    }
                    await loadWorld(state, requestedWorldName, { preserveView });
                  };
                  const runMapSync = async (state, operation) => {
                    if (state.syncPromise) {
                      return await state.syncPromise;
                    }
                    const pending = Promise.resolve()
                      .then(operation)
                      .finally(() => {
                        if (state.syncPromise === pending) {
                          state.syncPromise = null;
                        }
                      });
                    state.syncPromise = pending;
                    return await pending;
                  };
                  const handleBootstrapFailure = (state, error) => {
                    const detail =
                      error instanceof Error
                        ? normalizeMapError(error.message, "Map data is unavailable.")
                        : "Map data is unavailable.";
                    setModeText(state, "Map unavailable");
                    setSquaremapOfflineNotice(state, {
                      cacheUpdatedAtUnixMs: state.lastSquaremapCacheUpdatedAtUnixMs,
                      noCache: state.lastSquaremapCacheUpdatedAtUnixMs === null,
                    });
                    setStatus(
                      state,
                      state.lastSquaremapCacheUpdatedAtUnixMs === null
                        ? "Map unavailable while Squaremap is offline."
                        : "Squaremap is offline. Waiting to reconnect.",
                      state.lastSquaremapCacheUpdatedAtUnixMs === null ? "error" : "warning",
                    );
                    if (detail && !detail.startsWith("Squaremap")) {
                      setNotice(state, detail, state.lastSquaremapCacheUpdatedAtUnixMs === null ? "error" : "warning");
                    }
                  };
                  const bindControls = (state) => {
                    state.worldSelect?.addEventListener("change", () => {
                      void runMapSync(state, () => loadWorld(state, state.worldSelect.value));
                    });
                    state.refreshButton?.addEventListener("click", () => {
                      void runMapSync(state, () =>
                        state.manifest && state.currentWorld
                          ? refreshAll(state, { announce: true, forceTiles: true })
                          : bootstrapMap(state, { announce: true }),
                      ).catch((error) => handleBootstrapFailure(state, error));
                    });
                    if (!state.config.canWrite) {
                      return;
                    }
                    state.toolButtons.pan?.addEventListener("click", () => {
                      cancelLineDraft(state);
                      setToolState(state, "pan");
                    });
                    state.toolButtons.marker?.addEventListener("click", () => {
                      cancelLineDraft(state);
                      setToolState(state, "marker");
                    });
                    state.toolButtons.line?.addEventListener("click", () => {
                      setToolState(state, "line");
                      updatePreview(state);
                    });
                    state.finishButton?.addEventListener("click", () => {
                      void finishLineAnnotation(state).catch((error) => setStatus(state, error.message, "error"));
                    });
                    state.cancelButton?.addEventListener("click", () => {
                      cancelLineDraft(state);
                      setStatus(state, "Cancelled line draft.");
                    });
                  };
                  const deleteAnnotation = async (containerId, annotationId) => {
                    const state = instances.get(containerId);
                    if (!state) {
                      return;
                    }
                    const response = await fetch(
                      apiUrl(state, `/annotations/${encodeURIComponent(annotationId)}/delete`),
                      {
                        method: "POST",
                        headers: { Accept: "application/json" },
                      },
                    );
                    if (!response.ok) {
                      setStatus(state, `Delete failed: ${await responseErrorMessage(response)}`, "error");
                      return;
                    }
                    setStatus(state, "Annotation deleted.", "success");
                    await refreshAnnotations(state);
                  };
                  const init = async (config) => {
                    const canvas = get(config.canvasId);
                    const container = get(config.containerId);
                    if (!canvas || !container) {
                      return;
                    }
                    destroyInstance(config.containerId);
                    const state = {
                      config,
                      container,
                      canvas,
                      map: window.L.map(canvas, {
                        crs: window.L.CRS.Simple,
                        center: [0, 0],
                        zoom: 0,
                        minZoom: 0,
                        maxZoom: 8,
                        attributionControl: false,
                        preferCanvas: true,
                        noWrap: true,
                      }),
                      manifest: null,
                      worldByName: new Map(),
                      currentWorld: null,
                      tileLayer: null,
                      squaremapLayer: window.L.layerGroup(),
                      playerLayer: window.L.layerGroup(),
                      annotationLayer: window.L.layerGroup(),
                      previewLayerGroup: window.L.layerGroup(),
                      previewLayer: null,
                      pendingLine: [],
                      tileRevision: 0,
                      lastTileRefreshAt: null,
                      tool: "pan",
                      worldSelect: get(config.worldSelectId),
                      labelInput: get(config.labelInputId),
                      colorInput: get(config.colorInputId),
                      snapToggle: get(config.snapToggleId),
                      statusEl: get(config.statusId),
                      noticeEl: get(config.noticeId),
                      modeEl: get(config.modeId),
                      finishButton: get(config.finishButtonId),
                      cancelButton: get(config.cancelButtonId),
                      refreshButton: get(config.refreshButtonId),
                      toolButtons: {
                        pan: get(config.panButtonId),
                        marker: get(config.markerButtonId),
                        line: get(config.lineButtonId),
                      },
                      pollTimer: null,
                      syncPromise: null,
                      visibilityTimer: null,
                      iconBaseUrl: "",
                      squaremapSource: MAP_SOURCE_LIVE,
                      lastSquaremapCacheUpdatedAtUnixMs: null,
                    };
                    state.squaremapLayer.addTo(state.map);
                    state.playerLayer.addTo(state.map);
                    state.annotationLayer.addTo(state.map);
                    state.previewLayerGroup.addTo(state.map);
                    instances.set(config.containerId, state);
                    bindControls(state);
                    setToolState(state, state.config.canWrite ? "pan" : "pan");
                    state.map.on("click", (event) => {
                      if (!state.config.canWrite || !state.currentWorld) {
                        return;
                      }
                      const rawPoint = toPoint(state.currentWorld, event.latlng);
                      if (state.tool === "marker") {
                        void createMarkerAnnotation(state, rawPoint).catch((error) => setStatus(state, error.message, "error"));
                        return;
                      }
                      if (state.tool !== "line") {
                        return;
                      }
                      const anchor = state.pendingLine[state.pendingLine.length - 1];
                      const snappedPoint =
                        anchor && state.snapToggle?.checked ? snapLinePoint(anchor, rawPoint) : rawPoint;
                      state.pendingLine.push(snappedPoint);
                      updatePreview(state);
                      setToolState(state, "line");
                    });
                    state.map.on("mousemove", (event) => {
                      if (!state.config.canWrite || !state.currentWorld || state.tool !== "line" || state.pendingLine.length === 0) {
                        return;
                      }
                      const rawPoint = toPoint(state.currentWorld, event.latlng);
                      const anchor = state.pendingLine[state.pendingLine.length - 1];
                      const snappedPoint =
                        anchor && state.snapToggle?.checked ? snapLinePoint(anchor, rawPoint) : rawPoint;
                      updatePreview(state, snappedPoint);
                    });
                    state.visibilityTimer = window.setInterval(() => {
                      if (state.container.offsetParent !== null) {
                        state.map.invalidateSize(false);
                      }
                    }, 1000);
                    state.pollTimer = window.setInterval(() => {
                      void runMapSync(state, () =>
                        state.manifest && state.currentWorld
                          ? refreshAll(state)
                          : bootstrapMap(state, { preserveView: true }),
                      ).catch((error) => handleBootstrapFailure(state, error));
                    }, 5000);
                    try {
                      await runMapSync(state, () => bootstrapMap(state, { preserveView: false }));
                    } catch (error) {
                      handleBootstrapFailure(state, error);
                    }
                  };
                  return { mount: (config) => waitForLeaflet(() => void init(config)), deleteAnnotation };
                })();
              }
            </script>
            """.replace("__LEAFLET_CSS__", _leaflet_vendor_asset("leaflet.css")).replace(
            "__LEAFLET_JS__", _leaflet_vendor_asset("leaflet.js")
        )

    @staticmethod
    def _map_client_bootstrap_script(*, config_payload: dict[str, object]) -> str:
        return """
            (() => {
              const config = __CONFIG__;
              if (!window.modWebMap || typeof window.modWebMap.mount !== "function") {
                return;
              }
              window.modWebMap.mount(config);
            })();
            """.replace("__CONFIG__", json.dumps(config_payload))

    @staticmethod
    def _section_badge_rows(
        badges: tuple[_ModWebBadgeSpec, ...],
        *,
        row_count: int = 2,
    ) -> tuple[tuple[_ModWebBadgeSpec, ...], ...]:
        if row_count < 1:
            raise ValueError("Section badge rows require at least one row.")
        badge_rows: list[list[_ModWebBadgeSpec]] = [[] for _ in range(row_count)]
        for badge_index, badge in enumerate(badges):
            badge_rows[badge_index % row_count].append(badge)
        return tuple(
            tuple[_ModWebBadgeSpec, ...](badge_row)
            for badge_row in badge_rows
            if badge_row
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
                                            if chat_surface.map_url is not None:
                                                self._badge_link(
                                                    ui=ui,
                                                    text="Map",
                                                    tone="purple",
                                                    url=chat_surface.map_url,
                                                    new_tab=True,
                                                )
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
                                    for badge_row in self._section_badge_rows(section_badges):
                                        with ui.row().classes("mod-section-chrome-badge-row items-start justify-end gap-2"):
                                            for badge in badge_row:
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

    def _render_blueprints_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        tab: ModWebAppTabDefinition,
    ) -> None:
        del tab
        self._render_blueprints_editor(ui=ui, model=model, user=user)
        return None

    def _render_mods_section(
        self,
        *,
        ui: ModWebUi,
        model: ModWebPageModel,
        user: ModWebUser,
    ) -> None:
        selected_mod_names: set[str] = set[str]()
        checkboxes: dict[str, Checkbox] = {}
        mod_options = self._mod_options(model.mods.mods)
        show_search: bool = len(mod_options) > 1
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
                if not model.mods.mods:
                    ui.label("No mods are currently indexed for this app.").classes(
                        "mod-subtitle text-sm mod-tab-empty-detail"
                    )
                    if can_upload_mod:
                        ui.label("Upload a mod to seed this app.").classes("mod-subtitle text-sm mod-tab-empty-detail")
                    return

                @ui.refreshable
                def _mod_download_rows(search_query: str) -> None:
                    filtered_mods = self._filter_mod_entries(
                        mods=model.mods.mods,
                        options=mod_options,
                        search_query=search_query,
                    )
                    checkboxes.clear()
                    if not filtered_mods:
                        with ui.card().classes("mod-setting-card locked w-full"):
                            ui.label("No mods match that search.").classes("mod-subtitle text-sm")
                        return

                    with ui.column().classes("w-full mod-list"):

                        def _create_mod_selection_handler(mod_name: str) -> Callable[[ModWebValueContainer], None]:
                            def _handle_mod_selection_change(event: ModWebValueContainer) -> None:
                                set_selected(mod_name, bool(_value_as_object(event)))

                            return _handle_mod_selection_change

                        for entry in filtered_mods:
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
                                checkbox.set_value(entry.name in selected_mod_names)

                def _refresh_mod_rows(event: ModWebEventArgumentsContainer) -> None:
                    _mod_download_rows.refresh(_event_args_as_text(event))

                toolbar_bindings: _ModWebModToolbarBindings = self._render_mod_toolbar(
                    ui=ui,
                    model=model,
                    user=user,
                    toggle_selection=toggle_selection,
                    download_selected=download_selected,
                    upload_mod=upload_dialog.open,
                    show_search=show_search,
                    on_search=_refresh_mod_rows if show_search else None,
                )
                selection_button: Button | None = toolbar_bindings.selection_button
                download_button: Button | None = toolbar_bindings.download_button
                update_count()

                _mod_download_rows("")
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
        show_search: bool = False,
        on_search: Callable[[ModWebEventArgumentsContainer], None] | None = None,
    ) -> _ModWebModToolbarBindings:
        can_upload_mod: bool = upload_mod is not None and self._user_has_level(user, Power_Level.user)
        show_bulk_mod_actions: bool = bool(model.mods.mods)
        if not can_upload_mod and not show_bulk_mod_actions and not show_search:
            return _ModWebModToolbarBindings(selection_button=None, download_button=None)

        selection_button: Button | None = None
        download_button: Button | None = None

        with ui.row().classes("mod-tab-toolbar mod-mods-toolbar w-full"):
            if show_search:
                if on_search is None:
                    raise ValueError("Mod search handler is not available.")
                search_input: Input = (
                    ui.input(placeholder="Search mods")
                    .props("filled square dense clearable hide-bottom-space color=accent")
                    .classes("mod-config-search mod-settings-search mod-mods-toolbar-search")
                )
                search_input.on("update:model-value", on_search)
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
        if summary.non_downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.non_downloadable_count} blocked", tone="warn"))
        if summary.downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.downloadable_count} downloadable", tone="purple"))
        if summary.coremod_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.coremod_count} {coremod_label}", tone="red"))
        return tuple[_ModWebBadgeSpec, ...](badges)
