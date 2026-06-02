from __future__ import annotations

from .runtime_imports import (
    AbstractEventLoop,
    Awaitable,
    BadgeTone,
    Callable,
    Card,
    Label,
    LiteralString,
    ManagedApp,
    ModWebUser,
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeStateStreamEvent,
    Request,
    Timer,
    asyncio,
    config,
    mod_web_badge_class,
    quote,
    replace,
)
from .constants import (
    _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
    _SAME_ORIGIN_NODE_API_BASE,
    _SAME_ORIGIN_NODE_PROXY_BASE,
)
from .nicegui_protocols import AsyncRefresh, ModWebUi
from .types import (
    ModWebAppLink,
    ModWebBasePageModel,
    ModWebHomeNodeSummary,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebNodeStatus,
    ModWebTitleStat,
    _ModWebAppRuntimeState,
    _ModWebBadgeSpec,
    _ModWebLinkSpec,
)

from .service_base import ModWebServiceSupport
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nicegui.element import Element

class ModWebHomeMixin(ModWebServiceSupport):
    def _home_app_card_target(
        self,
        *,
        app: ModWebAppLink,
        user: ModWebUser | None = None,
        show_api_actions: bool,
    ) -> str | None:
        del user
        if app.url:
            return self._app_list_view_url(app.url, show_api_actions=show_api_actions)
        return None

    async def _render_home_page(
        self,
        *,
        ui: ModWebUi,
        user: ModWebUser,
        request: Request,
        show_api_actions: bool,
    ) -> None:
        self._apply_theme(ui=ui)
        request_path: str = self._request_path(request)
        simulated_down_node_names: tuple[str, ...] = self._simulated_down_node_names(request)
        sections: tuple[ModWebNodeAppSection, ...] = await self._home_app_sections(
            user, simulated_down_node_names=simulated_down_node_names
        )
        home_node_summaries: tuple[ModWebHomeNodeSummary, ...] = await self._home_node_summaries(
            sections=sections, user=user
        )
        node_order: tuple[str, ...] = tuple[str, ...](section.node.node_name for section in sections)
        sections_by_node: dict[str, ModWebNodeAppSection] = {section.node.node_name: section for section in sections}
        summaries_by_node: dict[str, ModWebHomeNodeSummary] = {
            summary.node.node_name: summary for summary in home_node_summaries
        }
        dev_mode_enabled: bool = config.INDEV

        def _current_sections() -> tuple[ModWebNodeAppSection, ...]:
            return tuple[ModWebNodeAppSection, ...](sections_by_node[node_name] for node_name in node_order)

        def _current_summaries() -> tuple[ModWebHomeNodeSummary, ...]:
            return tuple[ModWebHomeNodeSummary, ...](
                summaries_by_node[node_name] for node_name in node_order if node_name in summaries_by_node
            )

        async def _refresh_title_stats() -> tuple[ModWebTitleStat, ...]:
            refreshed_summaries: tuple[ModWebHomeNodeSummary, ...] = await self._home_node_summaries(
                sections=_current_sections(), user=user
            )
            return self._build_home_title_stats(refreshed_summaries)

        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_user_header(ui=ui, user=user)
            with ui.card().classes(self._hero_card_classes()):
                with ui.column().classes(self._hero_shell_classes()):
                    with ui.row().classes(self._hero_header_classes()):
                        with ui.column().classes(self._hero_header_main_classes()):
                            ui.label("Yukibot Dashboard").classes(self._hero_title_classes())
                        with ui.column().classes(self._hero_badges_classes(wide=True)):

                            @ui.refreshable
                            def _render_home_badges(current_sections: tuple[ModWebNodeAppSection, ...]) -> None:
                                app_links: tuple[ModWebAppLink, ...] = tuple[ModWebAppLink, ...](
                                    app for section in current_sections for app in section.app_links
                                )
                                unavailable_sections: tuple[ModWebNodeAppSection, ...] = tuple(
                                    section for section in current_sections if section.error is not None
                                )
                                with ui.row().classes(self._hero_badge_row_classes()):
                                    for section in current_sections:
                                        badge_toggle_url: str | None = None
                                        badge_tooltip: str | None = None
                                        if dev_mode_enabled and not section.node.is_current:
                                            badge_toggle_url = self._toggle_simulated_down_node_url(
                                                current_url=request_path,
                                                node_name=section.node.node_name,
                                                simulated_down_node_names=simulated_down_node_names,
                                            )
                                            badge_tooltip = (
                                                "Restore this simulated outage."
                                                if section.is_simulated_down
                                                else "Simulate this node going down."
                                            )
                                        self._interactive_badge(
                                            ui=ui,
                                            text=self._node_status_badge_text(section),
                                            tone=self._node_status_badge_tone(section),
                                            url=badge_toggle_url,
                                            tooltip_text=badge_tooltip,
                                        )
                                with ui.row().classes(self._hero_badge_row_classes()):
                                    self._badge(ui=ui, text=f"{len(current_sections)} nodes", tone="black")
                                    self._badge(ui=ui, text=f"{len(app_links)} apps", tone="black")
                                    self._badge(
                                        ui=ui,
                                        text=f"{sum(1 for app in app_links if app.supports_mods)} mod-enabled",
                                        tone="purple",
                                    )
                                    self._badge(
                                        ui=ui,
                                        text=f"{sum(1 for app in app_links if app.supports_saves)} save-enabled",
                                        tone="black",
                                    )
                                    self._badge(
                                        ui=ui,
                                        text=f"{sum(1 for app in app_links if app.supports_configs)} config-enabled",
                                        tone="grey",
                                    )
                                    self._badge(
                                        ui=ui,
                                        text=(
                                            f"{sum(1 for app in app_links if app.supports_console_actions)} "
                                            "console-enabled"
                                        ),
                                        tone="black",
                                    )
                                    if unavailable_sections:
                                        self._badge(
                                            ui=ui,
                                            text=f"{len(unavailable_sections)} unavailable",
                                            tone="red",
                                        )

                            _render_home_badges(_current_sections())
                    apply_title_stats: Callable[[tuple[ModWebTitleStat, ...]], None] = self._render_live_title_stats(
                        ui=ui,
                        initial_stats=self._build_home_title_stats(home_node_summaries),
                        refresh_async_stats=_refresh_title_stats,
                    )

            @ui.refreshable
            def _render_home_sections(current_sections: tuple[ModWebNodeAppSection, ...]) -> None:
                self._render_home_page_sections(
                    ui=ui,
                    sections=current_sections,
                    user=user,
                    show_api_actions=show_api_actions,
                )

            _render_home_sections(_current_sections())

            def _apply_sections(updated_sections: tuple[ModWebNodeAppSection, ...]) -> None:
                nonlocal sections_by_node, summaries_by_node
                previous_sections: tuple[ModWebNodeAppSection, ...] = _current_sections()
                for section in updated_sections:
                    previous_section: ModWebNodeAppSection | None = sections_by_node.get(section.node.node_name)
                    section_app_links = section.app_links
                    if previous_section is not None:
                        section_app_links: tuple[ModWebAppLink, ...] = self._mark_runtime_changes(
                            previous_apps=previous_section.app_links,
                            updated_apps=section.app_links,
                        )
                    section: ModWebNodeAppSection = replace(section, app_links=section_app_links)
                    sections_by_node[section.node.node_name] = section
                    existing_summary: ModWebHomeNodeSummary | None = summaries_by_node.get(section.node.node_name)
                    summaries_by_node[section.node.node_name] = ModWebHomeNodeSummary(
                        node=section.node,
                        app_count=len(section.app_links),
                        system_summary=(
                            None
                            if section.error is not None
                            else None
                            if existing_summary is None
                            else existing_summary.system_summary
                        ),
                    )
                current_sections: tuple[ModWebNodeAppSection, ...] = _current_sections()
                if not self._sections_equal_for_card_render(previous_sections, current_sections):
                    _render_home_badges.refresh(current_sections)
                    _render_home_sections.refresh(current_sections)

            refresh_sections: AsyncRefresh = self._build_async_refreshable_updater(
                refresh_async_value=lambda: self._home_app_sections(
                    user,
                    simulated_down_node_names=simulated_down_node_names,
                ),
                apply_value=_apply_sections,
                error_context="Mod web home app sections",
            )
            refresh_sections_timer: Timer = ui.timer(
                _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
                lambda: asyncio.create_task(refresh_sections()),
            )
            self._register_timer_cleanup(ui=ui, timer=refresh_sections_timer)
            page_closed = False
            loop: AbstractEventLoop = asyncio.get_running_loop()
            unsubscribes: list[Callable[[], None]] = []

            def _apply_node_update(node: ModWebNodeLink, event: NodeStateStreamEvent) -> None:
                if page_closed:
                    return
                previous_sections: tuple[ModWebNodeAppSection, ...] = _current_sections()
                current_section = sections_by_node.get(
                    node.node_name,
                    ModWebNodeAppSection(node=node, app_links=(), error="Unavailable"),
                )
                if event.app_entries is not None:
                    updated_app_links: tuple[ModWebAppLink, ...] = tuple[ModWebAppLink, ...](
                        self._app_link_from_entry(entry=entry, user=user, node_name=node.node_name)
                        for entry in event.app_entries
                    )
                    current_section: ModWebNodeAppSection = ModWebNodeAppSection(
                        node=node,
                        app_links=self._mark_runtime_changes(
                            previous_apps=current_section.app_links,
                            updated_apps=updated_app_links,
                        ),
                        error=None,
                    )
                sections_by_node[node.node_name] = current_section
                existing_summary: ModWebHomeNodeSummary | None = summaries_by_node.get(node.node_name)
                summaries_by_node[node.node_name] = ModWebHomeNodeSummary(
                    node=node,
                    app_count=len(current_section.app_links),
                    system_summary=(
                        event.system_summary
                        if event.system_summary is not None
                        else None
                        if current_section.error is not None
                        else None
                        if existing_summary is None
                        else existing_summary.system_summary
                    ),
                )
                current_sections = _current_sections()
                if not self._sections_equal_for_card_render(previous_sections, current_sections):
                    _render_home_badges.refresh(current_sections)
                    _render_home_sections.refresh(current_sections)
                apply_title_stats(self._build_home_title_stats(_current_summaries()))

            def _node_state_callback(node: ModWebNodeLink) -> Callable[[NodeStateStreamEvent], None]:
                def _handle_event(event: NodeStateStreamEvent) -> None:
                    loop.call_soon_threadsafe(lambda: _apply_node_update(node, event))

                return _handle_event

            for section in _current_sections():
                if section.is_simulated_down:
                    continue
                if section.node.is_current:
                    unsubscribe = self._node_api.subscribe_local_node_state(
                        _node_state_callback(section.node)
                    )
                else:
                    unsubscribe: Callable[[], None] = self._create_remote_node_state_subscription(
                        node=section.node,
                        user=user,
                        on_update=_node_state_callback(section.node),
                    )
                unsubscribes.append(unsubscribe)

            def _cleanup_live_updates() -> None:
                nonlocal page_closed
                page_closed = True
                for unsubscribe in unsubscribes:
                    unsubscribe()

            self._register_client_cleanup(ui=ui, cleanup=_cleanup_live_updates)

    def _render_home_page_sections(
        self,
        *,
        ui: ModWebUi,
        sections: tuple[ModWebNodeAppSection, ...],
        user: ModWebUser,
        show_api_actions: bool,
    ) -> None:
        del user
        app_links: tuple[ModWebAppLink, ...] = tuple[ModWebAppLink, ...](
            app for section in sections for app in section.app_links
        )
        has_unavailable_sections: bool = any(section.error is not None for section in sections)
        if not app_links and not has_unavailable_sections:
            with ui.card().classes("mod-card mod-card-empty w-full"):
                ui.label("No apps are currently available.").classes("p-8 text-lg mod-subtitle")
            return

        for section in sections:
            with ui.column().classes("w-full gap-3"):
                node_text_style: str | None = self._node_text_style(node_name=section.node.node_name)
                with ui.row().classes("w-full items-center justify-between gap-1 flex-wrap"):
                    with ui.column().classes("gap-1"):
                        section_title: Label = ui.label(section.node.label).classes("text-xl font-bold mod-title-small")
                        if node_text_style is not None:
                            section_title.style(node_text_style)
                        node_subtitle: str | None = self._node_display_subtitle(
                            label=section.node.label,
                            node_name=section.node.node_name,
                        )
                        if node_subtitle is not None:
                            subtitle: Label = ui.label(node_subtitle).classes("text-sm mod-subtitle")
                            if node_text_style is not None:
                                subtitle.style(node_text_style)
                    with ui.row().classes("gap-2 flex-wrap"):
                        self._badge(ui=ui, text=f"{len(section.app_links)} apps", tone="black")
                        if section.error is not None:
                            self._badge(ui=ui, text="Unavailable", tone="red")
                if section.error is not None:
                    self._render_node_unavailable_card(ui=ui, message=section.error)
                elif not section.app_links:
                    with ui.card().classes("mod-card mod-card-empty w-full"):
                        ui.label("No apps are currently available on this node.").classes("p-5 text-sm mod-subtitle")
                else:
                    for app in section.app_links:
                        card_target: str | None = self._home_app_card_target(
                            app=app,
                            show_api_actions=show_api_actions,
                        )
                        card: Card = (
                            ui.card().classes(self._app_card_link_classes(app)).style(self._app_card_link_style(app))
                        )
                        if card_target is not None:
                            card.on("click", lambda _=None, target_url=card_target: ui.navigate.to(target_url))
                        with card:
                            self._render_app_card_content(
                                ui=ui,
                                app=app,
                                show_api_actions=show_api_actions,
                            )

    def _render_node_apps_page(
        self,
        *,
        ui: ModWebUi,
        node: ModWebNodeLink,
        app_links: tuple[ModWebAppLink, ...],
        user: ModWebUser,
        show_api_actions: bool,
        initial_title_stats: tuple[ModWebTitleStat, ...],
        refresh_async_title_stats: Callable[[], Awaitable[tuple[ModWebTitleStat, ...]]] | None = None,
        subscribe_node_state_updates: Callable[
            [Callable[[NodeStateStreamEvent], None]],
            Callable[[], None],
        ]
        | None = None,
    ) -> None:
        self._apply_theme(ui=ui)
        current_app_links: tuple[ModWebAppLink, ...] = app_links
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_user_header(ui=ui, user=user)
            with ui.card().classes(self._hero_card_classes()):
                with ui.column().classes(self._hero_shell_classes()):
                    with ui.row().classes(self._hero_header_classes()):
                        with ui.column().classes(self._hero_header_main_classes()):
                            node_text_style: str | None = self._node_text_style(node_name=node.node_name)
                            node_title: Label = ui.label(node.label).classes(self._hero_title_classes())
                            if node_text_style is not None:
                                node_title.style(node_text_style)
                            node_subtitle: Label = ui.label(node.node_name).classes(self._hero_support_classes())
                            if node_text_style is not None:
                                node_subtitle.style(node_text_style)
                        with ui.column().classes(self._hero_badges_classes()):

                            @ui.refreshable
                            def _render_node_badges(current_app_links: tuple[ModWebAppLink, ...]) -> None:
                                with ui.row().classes(self._hero_badge_row_classes()):
                                    self._badge(ui=ui, text=f"{len(current_app_links)} apps", tone="black")
                                    self._badge(
                                        ui=ui,
                                        text=f"{sum(1 for app in current_app_links if app.supports_mods)} mod-enabled",
                                        tone="purple",
                                    )
                                    self._badge(
                                        ui=ui,
                                        text=f"{sum(1 for app in current_app_links if app.supports_saves)} save-enabled",
                                        tone="black",
                                    )
                                    self._badge(
                                        ui=ui,
                                        text=f"{sum(1 for app in current_app_links if app.supports_configs)} config-enabled",
                                        tone="grey",
                                    )
                                    self._badge(
                                        ui=ui,
                                        text=(
                                            f"{sum(1 for app in current_app_links if app.supports_console_actions)} "
                                            "console-enabled"
                                        ),
                                        tone="black",
                                    )

                            _render_node_badges(current_app_links)
                    with ui.row().classes(self._hero_action_row_classes()):
                        self._action_link(
                            ui=ui,
                            label="Home",
                            url=self._app_list_view_url(self.index_path(), show_api_actions=show_api_actions),
                            compact=True,
                        )
                    apply_title_stats: Callable[[tuple[ModWebTitleStat, ...]], None] = self._render_live_title_stats(
                        ui=ui,
                        initial_stats=initial_title_stats,
                        refresh_async_stats=refresh_async_title_stats,
                    )

            @ui.refreshable
            def _render_node_cards(current_app_links: tuple[ModWebAppLink, ...]) -> None:
                if not current_app_links:
                    with ui.card().classes("mod-card mod-card-empty w-full"):
                        ui.label("No apps are currently available on this node.").classes("p-8 text-lg mod-subtitle")
                    return

                with ui.column().classes("w-full gap-3"):
                    for app in current_app_links:
                        card_target: str = self._app_list_view_url(app.url, show_api_actions=show_api_actions)
                        card: Card = (
                            ui.card().classes(self._app_card_link_classes(app)).style(self._app_card_link_style(app))
                        )
                        card.on("click", lambda _=None, target_url=card_target: ui.navigate.to(target_url))
                        with card:
                            self._render_app_card_content(ui=ui, app=app, show_api_actions=show_api_actions)

            _render_node_cards(current_app_links)
            if subscribe_node_state_updates is not None:
                page_closed = False
                loop: AbstractEventLoop = asyncio.get_running_loop()

                def _apply_update(event: NodeStateStreamEvent) -> None:
                    nonlocal current_app_links
                    if page_closed:
                        return
                    if event.app_entries is not None:
                        updated_app_links: tuple[ModWebAppLink, ...] = tuple[ModWebAppLink, ...](
                            self._app_link_from_entry(entry=entry, user=user, node_name=node.node_name)
                            for entry in event.app_entries
                        )
                        current_app_links = self._mark_runtime_changes(
                            previous_apps=current_app_links,
                            updated_apps=updated_app_links,
                        )
                        _render_node_badges.refresh(current_app_links)
                        _render_node_cards.refresh(current_app_links)
                    if event.system_summary is not None:
                        apply_title_stats(self._build_system_title_stats(event.system_summary))

                def _handle_update(event: NodeStateStreamEvent) -> None:
                    loop.call_soon_threadsafe(lambda: _apply_update(event))

                unsubscribe: Callable[[], None] = subscribe_node_state_updates(_handle_update)

                def _cleanup_live_updates() -> None:
                    nonlocal page_closed
                    page_closed = True
                    unsubscribe()

                self._register_client_cleanup(ui=ui, cleanup=_cleanup_live_updates)

    @staticmethod
    def _node_display_subtitle(*, label: str, node_name: str) -> str | None:
        if label.strip().casefold() == node_name.strip().casefold():
            return None
        return node_name

    @staticmethod
    def _node_status_badge_text(section: ModWebNodeAppSection) -> str:
        if section.is_simulated_down:
            return f"{section.node.label}: Simulated Down"
        return f"{section.node.label}: {'Alive' if section.error is None else 'Down'}"

    @staticmethod
    def _node_status_badge_tone(section: ModWebNodeAppSection) -> BadgeTone:
        if section.error is not None:
            return "red"
        if section.node.is_current:
            return "purple"
        return "grey"

    @staticmethod
    def _login_node_status_badge_text(status: ModWebNodeStatus) -> str:
        if status.is_simulated_down:
            return f"{status.node.label}: Simulated Down"
        return f"{status.node.label}: {'Alive' if status.alive else 'Down'}"

    @staticmethod
    def _login_node_status_badge_tone(status: ModWebNodeStatus) -> BadgeTone:
        if not status.alive:
            return "red"
        if status.node.is_current:
            return "purple"
        return "grey"

    def _render_app_card_content(
        self,
        *,
        ui: ModWebUi,
        app: ModWebAppLink,
        show_api_actions: bool = False,
    ) -> None:
        runtime_badge: _ModWebBadgeSpec | None = self._app_card_runtime_badge(app)
        with ui.row().classes("mod-app-card-shell w-full items-center justify-between gap-2 p-3 flex-wrap"):
            with ui.column().classes("mod-app-card-main min-w-0 gap-1"):
                title_label = ui.label(app.friendly).classes(self._app_card_title_classes(app))
                self._attach_text_tooltip(ui=ui, target=title_label, text=app.friendly)
                ui.label("").classes("mod-app-card-subtitle text-sm mod-subtitle mod-app-card-subtitle-empty")
            with ui.row().classes("mod-app-card-actions items-center justify-end gap-3 flex-wrap"):
                if runtime_badge is not None:
                    runtime_badge_classes = "mod-app-runtime-chip"
                    if app.runtime_changed:
                        runtime_badge_classes: LiteralString = f"{runtime_badge_classes} mod-app-runtime-chip-live"
                    self._badge(
                        ui=ui,
                        text=runtime_badge.text,
                        tone=runtime_badge.tone,
                        extra_classes=runtime_badge_classes,
                    )
                with ui.row().classes("mod-app-card-badges items-center gap-3 flex-wrap"):
                    for badge in self._app_card_badges(app):
                        self._badge(ui=ui, text=badge.text, tone=badge.tone)
                if app.chat_url is not None:
                    self._action_link(
                        ui=ui,
                        label="Chat",
                        url=self._app_list_view_url(app.chat_url, show_api_actions=show_api_actions),
                        compact=True,
                        extra_classes="mod-action-border-accent",
                        stop_propagation=True,
                        new_tab=True,
                    )
                api_actions: tuple[_ModWebLinkSpec, ...] | tuple[()] = (
                    self._app_card_api_actions(app) if show_api_actions else ()
                )
                if api_actions:
                    self._render_app_card_api_pill(ui=ui, actions=api_actions)

    @staticmethod
    def _app_card_badges(app: ModWebAppLink) -> tuple[_ModWebBadgeSpec, ...]:
        badges: list[_ModWebBadgeSpec] = []
        if app.supports_saves:
            badges.append(_ModWebBadgeSpec(text="Saves", tone="black"))
        if app.supports_configs:
            badges.append(_ModWebBadgeSpec(text="Configs", tone="black"))
        if app.supports_settings:
            badges.append(_ModWebBadgeSpec(text="Settings", tone="black"))
        if app.supports_console_actions:
            badges.append(_ModWebBadgeSpec(text="Console", tone="black"))
        if app.supports_mods:
            badges.append(_ModWebBadgeSpec(text="Mods", tone="purple"))
        if app.supports_chat:
            badges.append(_ModWebBadgeSpec(text="Chat", tone="purple"))
        return tuple[_ModWebBadgeSpec, ...](badges)

    @staticmethod
    def _player_count_snapshot_text(*, player_count: int | None, player_capacity: int | None) -> str | None:
        if player_count is None or player_capacity is None:
            return None
        return f"{player_count} / {player_capacity}"

    @classmethod
    def _app_card_player_count_subtext(cls, app: ModWebAppLink) -> str | None:
        if app.transition_state is NodeAppTransitionState.STOPPING:
            return "Stopping"
        if app.transition_state is NodeAppTransitionState.STARTING:
            return "Starting"
        if not app.running:
            return None
        player_snapshot_text: str | None = cls._player_count_snapshot_text(
            player_count=app.player_count,
            player_capacity=app.player_capacity,
        )
        if player_snapshot_text is not None:
            return f"{player_snapshot_text} players"
        return "Running"

    @classmethod
    def _chat_player_count_badge(cls, app_stats: NodeAppRuntimeSummary | None) -> _ModWebBadgeSpec | None:
        if app_stats is None:
            return None
        player_snapshot_text: str | None = cls._player_count_snapshot_text(
            player_count=app_stats.player_count,
            player_capacity=app_stats.player_capacity,
        )
        if player_snapshot_text is None:
            return None
        if app_stats.player_count is None:
            raise RuntimeError("Player count unexpectedly missing for chat player badge.")
        return _ModWebBadgeSpec(
            text=player_snapshot_text,
            tone="purple" if app_stats.player_count > 0 else "grey",
        )

    @staticmethod
    def _set_badge_state(label: Label, text: str, tone: BadgeTone, *, extra_classes: str = "") -> None:
        label.set_text(text)
        label.classes(replace=f"{mod_web_badge_class(tone)} {extra_classes}".strip())
        label.style(remove="display: none;")

    @classmethod
    def _set_optional_badge_state(
        cls,
        label: Label,
        badge: _ModWebBadgeSpec | None,
        *,
        extra_classes: str = "",
    ) -> None:
        if badge is None:
            label.style(add="display: none;")
            return
        cls._set_badge_state(label, badge.text, badge.tone, extra_classes=extra_classes)

    @staticmethod
    def _app_card_runtime_badge(app: ModWebAppLink) -> _ModWebBadgeSpec | None:
        if app.transition_state is NodeAppTransitionState.STOPPING:
            return _ModWebBadgeSpec(text="Stopping", tone="warn")
        if app.transition_state is NodeAppTransitionState.STARTING:
            return _ModWebBadgeSpec(text="Starting", tone="purple")
        if app.running:
            player_snapshot_text: str | None = ModWebHomeMixin._player_count_snapshot_text(
                player_count=app.player_count,
                player_capacity=app.player_capacity,
            )
            if player_snapshot_text is not None:
                if app.player_count is None:
                    raise RuntimeError("Player count unexpectedly missing for runtime badge.")
                return _ModWebBadgeSpec(
                    text=player_snapshot_text,
                    tone="purple" if app.player_count > 0 else "black",
                )
            return _ModWebBadgeSpec(text="Running", tone="black")
        if app.enabled:
            return None
        return _ModWebBadgeSpec(text="Disabled", tone="red")

    @staticmethod
    def _app_card_api_actions(app: ModWebAppLink) -> tuple[_ModWebLinkSpec, ...]:
        actions: list[_ModWebLinkSpec] = []
        if app.supports_mods:
            if app.api_url is None:
                raise ValueError("App mod support requires an api_url.")
            actions.append(_ModWebLinkSpec(label="Mods", url=app.api_url))
        elif app.api_url is not None:
            raise ValueError("Unexpected api_url for app without mod support.")
        if app.supports_configs:
            if app.configs_api_url is not None:
                actions.append(_ModWebLinkSpec(label="Configs", url=app.configs_api_url))
        elif app.configs_api_url is not None:
            raise ValueError("Unexpected configs_api_url for app without config support.")
        if app.supports_saves:
            if app.saves_api_url is None:
                raise ValueError("App save support requires a saves_api_url.")
            actions.append(_ModWebLinkSpec(label="Saves", url=app.saves_api_url))
        elif app.saves_api_url is not None:
            raise ValueError("Unexpected saves_api_url for app without save support.")
        if app.supports_settings:
            if app.settings_api_url is None:
                raise ValueError("App settings support requires a settings_api_url.")
            actions.append(_ModWebLinkSpec(label="Settings", url=app.settings_api_url))
        elif app.settings_api_url is not None:
            raise ValueError("Unexpected settings_api_url for app without settings support.")
        return tuple[_ModWebLinkSpec, ...](actions)

    def _render_app_card_api_pill(self, *, ui: ModWebUi, actions: tuple[_ModWebLinkSpec, ...]) -> None:
        with ui.element("div").classes("mod-app-card-api-pill"):
            for index, action in enumerate[_ModWebLinkSpec](actions):
                if index > 0:
                    ui.element("span").classes("mod-app-card-api-separator")
                ui.link(action.label, action.url).classes("mod-app-card-api-link").on(
                    "click",
                    js_handler="(event) => event.stopPropagation()",
                )

    @staticmethod
    def _attach_text_tooltip(*, ui: ModWebUi, target: "Element", text: str) -> None:
        with target:
            ui.tooltip(text)

    @staticmethod
    def _app_card_link_classes(app: ModWebAppLink) -> str:
        classes = "mod-card mod-app-card mod-app-card-link w-full"
        if app.running:
            classes = f"{classes} mod-app-card-running"
        if app.runtime_changed:
            classes: LiteralString = f"{classes} mod-app-card-live"
        if not app.enabled:
            return f"{classes} mod-app-card-disabled"
        return classes

    @staticmethod
    def _app_card_link_style(app: ModWebAppLink) -> str:
        if app.color_hex is None:
            return ""
        return f"--mod-app-strip-color: {app.color_hex};"

    @staticmethod
    def _app_card_title_classes(app: ModWebAppLink) -> str:
        classes = "text-xl font-bold mod-title-small mod-app-card-title"
        if not app.enabled:
            return f"{classes} mod-app-card-title-disabled"
        return classes

    @staticmethod
    def _app_runtime_state(app: ModWebAppLink) -> _ModWebAppRuntimeState:
        return _ModWebAppRuntimeState(
            running=app.running,
            enabled=app.enabled,
            transition_state=app.transition_state,
            player_count=app.player_count,
            player_capacity=app.player_capacity,
        )

    @classmethod
    def _with_runtime_change_flag(cls, *, app: ModWebAppLink, previous_app: ModWebAppLink | None) -> ModWebAppLink:
        runtime_changed: bool = previous_app is not None and cls._app_runtime_state(
            previous_app
        ) != cls._app_runtime_state(app)
        return replace(app, runtime_changed=runtime_changed)

    @classmethod
    def _app_link_without_runtime_change_flag(cls, app: ModWebAppLink) -> ModWebAppLink:
        return replace(app, runtime_changed=False)

    @classmethod
    def _section_without_runtime_change_flags(cls, section: ModWebNodeAppSection) -> ModWebNodeAppSection:
        return replace(
            section,
            app_links=tuple[ModWebAppLink, ...](
                cls._app_link_without_runtime_change_flag(app) for app in section.app_links
            ),
        )

    @classmethod
    def _sections_equal_for_card_render(
        cls,
        left: tuple[ModWebNodeAppSection, ...],
        right: tuple[ModWebNodeAppSection, ...],
    ) -> bool:
        return tuple[ModWebNodeAppSection, ...](
            cls._section_without_runtime_change_flags(section) for section in left
        ) == tuple(cls._section_without_runtime_change_flags(section) for section in right)

    @classmethod
    def _mark_runtime_changes(
        cls,
        *,
        previous_apps: tuple[ModWebAppLink, ...],
        updated_apps: tuple[ModWebAppLink, ...],
    ) -> tuple[ModWebAppLink, ...]:
        previous_by_key: dict[tuple[str, str], ModWebAppLink] = {
            (app.node_name.casefold(), app.name.casefold()): app for app in previous_apps
        }
        return tuple[ModWebAppLink, ...](
            cls._with_runtime_change_flag(
                app=app,
                previous_app=previous_by_key.get((app.node_name.casefold(), app.name.casefold())),
            )
            for app in updated_apps
        )

    @staticmethod
    def _app_link_with_runtime(app: ModWebAppLink, app_stats: NodeAppRuntimeSummary) -> ModWebAppLink:
        updated_app: ModWebAppLink = replace(
            app,
            running=app_stats.running,
            enabled=app_stats.enabled,
            transition_state=app_stats.transition_state,
            player_count=app_stats.player_count,
            player_capacity=app_stats.player_capacity,
        )
        return ModWebHomeMixin._with_runtime_change_flag(app=updated_app, previous_app=app)

    @staticmethod
    def _model_with_runtime_state(
        model: ModWebBasePageModel,
        *,
        app_stats: NodeAppRuntimeSummary | None,
        app_start_blocked: bool,
    ) -> ModWebBasePageModel:
        return replace(model, app_stats=app_stats, app_start_blocked=app_start_blocked)

    @staticmethod
    def _is_current_node_name(node_name: str) -> bool:
        return node_name.strip().casefold() == config.MOD_WEB_SERVER.node_name.strip().casefold()

    async def _refresh_runtime_model(self, *, model: ModWebBasePageModel, user: ModWebUser) -> ModWebBasePageModel:
        if self._is_current_node_name(model.node_name):
            app: ManagedApp = self._resolve_app(model.app_name)
            app_stats: NodeAppRuntimeSummary = await self._node_api.build_app_runtime_summary(app)
            return self._model_with_runtime_state(
                model,
                app_stats=app_stats,
                app_start_blocked=self._app_start_blocked_local(app),
            )

        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        app_stats, system_summary = await asyncio.gather(
            asyncio.to_thread(self._remote_app_runtime_summary, node, model.app_name, user),
            asyncio.to_thread(self._remote_node_system_summary, node, user),
        )
        return self._model_with_runtime_state(
            model,
            app_stats=app_stats,
            app_start_blocked=self._app_start_blocked_remote(
                app_friendly=model.app_friendly,
                app_stats=app_stats,
                running_names=system_summary.running_names,
            ),
        )

    def _app_link_from_entry(self, *, entry: NodeAppEntry, user: ModWebUser, node_name: str) -> ModWebAppLink:
        is_current_node: bool = node_name.casefold() == config.MOD_WEB_SERVER.node_name.casefold()
        if is_current_node:
            url = self.app_path(entry.name)
            api_url = (
                self._node_api.list_mods_url(entry.name, base_url=_SAME_ORIGIN_NODE_API_BASE)
                if entry.supports_mods
                else None
            )
            configs_api_url = (
                self._node_api.list_configs_url(entry.name, base_url=_SAME_ORIGIN_NODE_API_BASE)
                if entry.supports_configs and self._user_has_level(user, entry.config_read_level)
                else None
            )
            saves_api_url = (
                self._node_api.list_saves_url(entry.name, base_url=_SAME_ORIGIN_NODE_API_BASE)
                if entry.supports_saves
                else None
            )
            settings_api_url = (
                self._node_api.list_settings_url(entry.name, base_url=_SAME_ORIGIN_NODE_API_BASE)
                if entry.supports_settings
                else None
            )
            chat_url = self.app_chat_path(entry.name) if entry.supports_chat else None
        else:
            encoded_node_name: str = quote(node_name, safe="")
            encoded_app_name: str = quote(entry.name, safe="")
            url: str = self.node_app_path(node_name, entry.name)
            api_url: str | None = (
                f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{encoded_node_name}/apps/{encoded_app_name}/mods"
                if entry.supports_mods
                else None
            )
            configs_api_url: str | None = (
                f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{encoded_node_name}/apps/{encoded_app_name}/configs"
                if entry.supports_configs and self._user_has_level(user, entry.config_read_level)
                else None
            )
            saves_api_url: str | None = (
                f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{encoded_node_name}/apps/{encoded_app_name}/saves"
                if entry.supports_saves
                else None
            )
            settings_api_url: str | None = (
                f"{_SAME_ORIGIN_NODE_PROXY_BASE}/{encoded_node_name}/apps/{encoded_app_name}/settings"
                if entry.supports_settings
                else None
            )
            chat_url: str | None = self.node_app_chat_path(node_name, entry.name) if entry.supports_chat else None
        return ModWebAppLink(
            name=entry.name,
            friendly=entry.friendly,
            node_name=node_name,
            running=entry.running,
            enabled=entry.enabled,
            color_hex=entry.color_hex,
            supports_mods=entry.supports_mods,
            supports_configs=entry.supports_configs,
            supports_saves=entry.supports_saves,
            supports_settings=entry.supports_settings,
            url=url,
            api_url=api_url,
            configs_api_url=configs_api_url,
            transition_state=entry.transition_state,
            player_count=entry.player_count,
            player_capacity=entry.player_capacity,
            saves_api_url=saves_api_url,
            settings_api_url=settings_api_url,
            supports_console_actions=entry.supports_console_actions,
            supports_chat=entry.supports_chat,
            chat_url=chat_url,
        )
