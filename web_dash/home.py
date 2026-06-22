from __future__ import annotations

from typing import TYPE_CHECKING

from . import avatars as mod_web_avatars
from .constants import (
    _APP_SECTION_QUERY_PARAM,
    _SAME_ORIGIN_NODE_API_BASE,
    _SAME_ORIGIN_NODE_PROXY_BASE,
    _TITLE_STATS_REFRESH_INTERVAL_SECONDS,
    log,
)
from .nicegui_protocols import ModWebUi, _value_as_text
from .runtime_imports import (
    AbstractEventLoop,
    AppUpdateInfo,
    AppUpdateStatus,
    Awaitable,
    BadgeTone,
    Button,
    Callable,
    Card,
    Enum,
    Input,
    Label,
    ManagedApp,
    ModWebUser,
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeStateStreamEvent,
    NodeSystemSummary,
    Power_Level,
    Request,
    asyncio,
    cast,
    config,
    dataclass,
    escape,
    mod_web_badge_class,
    quote,
    replace,
)

_KEEP_PAGE_MODEL_VALUE = object()
from .service_base import ModWebServiceSupport
from .types import (
    ModWebAppLink,
    ModWebAppTabDefinition,
    ModWebBasePageModel,
    ModWebHomeNodeSummary,
    ModWebNodeAppSection,
    ModWebNodeLink,
    ModWebNodeStatus,
    ModWebTitleStat,
    _ModWebAppCardBadgeSpec,
    _ModWebAppRuntimeState,
    _ModWebBadgeSpec,
    _ModWebLinkSpec,
    _ModWebNodePresenceBadgeSpec,
)

if TYPE_CHECKING:
    from nicegui.element import Element


@dataclass(frozen=True, slots=True)
class _ModWebHomeMetricSpec:
    label: str
    icon: str
    value: str
    tone: BadgeTone


@dataclass(frozen=True, slots=True)
class _ModWebHomeNodeStatSpec:
    node_name: str
    node_label: str
    node_subtitle: str | None
    status_text: str | None
    status_tone: BadgeTone
    card_tone: BadgeTone
    metrics: tuple[_ModWebHomeMetricSpec, ...]
    running_text: str
    running_tone: BadgeTone
    running_tooltip: str | None = None


class _ModWebNodeSettingsFieldKey(Enum):
    CPU_TOTAL = "cpu_points_total"
    RAM_TOTAL = "ram_points_total"
    CPU_RESERVED = "cpu_points_reserved"
    RAM_RESERVED = "ram_points_reserved"


@dataclass(frozen=True, slots=True)
class _ModWebNodeSettingsNumberFieldSpec:
    key: _ModWebNodeSettingsFieldKey
    label: str
    field_label: str


@dataclass(slots=True)
class _ModWebNodeSettingsPanelState:
    overlay: Element
    title_label: Label
    subtitle_label: Label
    field_inputs: dict[_ModWebNodeSettingsFieldKey, Input]
    google_font_urls_input: Input | None = None
    simulate_button: Button | None = None
    selected_node_name: str | None = None

    def require_selected_node_name(self) -> str:
        if self.selected_node_name is None:
            raise RuntimeError("Node settings panel opened without a selected node.")
        return self.selected_node_name

    def show(self) -> None:
        self.overlay.style(remove="display: none;")

    def hide(self) -> None:
        self.overlay.style(add="display: none;")

    def input_for(self, key: _ModWebNodeSettingsFieldKey) -> Input:
        return self.field_inputs[key]

    def set_capacity_profile(self, capacity: config.NodeCapacityProfile) -> None:
        self.input_for(_ModWebNodeSettingsFieldKey.CPU_TOTAL).set_value(str(capacity.cpu_points_total))
        self.input_for(_ModWebNodeSettingsFieldKey.RAM_TOTAL).set_value(str(capacity.ram_points_total))
        self.input_for(_ModWebNodeSettingsFieldKey.CPU_RESERVED).set_value(str(capacity.cpu_points_reserved))
        self.input_for(_ModWebNodeSettingsFieldKey.RAM_RESERVED).set_value(str(capacity.ram_points_reserved))

    def set_google_font_urls(self, settings: config.NodeFontSourceSettings) -> None:
        if self.google_font_urls_input is None:
            raise RuntimeError("Node settings panel Google font URL input is not available.")
        self.google_font_urls_input.set_value("\n".join(settings.google_font_urls))


_HOME_APPS_ICON: str = "apps"


_NODE_SETTINGS_CAPACITY_FIELD_ROWS: tuple[tuple[_ModWebNodeSettingsNumberFieldSpec, ...], ...] = (
    (
        _ModWebNodeSettingsNumberFieldSpec(
            key=_ModWebNodeSettingsFieldKey.CPU_TOTAL,
            label="CPU Total",
            field_label="CPU points total",
        ),
        _ModWebNodeSettingsNumberFieldSpec(
            key=_ModWebNodeSettingsFieldKey.RAM_TOTAL,
            label="RAM Total",
            field_label="RAM points total",
        ),
    ),
    (
        _ModWebNodeSettingsNumberFieldSpec(
            key=_ModWebNodeSettingsFieldKey.CPU_RESERVED,
            label="CPU Reserved",
            field_label="CPU points reserved",
        ),
        _ModWebNodeSettingsNumberFieldSpec(
            key=_ModWebNodeSettingsFieldKey.RAM_RESERVED,
            label="RAM Reserved",
            field_label="RAM points reserved",
        ),
    ),
)


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
        can_manage_node_configuration: bool = self._user_has_level(user, Power_Level.root)
        simulated_down_keys: set[str] = {node_name.casefold() for node_name in simulated_down_node_names}
        node_settings_panel: _ModWebNodeSettingsPanelState | None = None
        node_dialog_simulate_button: Button | None = None
        node_capacity_inputs: dict[_ModWebNodeSettingsFieldKey, Input] = {}
        node_google_font_urls_input: Input | None = None

        def _current_sections() -> tuple[ModWebNodeAppSection, ...]:
            return tuple[ModWebNodeAppSection, ...](sections_by_node[node_name] for node_name in node_order)

        def _current_summaries() -> tuple[ModWebHomeNodeSummary, ...]:
            return tuple[ModWebHomeNodeSummary, ...](
                summaries_by_node[node_name] for node_name in node_order if node_name in summaries_by_node
            )

        def _parse_required_non_negative_int(*, raw_value: str, field_label: str) -> int:
            value = raw_value.strip()
            if not value:
                raise ValueError(f"{field_label} must not be empty.")
            try:
                parsed = int(value)
            except ValueError as xcp:
                raise ValueError(f"{field_label} must be a whole number.") from xcp
            if parsed < 0:
                raise ValueError(f"{field_label} must not be negative.")
            return parsed

        def _require_node_settings_panel() -> _ModWebNodeSettingsPanelState:
            if node_settings_panel is None:
                raise RuntimeError("Node settings panel is not available.")
            return node_settings_panel

        def _render_node_settings_number_input(*, label: str) -> Input:
            return (
                ui.input(label)
                .props("filled square dense hide-bottom-space color=accent type=number inputmode=numeric step=1 min=0")
                .classes("mod-app-details-field mod-app-details-point-field")
            )

        def _render_node_settings_capacity_inputs() -> dict[_ModWebNodeSettingsFieldKey, Input]:
            field_inputs: dict[_ModWebNodeSettingsFieldKey, Input] = {}
            with ui.column().classes("mod-app-details-subsection"):
                ui.label("Capacity").classes("mod-stat-label")
                ui.label("Adjust total capacity and reserved headroom for this node.").classes("mod-subtitle text-xs")
                for row_specs in _NODE_SETTINGS_CAPACITY_FIELD_ROWS:
                    with ui.row().classes("w-full gap-2 flex-wrap"):
                        for field_spec in row_specs:
                            field_inputs[field_spec.key] = _render_node_settings_number_input(label=field_spec.label)
            return field_inputs

        def _render_node_settings_font_source_input() -> Input:
            with ui.column().classes("mod-app-details-subsection"):
                ui.label("Title Fonts").classes("mod-stat-label")
                ui.label(
                    "Add one Google Fonts specimen or CSS URL per line. These are downloaded on save and made available to app title fonts."
                ).classes("mod-subtitle text-xs")
                return (
                    ui.input("Google Font URLs", value="")
                    .props("filled square type=textarea autogrow hide-bottom-space color=accent")
                    .classes("mod-app-details-field mod-app-details-notes")
                )

        def _node_settings_field_spec(
            key: _ModWebNodeSettingsFieldKey,
        ) -> _ModWebNodeSettingsNumberFieldSpec:
            for row_specs in _NODE_SETTINGS_CAPACITY_FIELD_ROWS:
                for field_spec in row_specs:
                    if field_spec.key is key:
                        return field_spec
            raise RuntimeError(f"Missing node settings field spec for key: {key!r}")

        def _set_node_settings_panel_context(node_name: str) -> None:
            panel = _require_node_settings_panel()
            panel.selected_node_name = node_name
            section = sections_by_node.get(node_name)
            if section is None:
                raise RuntimeError(f"Cannot open node settings for unknown node: {node_name!r}")
            node = section.node
            panel.title_label.set_text("Node Details")
            panel.subtitle_label.set_text(f"Update node-specific settings for {node.label}.")
            if panel.simulate_button is not None:
                panel.simulate_button.set_text(
                    "Restore Availability" if node.node_name.casefold() in simulated_down_keys else "Simulate Down"
                )
            log.info("Node settings panel context set: node=%s", node_name)

        def _hide_node_settings_panel() -> None:
            _require_node_settings_panel().hide()

        async def _refresh_node_settings_panel(node_name: str) -> None:
            panel = _require_node_settings_panel()
            log.info("Refreshing node settings panel: node=%s", node_name)
            try:
                capacity = await self._node_capacity(node_name=node_name, user=user)
                font_sources = await self._node_font_sources(node_name=node_name, user=user)
            except Exception as xcp:
                log.warning("Node settings load failed: node=%s error=%s", node_name, xcp)
                ui.notify(f"Node settings load failed: {xcp}", type="negative")
                return
            if panel.selected_node_name != node_name:
                log.info(
                    "Skipping stale node settings panel population: requested=%s selected=%s",
                    node_name,
                    panel.selected_node_name,
                )
                return
            panel.set_capacity_profile(capacity)
            panel.set_google_font_urls(font_sources)
            log.info("Node settings panel populated: node=%s", node_name)

        async def _open_node_configuration_panel(node_name: str) -> None:
            panel = _require_node_settings_panel()
            log.info("Node settings panel open scheduled: node=%s", node_name)
            await asyncio.sleep(0)
            _set_node_settings_panel_context(node_name)
            panel.show()
            log.info("Node settings panel shown: node=%s", node_name)
            await _refresh_node_settings_panel(node_name)

        def _create_open_node_configuration_handler(node_name: str) -> Callable[[object | None], None]:
            def _handle(_: object | None = None) -> None:
                log.info("Node settings badge clicked: node=%s", node_name)
                if node_settings_panel is None:
                    log.warning("Node settings overlay missing for node=%s", node_name)
                    return
                asyncio.create_task(_open_node_configuration_panel(node_name))

            return _handle

        async def _handle_toggle_simulated_down(_: object | None = None) -> None:
            node_name = _require_node_settings_panel().require_selected_node_name()
            target_url = self._toggle_simulated_down_node_url(
                current_url=request_path,
                node_name=node_name,
                simulated_down_node_names=simulated_down_node_names,
            )
            log.info("Toggling simulated node availability: node=%s", node_name)
            ui.navigate.to(target_url)

        def _create_save_node_configuration_handler() -> Callable[[object | None], Awaitable[None]]:
            async def _handle(_: object | None = None) -> None:
                panel = _require_node_settings_panel()
                node_name = panel.require_selected_node_name()
                try:
                    capacity = config.NodeCapacityProfile(
                        cpu_points_total=_parse_required_non_negative_int(
                            raw_value=_value_as_text(panel.input_for(_ModWebNodeSettingsFieldKey.CPU_TOTAL)),
                            field_label=_node_settings_field_spec(_ModWebNodeSettingsFieldKey.CPU_TOTAL).field_label,
                        ),
                        ram_points_total=_parse_required_non_negative_int(
                            raw_value=_value_as_text(panel.input_for(_ModWebNodeSettingsFieldKey.RAM_TOTAL)),
                            field_label=_node_settings_field_spec(_ModWebNodeSettingsFieldKey.RAM_TOTAL).field_label,
                        ),
                        cpu_points_reserved=_parse_required_non_negative_int(
                            raw_value=_value_as_text(panel.input_for(_ModWebNodeSettingsFieldKey.CPU_RESERVED)),
                            field_label=_node_settings_field_spec(_ModWebNodeSettingsFieldKey.CPU_RESERVED).field_label,
                        ),
                        ram_points_reserved=_parse_required_non_negative_int(
                            raw_value=_value_as_text(panel.input_for(_ModWebNodeSettingsFieldKey.RAM_RESERVED)),
                            field_label=_node_settings_field_spec(_ModWebNodeSettingsFieldKey.RAM_RESERVED).field_label,
                        ),
                    )
                    if panel.google_font_urls_input is None:
                        raise RuntimeError("Node settings font URL input is unavailable.")
                    font_sources = config.NodeFontSourceSettings(
                        google_font_urls=_value_as_text(panel.google_font_urls_input)
                    )
                except (TypeError, ValueError) as xcp:
                    ui.notify(str(xcp), type="negative")
                    return
                try:
                    capacity_result = await self._update_node_capacity(node_name=node_name, user=user, capacity=capacity)
                    font_source_result = await self._update_node_font_sources(
                        node_name=node_name,
                        user=user,
                        settings=font_sources,
                    )
                except Exception as xcp:
                    log.warning("Node settings update failed: node=%s error=%s", node_name, xcp)
                    ui.notify(f"Node settings update failed: {xcp}", type="negative")
                    return
                ui.notify(f"{capacity_result.message} {font_source_result.message}", type="positive")
                ui.navigate.reload()

            return _handle

        with ui.column().classes("w-full gap-6 px-4 py-8 md:px-8"):
            with ui.column().classes("mod-page w-full gap-6"):
                self._render_user_header(ui=ui, user=user)
                if can_manage_node_configuration:
                    with (
                        ui.element("div")
                        .classes("mod-node-settings-overlay")
                        .style("display: none;") as node_configuration_overlay
                    ):
                        backdrop = ui.element("div").classes("mod-node-settings-backdrop")
                        backdrop.on("click", lambda _: _hide_node_settings_panel())
                        panel_shell = ui.element("div").classes("mod-node-settings-shell")
                        panel_shell.on("click", js_handler="(event) => event.stopPropagation()")
                        with panel_shell:
                            with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                                with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                                    with ui.column().classes("gap-1"):
                                        node_dialog_title_label = ui.label("Node Details").classes(
                                            "text-xl font-black mod-title-small"
                                        )
                                        node_dialog_subtitle_label = ui.label("").classes("mod-subtitle text-sm")
                                    with ui.column().classes("mod-app-details-section"):
                                        node_capacity_inputs = _render_node_settings_capacity_inputs()
                                        node_google_font_urls_input = _render_node_settings_font_source_input()
                                    if dev_mode_enabled:
                                        with ui.column().classes("mod-app-details-section"):
                                            ui.label("Dev").classes("mod-stat-label")
                                            node_dialog_simulate_button = ui.button(
                                                "Simulate Down",
                                                on_click=_handle_toggle_simulated_down,
                                            ).classes("mod-list-button secondary")
                                    with ui.row().classes("w-full justify-end gap-2 mod-app-details-actions"):
                                        ui.button("Cancel", on_click=lambda _: _hide_node_settings_panel()).classes(
                                            "mod-list-button secondary"
                                        )
                                        ui.button(
                                            "Save",
                                            on_click=_create_save_node_configuration_handler(),
                                        ).classes("mod-list-button")
                    node_settings_panel = _ModWebNodeSettingsPanelState(
                        overlay=node_configuration_overlay,
                        title_label=node_dialog_title_label,
                        subtitle_label=node_dialog_subtitle_label,
                        field_inputs=node_capacity_inputs,
                        google_font_urls_input=node_google_font_urls_input,
                        simulate_button=node_dialog_simulate_button,
                    )
                    for section in _current_sections():
                        log.info("Node settings panel registered: node=%s", section.node.node_name)
                with ui.card().classes(self._hero_card_classes()):
                    with ui.column().classes(self._hero_shell_classes()):
                        with ui.row().classes(self._hero_header_classes()):
                            with ui.column().classes(self._hero_header_main_classes()):
                                ui.label("Yukibot Dashboard").classes(self._hero_title_classes())
                            with ui.column().classes(self._hero_badges_classes(wide=True)):

                                @ui.refreshable
                                def _render_home_badges(current_sections: tuple[ModWebNodeAppSection, ...]) -> None:
                                    home_node_latency_badges: list[_ModWebNodePresenceBadgeSpec] = []
                                    app_links: tuple[ModWebAppLink, ...] = tuple[ModWebAppLink, ...](
                                        app for section in current_sections for app in section.app_links
                                    )
                                    unavailable_sections: tuple[ModWebNodeAppSection, ...] = tuple(
                                        section for section in current_sections if section.error is not None
                                    )
                                    with ui.row().classes(self._hero_badge_row_classes()):
                                        for section in current_sections:
                                            badge, badge_text = self._render_home_node_status_badge(
                                                ui=ui,
                                                section=section,
                                                on_click=(
                                                    _create_open_node_configuration_handler(section.node.node_name)
                                                    if can_manage_node_configuration
                                                    else None
                                                ),
                                                extra_classes=(
                                                    "mod-node-status-badge mod-node-status-badge-actionable"
                                                    if can_manage_node_configuration
                                                    else "mod-node-status-badge"
                                                ),
                                            )
                                            badge_spec = self._home_node_latency_badge_spec(
                                                badge_element=badge,
                                                text_element=badge_text,
                                                section=section,
                                                extra_classes=(
                                                    "mod-node-status-badge mod-node-status-badge-actionable"
                                                    if can_manage_node_configuration
                                                    else "mod-node-status-badge"
                                                ),
                                            )
                                            if badge_spec is not None:
                                                home_node_latency_badges.append(badge_spec)
                                    for badge_row in self._section_badge_rows(
                                        self._node_capability_badges(
                                            app_links=app_links,
                                            unavailable_count=len(unavailable_sections),
                                        )
                                    ):
                                        with ui.row().classes(self._hero_badge_row_classes()):
                                            for badge in badge_row:
                                                self._badge_spec(ui=ui, badge=badge)
                                    self._run_home_node_latency_badges_javascript(
                                        ui=ui,
                                        badge_specs=tuple(home_node_latency_badges),
                                    )

                                _render_home_badges(_current_sections())
                        apply_home_node_stats: Callable[[tuple[ModWebHomeNodeSummary, ...]], None] = (
                            self._render_live_home_node_stats(
                                ui=ui,
                                initial_summaries=home_node_summaries,
                            )
                        )

            @ui.refreshable
            def _render_home_sections(
                current_sections: tuple[ModWebNodeAppSection, ...],
                current_summaries: tuple[ModWebHomeNodeSummary, ...],
            ) -> None:
                self._render_home_page_sections(
                    ui=ui,
                    sections=current_sections,
                    node_summaries=current_summaries,
                    user=user,
                    show_api_actions=show_api_actions,
                )

            _render_home_sections(_current_sections(), _current_summaries())

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
                current_summaries = _current_summaries()
                if not self._sections_equal_for_card_render(previous_sections, current_sections):
                    _render_home_badges.refresh(current_sections)
                    _render_home_sections.refresh(current_sections, current_summaries)
                apply_home_node_stats(current_summaries)

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

    @classmethod
    def _home_node_status_entry(
        cls,
        *,
        system_summary: NodeSystemSummary | None,
        app_count: int,
    ) -> tuple[str | None, BadgeTone]:
        if system_summary is None:
            return ("Unavailable", "red")
        running_count: int = len(system_summary.running_names)
        if app_count <= 0 or running_count <= 0:
            return (None, "black")
        if running_count == 1:
            return ("App Running", "purple")
        return (f"{running_count} Running", "purple")

    @staticmethod
    def _home_node_card_tone(*, system_summary: NodeSystemSummary | None, app_count: int) -> BadgeTone:
        if system_summary is None:
            return "red"
        if app_count <= 0:
            return "grey"
        if system_summary.running_names:
            return "purple"
        return "black"

    def _build_home_node_stat_specs(
        self,
        node_summaries: tuple[ModWebHomeNodeSummary, ...],
    ) -> tuple[_ModWebHomeNodeStatSpec, ...]:
        node_stats: list[_ModWebHomeNodeStatSpec] = []
        for node_summary in node_summaries:
            system_summary: NodeSystemSummary | None = node_summary.system_summary
            cpu_value, cpu_tone = self._system_cpu_entry(system_summary)
            ram_value, ram_tone = self._system_ram_entry(system_summary)
            storage_value, storage_tone = self._system_storage_entry(system_summary)
            uptime_value, uptime_tone = self._system_uptime_entry(system_summary)
            running_tooltip: str | None = None
            if system_summary is None:
                running_text = "Unavailable"
                running_tone = "red"
            elif not system_summary.running_names:
                running_text = "Nothin Running"
                running_tone = "grey"
            else:
                running_text = self._running_value(system_summary.running_names)
                running_tone = "purple"
            if system_summary is not None and len(system_summary.running_names) > 2:
                running_tooltip = ", ".join(system_summary.running_names)
            status_text, status_tone = self._home_node_status_entry(
                system_summary=system_summary,
                app_count=node_summary.app_count,
            )
            node_stats.append(
                _ModWebHomeNodeStatSpec(
                    node_name=node_summary.node.node_name,
                    node_label=node_summary.node.label,
                    node_subtitle=self._node_display_subtitle(
                        label=node_summary.node.label,
                        node_name=node_summary.node.node_name,
                    ),
                    status_text=status_text,
                    status_tone=status_tone,
                    card_tone=self._home_node_card_tone(
                        system_summary=system_summary,
                        app_count=node_summary.app_count,
                    ),
                    metrics=(
                        _ModWebHomeMetricSpec(label="CPU", icon="speed", value=cpu_value, tone=cpu_tone),
                        _ModWebHomeMetricSpec(label="RAM", icon="memory", value=ram_value, tone=ram_tone),
                        _ModWebHomeMetricSpec(
                            label="Storage",
                            icon="storage",
                            value=storage_value,
                            tone=storage_tone,
                        ),
                        _ModWebHomeMetricSpec(label="Uptime", icon="schedule", value=uptime_value, tone=uptime_tone),
                    ),
                    running_text=running_text,
                    running_tone=running_tone,
                    running_tooltip=running_tooltip,
                )
            )
        return tuple[_ModWebHomeNodeStatSpec, ...](node_stats)

    def _render_live_home_node_stats_renderer(
        self,
        *,
        ui: ModWebUi,
        initial_summaries: tuple[ModWebHomeNodeSummary, ...],
    ) -> Callable[[tuple[ModWebHomeNodeSummary, ...]], None]:
        @ui.refreshable
        def _render_stats(node_summaries: tuple[ModWebHomeNodeSummary, ...]) -> None:
            with ui.row().classes("mod-home-node-grid w-full gap-3"):
                for stat in self._build_home_node_stat_specs(node_summaries):
                    with ui.card().classes(f"mod-home-node-card mod-home-node-card-{stat.card_tone}"):
                        with ui.column().classes("w-full gap-3 p-3"):
                            with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                                node_text_style: str | None = self._node_text_style(node_name=stat.node_name)
                                with ui.column().classes("gap-0 min-w-0"):
                                    node_title: Label = ui.label(stat.node_label).classes("mod-home-node-title")
                                    if node_text_style is not None:
                                        node_title.style(node_text_style)
                                    if stat.node_subtitle is not None:
                                        node_subtitle: Label = ui.label(stat.node_subtitle).classes(
                                            "mod-home-node-subtitle"
                                        )
                                        if node_text_style is not None:
                                            node_subtitle.style(node_text_style)
                                if stat.status_text is not None:
                                    self._badge(ui=ui, text=stat.status_text, tone=stat.status_tone)
                            with ui.element("div").classes("mod-home-node-metrics"):
                                for metric in stat.metrics:
                                    metric_row: Element = ui.row().classes("mod-home-node-metric")
                                    with metric_row:
                                        ui.icon(metric.icon).classes(
                                            f"mod-home-node-metric-icon mod-tone-{metric.tone}"
                                        )
                                        ui.label(metric.value).classes("mod-home-node-metric-value")
                                    self._attach_text_tooltip(ui=ui, target=metric_row, text=metric.label)
                            running_row: Element = ui.row().classes("mod-home-node-running")
                            with running_row:
                                ui.icon(_HOME_APPS_ICON).classes(
                                    f"mod-home-node-running-icon mod-tone-{stat.running_tone}"
                                )
                                ui.label(stat.running_text).classes("mod-home-node-running-value")
                            if stat.running_tooltip is not None:
                                self._attach_text_tooltip(ui=ui, target=running_row, text=stat.running_tooltip)

        _render_stats(initial_summaries)
        return _render_stats.refresh

    def _render_live_home_node_stats(
        self,
        *,
        ui: ModWebUi,
        initial_summaries: tuple[ModWebHomeNodeSummary, ...],
        refresh_async_summaries: Callable[[], Awaitable[tuple[ModWebHomeNodeSummary, ...]]] | None = None,
    ) -> Callable[[tuple[ModWebHomeNodeSummary, ...]], None]:
        apply_summaries: Callable[[tuple[ModWebHomeNodeSummary, ...]], None] = (
            self._render_live_home_node_stats_renderer(
                ui=ui,
                initial_summaries=initial_summaries,
            )
        )
        if refresh_async_summaries is not None:
            refresh_async = self._build_async_refreshable_updater(
                refresh_async_value=refresh_async_summaries,
                apply_value=apply_summaries,
                error_context="Mod web home node stats",
            )
            refresh_timer = ui.timer(
                _TITLE_STATS_REFRESH_INTERVAL_SECONDS,
                lambda: asyncio.create_task(refresh_async()),
            )
            self._register_timer_cleanup(ui=ui, timer=refresh_timer)
        return apply_summaries

    def _render_home_page_sections(
        self,
        *,
        ui: ModWebUi,
        sections: tuple[ModWebNodeAppSection, ...],
        node_summaries: tuple[ModWebHomeNodeSummary, ...],
        user: ModWebUser,
        show_api_actions: bool,
    ) -> None:
        del user
        summary_by_node_name: dict[str, ModWebHomeNodeSummary] = {
            summary.node.node_name: summary for summary in node_summaries
        }
        app_links: tuple[ModWebAppLink, ...] = tuple[ModWebAppLink, ...](
            app for section in sections for app in section.app_links
        )
        has_unavailable_sections: bool = any(section.error is not None for section in sections)
        if not app_links and not has_unavailable_sections:
            with ui.card().classes("mod-card mod-card-empty w-full"):
                ui.label("No apps are currently available.").classes("p-8 text-lg mod-subtitle")
            return

        with ui.element("div").classes("mod-home-section-grid w-full"):
            for section in sections:
                with ui.column().classes("mod-home-section w-full gap-3"):
                    node_text_style: str | None = self._node_text_style(node_name=section.node.node_name)
                    with ui.row().classes("w-full items-center justify-between gap-1 flex-wrap"):
                        with ui.column().classes("gap-1"):
                            with ui.row().classes("items-center gap-2 min-w-0"):
                                ui.html(
                                    self._home_section_avatar_markup(
                                        node_name=section.node.node_name,
                                        display_name=section.node.label,
                                    )
                                )
                                section_title: Label = ui.label(section.node.label).classes(
                                    "text-xl font-bold mod-title-small"
                                )
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
                            self._badge_spec(ui=ui, badge=self._app_count_badge(app_count=len(section.app_links)))
                            for badge in self._node_resource_point_badges(
                                summary_by_node_name.get(section.node.node_name)
                            ):
                                self._badge_spec(ui=ui, badge=badge)
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
                                for badge_row in self._section_badge_rows(
                                    self._node_capability_badges(app_links=current_app_links)
                                ):
                                    with ui.row().classes(self._hero_badge_row_classes()):
                                        for badge in badge_row:
                                            self._badge_spec(ui=ui, badge=badge)

                            _render_node_badges(current_app_links)
                    with ui.row().classes(self._hero_action_row_classes()):
                        self._action_link(
                            ui=ui,
                            label="Home",
                            url=self._app_list_view_url(self.index_path(), show_api_actions=show_api_actions),
                            compact=True,
                        )
                    title_stats_refresh = None if subscribe_node_state_updates is not None else refresh_async_title_stats
                    apply_title_stats: Callable[[tuple[ModWebTitleStat, ...]], None] = self._render_live_title_stats(
                        ui=ui,
                        initial_stats=initial_title_stats,
                        refresh_async_stats=title_stats_refresh,
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
    def _bot_avatar_uri_fallback() -> str:
        resource_avatar_uri: str | None = mod_web_avatars._user_avatar_icon_data_uri(Power_Level.user)
        if resource_avatar_uri is not None:
            return resource_avatar_uri
        return mod_web_avatars._user_avatar_fallback_svg_data_uri(Power_Level.user)

    @staticmethod
    def _avatar_url_text(candidate: object | None) -> str | None:
        if candidate is None:
            return None
        for method_name in ("make_guild_avatar_url", "make_avatar_url"):
            avatar_factory = getattr(candidate, method_name, None)
            if not callable(avatar_factory):
                continue
            avatar_url = avatar_factory()
            if avatar_url is not None:
                return str(avatar_url)
        display_avatar_url = getattr(candidate, "display_avatar_url", None)
        if display_avatar_url is None:
            return None
        return str(display_avatar_url)

    def _node_bot_avatar_uri(self, *, node_name: str) -> str:
        bot = self._mod_web_bot()
        if bot is not None:
            target_user_id: int | None = self._node_bot_user_id(node_name=node_name, bot=bot)
            if target_user_id is not None:
                if node_name.casefold() == config.MOD_WEB_SERVER.node_name.casefold():
                    avatar_uri = self._avatar_url_text(bot.get_me())
                    if avatar_uri is not None:
                        return avatar_uri
                cached_user = bot.cache.get_user(target_user_id)
                avatar_uri = self._avatar_url_text(cached_user)
                if avatar_uri is not None:
                    return avatar_uri
                cached_member = bot.cache.get_member(config.DISCORD_GUILD, target_user_id)
                avatar_uri = self._avatar_url_text(cached_member)
                if avatar_uri is not None:
                    return avatar_uri
        snapshot = self._known_bot_snapshot_for_node(node_name=node_name)
        if snapshot is not None and snapshot.features.presentation is not None:
            avatar_uri = snapshot.features.presentation.avatar_uri
            if avatar_uri is not None:
                return avatar_uri
        return self._bot_avatar_uri_fallback()

    def _home_section_avatar_markup(self, *, node_name: str, display_name: str) -> str:
        avatar_alt = escape(f"{display_name} bot avatar", quote=True)
        avatar_uri = self._node_bot_avatar_uri(node_name=node_name)
        return (
            "<img"
            f' class="mod-user-avatar mod-home-section-avatar" src="{escape(avatar_uri, quote=True)}"'
            f' alt="{avatar_alt}" loading="lazy" referrerpolicy="no-referrer">'
        )

    @staticmethod
    def _node_status_badge_text(section: ModWebNodeAppSection) -> str:
        if section.is_simulated_down:
            return f"{section.node.label}: Simulated Down"
        return f"{section.node.label}: {'Alive' if section.error is None else 'Down'}"

    @staticmethod
    def _node_capability_badges(
        *,
        app_links: tuple[ModWebAppLink, ...],
        unavailable_count: int = 0,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        badges: list[_ModWebBadgeSpec] = [
            ModWebHomeMixin._app_count_badge(app_count=len(app_links)),
            _ModWebBadgeSpec(
                text=f"{sum(1 for app in app_links if app.supports_mods)} Modly",
                tone="purple",
            ),
            _ModWebBadgeSpec(
                text=f"{sum(1 for app in app_links if app.supports_saves)} Savely",
                tone="black",
            ),
            _ModWebBadgeSpec(
                text=f"{sum(1 for app in app_links if app.supports_configs)} Configy",
                tone="black",
            ),
            _ModWebBadgeSpec(
                text=f"{sum(1 for app in app_links if app.supports_console_actions)} Consolely",
                tone="black",
            ),
            _ModWebBadgeSpec(
                text=f"{sum(1 for app in app_links if app.supports_chat)} Chatty",
                tone="purple",
            ),
        ]
        if unavailable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{unavailable_count} unavailable", tone="red"))
        return tuple(badges)

    @staticmethod
    def _app_count_badge(*, app_count: int) -> _ModWebBadgeSpec:
        return _ModWebBadgeSpec(text=str(app_count), tone="black", icon=_HOME_APPS_ICON, tooltip_text="Apps")

    @classmethod
    def _node_resource_point_badges(
        cls,
        node_summary: ModWebHomeNodeSummary | None,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        if node_summary is None or node_summary.system_summary is None:
            return ()
        system_summary = node_summary.system_summary
        cpu_badge = cls._node_resource_point_badge(
            available_points=system_summary.cpu_points_available,
            capacity_points=system_summary.cpu_points_capacity,
            icon="speed",
            tooltip_text="CPU",
        )
        ram_badge = cls._node_resource_point_badge(
            available_points=system_summary.ram_points_available,
            capacity_points=system_summary.ram_points_capacity,
            icon="memory",
            tooltip_text="RAM",
        )
        return tuple(badge for badge in (cpu_badge, ram_badge) if badge is not None)

    @staticmethod
    def _node_resource_point_badge(
        *,
        available_points: int | None,
        capacity_points: int | None,
        icon: str,
        tooltip_text: str,
    ) -> _ModWebBadgeSpec | None:
        if available_points is None or capacity_points is None or capacity_points <= 0:
            return None
        if available_points <= 0:
            tone: BadgeTone = "red"
        elif available_points * 4 <= capacity_points:
            tone = "warn"
        else:
            tone = "black"
        return _ModWebBadgeSpec(text=f"{available_points}/{capacity_points}", tone=tone, icon=icon, tooltip_text=tooltip_text)

    @staticmethod
    def _home_node_badge_initial_text(section: ModWebNodeAppSection) -> str:
        if section.error is not None:
            return ModWebHomeMixin._node_status_badge_text(section)
        return f"{section.node.label}: ..."

    @classmethod
    def _home_node_latency_badge_spec(
        cls,
        *,
        badge_element: Element,
        text_element: Element,
        section: ModWebNodeAppSection,
        extra_classes: str,
    ) -> _ModWebNodePresenceBadgeSpec | None:
        badge_element_id = getattr(badge_element, "id", None)
        text_element_id = getattr(text_element, "id", None)
        if section.error is None and (not isinstance(badge_element_id, int) or not isinstance(text_element_id, int)):
            raise RuntimeError(f"Healthy node badge text element is missing its id: {section.node.node_name}")
        if not isinstance(badge_element_id, int) or not isinstance(text_element_id, int):
            return None
        pending_text = cls._home_node_badge_initial_text(section)
        return _ModWebNodePresenceBadgeSpec(
            node_name=section.node.node_name,
            badge_element_id=badge_element_id,
            text_element_id=text_element_id,
            node_label=section.node.label,
            pending_text=pending_text,
            alive_text=f"{section.node.label}: Alive",
            down_text=f"{section.node.label}: Down",
            presence_stream_url=section.node.presence_stream_url if section.error is None else None,
            pending_class_name=cls._badge_class_name(
                tone=cls._node_status_badge_tone(section),
                extra_classes=extra_classes,
            ),
            healthy_class_name=cls._badge_class_name(tone="black", extra_classes=extra_classes),
            unhealthy_class_name=cls._badge_class_name(tone="red", extra_classes=extra_classes),
            show_latency=section.error is None,
        )

    @classmethod
    def _run_home_node_latency_badges_javascript(
        cls,
        *,
        ui: ModWebUi,
        badge_specs: tuple[_ModWebNodePresenceBadgeSpec, ...],
    ) -> None:
        cls._run_node_presence_badges_javascript(
            ui=ui,
            badge_specs=badge_specs,
            controller_key="modWebHomeNodeLatency",
        )

    @classmethod
    def _home_node_latency_badges_javascript(
        cls,
        badge_specs: tuple[_ModWebNodePresenceBadgeSpec, ...],
    ) -> str:
        return cls._node_presence_badges_javascript(
            badge_specs=badge_specs,
            controller_key="modWebHomeNodeLatency",
        )

    @staticmethod
    def _node_status_badge_tone(section: ModWebNodeAppSection) -> BadgeTone:
        if section.is_simulated_down:
            return "warn"
        if section.error is not None:
            return "red"
        return "black"

    def _render_home_node_status_badge(
        self,
        *,
        ui: ModWebUi,
        section: ModWebNodeAppSection,
        on_click: Callable[[object | None], object] | None,
        extra_classes: str,
    ) -> tuple[Element, Label]:
        badge_text = self._home_node_badge_initial_text(section)
        badge = ui.element("span").classes(self._badge_class_name(tone=self._node_status_badge_tone(section), extra_classes=extra_classes))
        if on_click is not None:
            badge.on("click", on_click)
        self._attach_badge_tooltip(ui=ui, target=badge, text=badge_text)
        with badge:
            text_label = ui.label(badge_text).classes("mod-home-node-status-badge-text")
        return badge, text_label

    @staticmethod
    def _login_node_status_badge_text(status: ModWebNodeStatus) -> str:
        if status.is_simulated_down:
            return f"{status.node.label}: Simulated Down"
        return f"{status.node.label}: {'Alive' if status.alive else 'Down'}"

    @staticmethod
    def _login_node_status_badge_tone(status: ModWebNodeStatus) -> BadgeTone:
        if status.is_simulated_down:
            return "warn"
        if not status.alive:
            return "red"
        return "black"

    def _render_app_card_content(
        self,
        *,
        ui: ModWebUi,
        app: ModWebAppLink,
        show_api_actions: bool = False,
    ) -> None:
        runtime_badge: _ModWebBadgeSpec | None = self._app_card_runtime_badge(app)
        with ui.row().classes("mod-app-card-shell w-full items-center justify-between gap-2 p-3 flex-wrap"):
            with ui.column().classes("mod-app-card-main min-w-0 gap-0"):
                title_label = ui.label(app.friendly).classes(self._app_card_title_classes(app))
                self._attach_text_tooltip(ui=ui, target=title_label, text=app.friendly)
            with ui.row().classes("mod-app-card-actions items-center justify-end gap-3 flex-wrap"):
                if runtime_badge is not None:
                    runtime_badge_classes = "mod-app-runtime-chip"
                    if app.runtime_changed:
                        runtime_badge_classes = f"{runtime_badge_classes} mod-app-runtime-chip-live"
                    runtime_badge_label = self._badge(
                        ui=ui,
                        text=runtime_badge.text,
                        tone=runtime_badge.tone,
                        extra_classes=runtime_badge_classes,
                    )
                    runtime_badge_tooltip_html: str | None = self._player_count_tooltip_html(
                        player_count=app.player_count,
                        player_capacity=app.player_capacity,
                        connected_player_names=app.connected_player_names,
                    )
                    if runtime_badge_tooltip_html is not None:
                        self._attach_html_tooltip(ui=ui, target=runtime_badge_label, html=runtime_badge_tooltip_html)
                with ui.row().classes("mod-app-card-badges items-center gap-3 flex-wrap"):
                    for badge in self._app_card_badges(app):
                        badge_target: str | None = self._app_card_badge_target(
                            app=app,
                            badge=badge,
                            show_api_actions=show_api_actions,
                        )
                        shift_target: str | None = (
                            self._app_list_view_url(app.chat_url, show_api_actions=show_api_actions)
                            if badge.tab_id == "chat" and app.chat_url is not None
                            else None
                        )
                        if badge_target is None:
                            self._badge(ui=ui, text=badge.text, tone=badge.tone)
                        else:
                            self._badge_link(
                                ui=ui,
                                text=badge.text,
                                tone=badge.tone,
                                url=badge_target,
                                shift_url=shift_target,
                                stop_propagation=True,
                            )
                api_actions: tuple[_ModWebLinkSpec, ...] | tuple[()] = (
                    self._app_card_api_actions(app) if show_api_actions else ()
                )
                if api_actions:
                    self._render_app_card_api_pill(ui=ui, actions=api_actions)

    def _app_card_badges(self, app: ModWebAppLink) -> tuple[_ModWebAppCardBadgeSpec, ...]:
        badges: list[_ModWebAppCardBadgeSpec] = []
        for tab in self._app_link_tabs(app):
            if tab.show_on_app_card:
                badges.append(
                    _ModWebAppCardBadgeSpec(
                        text=tab.label,
                        tone=tab.app_card_tone,
                        tab_id=tab.tab_id,
                    )
                )
            if tab.app_card_badge_handler_name is None:
                continue
            badge_handler = getattr(self, tab.app_card_badge_handler_name, None)
            if badge_handler is None:
                raise ValueError(f"Unknown app tab app-card badge handler: {tab.app_card_badge_handler_name}")
            badges.extend(badge_handler(app=app, tab=tab))
        return tuple(badges)

    @staticmethod
    def _blueprint_app_card_badges(
        *,
        app: ModWebAppLink,
        tab: "ModWebAppTabDefinition",
    ) -> tuple[_ModWebAppCardBadgeSpec, ...]:
        del app
        return (_ModWebAppCardBadgeSpec(text=tab.label, tone=tab.app_card_tone, tab_id=tab.tab_id),)

    def _app_card_badge_target(
        self,
        *,
        app: ModWebAppLink,
        badge: _ModWebAppCardBadgeSpec,
        show_api_actions: bool,
    ) -> str | None:
        if badge.tab_id is None or not app.url:
            return None
        base_url: str = self._app_list_view_url(app.url, show_api_actions=show_api_actions)
        return self._request_url_with_query_values(
            base_url,
            param_name=_APP_SECTION_QUERY_PARAM,
            values=(badge.tab_id,),
        )

    @staticmethod
    def _player_count_snapshot_text(*, player_count: int | None, player_capacity: int | None) -> str | None:
        if player_count is None or player_capacity is None:
            return None
        return f"{player_count} / {player_capacity}"

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
        if not app.enabled:
            return _ModWebBadgeSpec(text="Disabled", tone="red")
        if app.runtime_fault is not None:
            return _ModWebBadgeSpec(text="Crashed", tone="red")
        return None

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
        if not hasattr(ui, "tooltip"):
            return
        with target:
            ui.tooltip(text)

    def _app_card_link_classes(self, app: ModWebAppLink) -> str:
        classes = "mod-card mod-app-card mod-app-card-link w-full"
        runtime_state_class: str | None = self._app_runtime_state_class(
            running=app.running,
            transition_state=app.transition_state,
            class_prefix="mod-app-card",
        )
        if runtime_state_class is not None:
            classes = f"{classes} {runtime_state_class}"
        if app.runtime_changed:
            classes = f"{classes} mod-app-card-live"
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
            connected_player_names=app.connected_player_names,
            runtime_fault=app.runtime_fault,
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

    def _app_link_with_runtime(self, app: ModWebAppLink, app_stats: NodeAppRuntimeSummary) -> ModWebAppLink:
        updated_app: ModWebAppLink = replace(
            app,
            running=app_stats.running,
            enabled=app_stats.enabled,
            transition_state=app_stats.transition_state,
            player_count=app_stats.player_count,
            player_capacity=app_stats.player_capacity,
            connected_player_names=app_stats.connected_player_names,
            runtime_fault=app_stats.runtime_fault,
        )
        return self._app_link_with_tabs(ModWebHomeMixin._with_runtime_change_flag(app=updated_app, previous_app=app))

    def _model_with_runtime_state(
        self,
        model: ModWebBasePageModel,
        *,
        app_stats: NodeAppRuntimeSummary | None,
        app_start_blocked: bool,
        update_info: AppUpdateInfo | None | object = _KEEP_PAGE_MODEL_VALUE,
        update_status: AppUpdateStatus | None | object = _KEEP_PAGE_MODEL_VALUE,
    ) -> ModWebBasePageModel:
        next_update_info: AppUpdateInfo | None = (
            model.update_info if update_info is _KEEP_PAGE_MODEL_VALUE else cast(AppUpdateInfo | None, update_info)
        )
        next_update_status: AppUpdateStatus | None = (
            model.update_status
            if update_status is _KEEP_PAGE_MODEL_VALUE
            else cast(AppUpdateStatus | None, update_status)
        )
        return self._page_model_with_tabs(
            replace(
                model,
                app_stats=app_stats,
                app_start_blocked=app_start_blocked,
                update_info=next_update_info,
                update_status=next_update_status,
            )
        )

    @staticmethod
    def _is_current_node_name(node_name: str) -> bool:
        return node_name.strip().casefold() == config.MOD_WEB_SERVER.node_name.strip().casefold()

    async def _refresh_runtime_model(self, *, model: ModWebBasePageModel, user: ModWebUser) -> ModWebBasePageModel:
        if self._is_current_node_name(model.node_name):
            app: ManagedApp = self._resolve_app(model.app_name)
            app_entry = self._node_api.build_app_entry(app)
            app_stats: NodeAppRuntimeSummary = await self._node_api.build_app_runtime_summary(app)
            return self._model_with_runtime_state(
                model,
                app_stats=app_stats,
                app_start_blocked=self._app_start_blocked_local(app),
                update_info=app_entry.update_info,
                update_status=app_entry.update_status,
            )

        node: ModWebNodeLink = self._remote_node_link(model.node_name)
        app_entry, app_stats, system_summary = await asyncio.gather(
            self._remote_app_entry_async(node, model.app_name, user),
            self._remote_app_runtime_summary_async(node, model.app_name, user),
            self._remote_node_system_summary_or_none_async(
                node,
                user,
                error_context="Remote mod web runtime model system summary failed",
            ),
        )
        return self._model_with_runtime_state(
            model,
            app_stats=app_stats,
            app_start_blocked=self._app_start_blocked_remote(
                app_name=model.app_name,
                app_stats=app_stats,
                start_blocked_app_ids=() if system_summary is None else system_summary.start_blocked_app_ids,
            ),
            update_info=app_entry.update_info,
            update_status=app_entry.update_status,
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
        return self._app_link_with_tabs(
            ModWebAppLink(
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
                connected_player_names=entry.connected_player_names,
                runtime_fault=entry.runtime_fault,
                saves_api_url=saves_api_url,
                settings_api_url=settings_api_url,
                map_url=entry.map_url,
                supports_blueprints=entry.supports_blueprints,
                supports_console_actions=entry.supports_console_actions,
                supports_chat=entry.supports_chat,
                supports_updates=entry.supports_updates,
                chat_url=chat_url,
                update_status=entry.update_status,
            )
        )
