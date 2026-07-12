from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import avatars as mod_web_avatars
from .constants import (
    _APP_SECTION_QUERY_PARAM,
    _SAME_ORIGIN_NODE_PROXY_BASE,
    _TITLE_STATS_REFRESH_INTERVAL_SECONDS,
    log,
)
from .links import mod_web_node_system_path
from .nicegui_protocols import ModWebUi, _value_as_bool, _value_as_text
from .runtime_imports import (
    MAX_RESTART_INTERVAL_MINUTES,
    MIN_RESTART_INTERVAL_MINUTES,
    AbstractEventLoop,
    AppUpdateInfo,
    AppUpdateStatus,
    Awaitable,
    BadgeTone,
    Button,
    Callable,
    Card,
    Checkbox,
    Enum,
    Html,
    Input,
    Label,
    MaintenanceService,
    ModWebUser,
    NodeAppEntry,
    NodeAppRuntimeSummary,
    NodeAppTransitionState,
    NodeDiskManagementState,
    NodeRestartScheduleEntry,
    NodeRestartScheduleState,
    NodeRestartState,
    NodeStateStreamEvent,
    NodeSystemAction,
    NodeSystemHistory,
    NodeSystemSample,
    NodeSystemSummary,
    Power_Level,
    Request,
    RestartTarget,
    Select,
    Tooltip,
    app_scope_from_name,
    asyncio,
    cast,
    config,
    dataclass,
    escape,
    mod_web_badge_class,
    quote,
    replace,
)
from .utils import _format_player_capacity, _format_uptime_seconds

_KEEP_PAGE_MODEL_VALUE = object()
_CLIENT_TIMEZONE_VALUE = "client"
_RESTART_TIMEZONE_OPTIONS: dict[str, str] = {
    _CLIENT_TIMEZONE_VALUE: "Client local time",
    "UTC": "UTC",
    "Europe/London": "London",
    "Australia/Melbourne": "Melbourne",
    "Europe/Helsinki": "Helsinki",
}
_RESTART_DISPLAY_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("UTC", "UTC"),
    ("London", "Europe/London"),
    ("Melbourne", "Australia/Melbourne"),
    ("Helsinki", "Europe/Helsinki"),
)
_RESTART_SCHEDULE_FIELD_PROPS = "filled square dense hide-bottom-space color=accent"


class _RestartWeekday(Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


_RESTART_WEEKDAYS: tuple[_RestartWeekday, ...] = tuple(_RestartWeekday)
_RESTART_WEEKDAY_OPTIONS: dict[str, str] = {
    weekday.value: weekday.value.title() for weekday in _RESTART_WEEKDAYS
}


def _restart_interval_parts(interval_minutes: int) -> tuple[int, int, int]:
    days, remaining = divmod(interval_minutes, 24 * 60)
    hours, minutes = divmod(remaining, 60)
    return days, hours, minutes


def _format_restart_hours_input(hours: int, minutes: int) -> str:
    return str(hours) if minutes == 0 else f"{hours}:{minutes:02d}"


def _parse_restart_hours_input(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) not in (1, 2) or any(not part.isdigit() for part in parts):
        raise ValueError("Use hours as H or H:MM.")
    hours = int(parts[0])
    minutes = int(parts[1]) if len(parts) == 2 else 0
    if hours > 23 or minutes > 59:
        raise ValueError("Use 0–23 hours and 0–59 minutes.")
    return hours, minutes


def _restart_interval_from_parts(*, days: int, hours: int, minutes: int) -> int:
    if days < 0 or hours < 0 or minutes < 0 or hours > 23 or minutes > 59:
        raise ValueError("Use 0–23 hours and 0–59 minutes.")
    interval_minutes = days * 24 * 60 + hours * 60 + minutes
    if not MIN_RESTART_INTERVAL_MINUTES <= interval_minutes <= MAX_RESTART_INTERVAL_MINUTES:
        raise ValueError("The interval must be between 1 hour and 1 week.")
    return interval_minutes


def _restart_anchor_timestamp(
    weekday: _RestartWeekday,
    time_value: str,
    timezone_name: str,
    *,
    now_timestamp: int | None = None,
) -> int:
    try:
        local_time = time.fromisoformat(time_value)
    except ValueError as xcp:
        raise ValueError("Choose an anchor time.") from xcp
    if local_time.tzinfo is not None or local_time.second != 0 or local_time.microsecond != 0:
        raise ValueError("The anchor time must use local hours and minutes.")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as xcp:
        raise ValueError("The selected timezone is unavailable.") from xcp
    local_now = (
        datetime.now(timezone)
        if now_timestamp is None
        else datetime.fromtimestamp(now_timestamp, timezone)
    )
    days_ahead = (_RESTART_WEEKDAYS.index(weekday) - local_now.weekday()) % len(_RESTART_WEEKDAYS)
    local_date = (local_now + timedelta(days=days_ahead)).date()
    local_datetime = datetime.combine(local_date, local_time)
    if local_datetime <= local_now.replace(tzinfo=None):
        local_datetime += timedelta(days=len(_RESTART_WEEKDAYS))
    earlier = local_datetime.replace(tzinfo=timezone, fold=0)
    later = local_datetime.replace(tzinfo=timezone, fold=1)
    round_trip = earlier.astimezone(ZoneInfo("UTC")).astimezone(timezone).replace(tzinfo=None)
    if round_trip != local_datetime:
        raise ValueError("That local time does not exist because of daylight saving.")
    if earlier.utcoffset() != later.utcoffset():
        raise ValueError("That local time is ambiguous because of daylight saving. Choose another time or UTC.")
    return int(earlier.timestamp())


def _format_restart_timestamp(timestamp: int, timezone_name: str) -> str:
    scheduled_at = datetime.fromtimestamp(timestamp, ZoneInfo(timezone_name))
    return scheduled_at.strftime("%a, %d %b %Y · %H:%M %Z")


def _format_restart_state_line(label: str, timestamp: int, restart_kind: str, timezone_name: str) -> str:
    return f"{label}: {_format_restart_timestamp(timestamp, timezone_name)} [{restart_kind}]"


_RESTART_STATE_LINE_CLASSES = "mod-subtitle text-xs"


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
from .ui_helpers import ModWebUiHelpersMixin

if TYPE_CHECKING:
    from nicegui.element import Element
    from nicegui.elements.dialog import Dialog


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


@dataclass(frozen=True, slots=True)
class _ModWebHomeMetricBinding:
    icon_element: Element
    value_label: Label


@dataclass(frozen=True, slots=True)
class _ModWebHomeTooltipBinding:
    tooltip: Tooltip
    tooltip_content: Html


@dataclass(frozen=True, slots=True)
class _ModWebHomeNodeStatBindings:
    card: Card
    title_label: Label
    subtitle_label: Label | None
    status_badge: Label
    metric_bindings: tuple[_ModWebHomeMetricBinding, ...]
    running_icon: Element
    running_value_label: Label
    running_tooltip: _ModWebHomeTooltipBinding


class _ModWebSystemChartMetric(Enum):
    CPU = "cpu"
    RAM = "ram"
    STORAGE = "storage"


@dataclass(frozen=True, slots=True)
class _ModWebSystemChartSeries:
    metric: _ModWebSystemChartMetric
    label: str
    color_hex: str


@dataclass(frozen=True, slots=True)
class _ModWebSystemActionSpec:
    action: NodeSystemAction
    title: str
    button_label: str
    required_target: RestartTarget | None = None


class _ModWebNodeSettingsFieldKey(Enum):
    CPU_TOTAL = "cpu_points_total"
    RAM_TOTAL = "ram_points_total"
    CPU_RESERVED = "cpu_points_reserved"
    RAM_RESERVED = "ram_points_reserved"


class _ModWebNodeDiskChoice(Enum):
    DEFAULT_PRIMARY = "__default_primary_disk__"
    NO_SECONDARY = "__no_secondary_disk__"


@dataclass(frozen=True, slots=True)
class _ModWebNodeSettingsNumberFieldSpec:
    key: _ModWebNodeSettingsFieldKey
    label: str
    field_label: str


_HOME_APPS_ICON: str = "apps"

_SYSTEM_CHART_SERIES: tuple[_ModWebSystemChartSeries, ...] = (
    _ModWebSystemChartSeries(metric=_ModWebSystemChartMetric.CPU, label="CPU", color_hex="#a78bfa"),
    _ModWebSystemChartSeries(metric=_ModWebSystemChartMetric.RAM, label="RAM", color_hex="#38bdf8"),
    _ModWebSystemChartSeries(metric=_ModWebSystemChartMetric.STORAGE, label="Storage", color_hex="#f59e0b"),
)

_SYSTEM_ACTION_SPECS: tuple[_ModWebSystemActionSpec, ...] = (
    _ModWebSystemActionSpec(
        action=NodeSystemAction.RESTART_PROCESS,
        title="Restart Bot",
        button_label="Restart Bot",
    ),
    _ModWebSystemActionSpec(
        action=NodeSystemAction.REBOOT_HOST,
        title="Restart System",
        button_label="Restart System",
    ),
    _ModWebSystemActionSpec(
        action=NodeSystemAction.RESTART_PORTAL,
        title="Restart Portal",
        button_label="Restart Portal",
        required_target=RestartTarget.PORTAL,
    ),
)


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
        simulated_down_node_names: tuple[str, ...] = self._simulated_down_node_names(request)
        simulated_down_keys = {node_name.casefold() for node_name in simulated_down_node_names}
        summary_nodes = tuple(
            node for node in self._node_links() if node.node_name.casefold() not in simulated_down_keys
        )

        async def _load_system_summary(node: ModWebNodeLink) -> NodeSystemSummary | None:
            return await self._remote_node_system_summary_or_none_async(
                node,
                user,
                error_context="Remote mod web home system summary failed",
            )

        summary_tasks = tuple(
            asyncio.create_task(_load_system_summary(node))
            for node in summary_nodes
        )
        try:
            sections: tuple[ModWebNodeAppSection, ...] = await self._home_app_sections(
                user, simulated_down_node_names=simulated_down_node_names
            )
            summary_values = await asyncio.gather(*summary_tasks)
        except BaseException:
            for summary_task in summary_tasks:
                summary_task.cancel()
            await asyncio.gather(*summary_tasks, return_exceptions=True)
            raise
        system_summaries_by_node = {
            node.node_name: summary for node, summary in zip(summary_nodes, summary_values, strict=True)
        }
        home_node_summaries: tuple[ModWebHomeNodeSummary, ...] = await self._home_node_summaries(
            sections=sections,
            user=user,
            system_summaries_by_node=system_summaries_by_node,
        )
        node_order: tuple[str, ...] = tuple[str, ...](section.node.node_name for section in sections)
        sections_by_node: dict[str, ModWebNodeAppSection] = {section.node.node_name: section for section in sections}
        summaries_by_node: dict[str, ModWebHomeNodeSummary] = {
            summary.node.node_name: summary for summary in home_node_summaries
        }
        can_view_node_system: bool = self._user_has_level(user, Power_Level.sudo)

        def _current_sections() -> tuple[ModWebNodeAppSection, ...]:
            return tuple[ModWebNodeAppSection, ...](sections_by_node[node_name] for node_name in node_order)

        def _current_summaries() -> tuple[ModWebHomeNodeSummary, ...]:
            return tuple[ModWebHomeNodeSummary, ...](
                summaries_by_node[node_name] for node_name in node_order if node_name in summaries_by_node
            )

        def _app_links_for_sections(
            current_sections: tuple[ModWebNodeAppSection, ...],
        ) -> tuple[ModWebAppLink, ...]:
            return tuple(
                app
                for section in current_sections
                for app in section.app_links
            )

        with ui.column().classes("w-full gap-6 px-4 py-8 md:px-8"):
            with ui.column().classes("mod-page w-full gap-6"):
                self._render_user_header(ui=ui, user=user)
                with ui.card().classes(f"{self._hero_card_classes()} mod-home-hero"):
                    with ui.element("div").classes("mod-app-node-badge-wrap mod-home-edge-badge-wrap"):

                        @ui.refreshable
                        def _render_home_edge_badges(current_sections: tuple[ModWebNodeAppSection, ...]) -> None:
                            home_node_latency_badges: list[_ModWebNodePresenceBadgeSpec] = []
                            with ui.row().classes("mod-app-node-badge-row mod-home-edge-badge-row"):
                                self._badge_spec(
                                    ui=ui,
                                    badge=self._app_count_badge(
                                        app_count=len(_app_links_for_sections(current_sections))
                                    ),
                                    extra_classes="mod-app-corner-badge mod-home-app-count-badge",
                                )
                                with ui.row().classes("mod-home-node-badge-list"):
                                    for section in current_sections:
                                        badge, badge_text = self._render_home_node_status_badge(
                                            ui=ui,
                                            section=section,
                                            on_click=None,
                                            extra_classes="mod-node-status-badge mod-app-corner-badge",
                                        )
                                        badge_spec = self._home_node_latency_badge_spec(
                                            badge_element=badge,
                                            text_element=badge_text,
                                            section=section,
                                            extra_classes="mod-node-status-badge mod-app-corner-badge",
                                        )
                                        if badge_spec is not None:
                                            home_node_latency_badges.append(badge_spec)
                            self._run_home_node_latency_badges_javascript(
                                ui=ui,
                                badge_specs=tuple(home_node_latency_badges),
                            )

                        _render_home_edge_badges(_current_sections())
                    with ui.column().classes(f"{self._hero_shell_classes()} mod-home-hero-shell"):
                        with ui.row().classes(f"{self._hero_header_classes()} mod-home-hero-header"):
                            with ui.column().classes("mod-hero-header-main gap-1"):
                                ui.label("Yukibot Dashboard").classes(
                                    f"{self._hero_title_classes()} mod-home-hero-title"
                                )
                        apply_home_node_stats: Callable[[tuple[ModWebHomeNodeSummary, ...]], None] = (
                            self._render_live_home_node_stats(
                                ui=ui,
                                initial_summaries=home_node_summaries,
                                system_page_enabled=can_view_node_system,
                            )
                        )

                        @ui.refreshable
                        def _render_home_capability_badges(
                            current_sections: tuple[ModWebNodeAppSection, ...],
                        ) -> None:
                            app_links: tuple[ModWebAppLink, ...] = _app_links_for_sections(current_sections)
                            unavailable_count: int = sum(
                                section.error is not None for section in current_sections
                            )
                            with ui.row().classes("mod-home-capability-badges"):
                                for capability_badge in self._node_capability_badges(
                                    app_links=app_links,
                                    unavailable_count=unavailable_count,
                                ):
                                    self._badge_spec(
                                        ui=ui,
                                        badge=capability_badge,
                                    )

                        _render_home_capability_badges(_current_sections())

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
                    _render_home_edge_badges.refresh(current_sections)
                    _render_home_capability_badges.refresh(current_sections)
                    _render_home_sections.refresh(current_sections, current_summaries)
                apply_home_node_stats(current_summaries)

            def _node_state_callback(node: ModWebNodeLink) -> Callable[[NodeStateStreamEvent], None]:
                def _handle_event(event: NodeStateStreamEvent) -> None:
                    loop.call_soon_threadsafe(lambda: _apply_node_update(node, event))

                return _handle_event

            for section in _current_sections():
                if section.is_simulated_down:
                    continue
                unsubscribe = self._create_remote_node_state_subscription(
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
            ram_value, ram_tone = self._system_ram_percent_entry(system_summary)
            storage_value, storage_tone = self._system_storage_percent_entry(system_summary)
            uptime_value, uptime_tone = self._system_bot_uptime_entry(system_summary)
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
                            label="Disk",
                            icon="storage",
                            value=storage_value,
                            tone=storage_tone,
                        ),
                        _ModWebHomeMetricSpec(
                            label="Bot Uptime",
                            icon="smart_toy",
                            value=uptime_value,
                            tone=uptime_tone,
                        ),
                    ),
                    running_text=running_text,
                    running_tone=running_tone,
                    running_tooltip=running_tooltip,
                )
            )
        return tuple[_ModWebHomeNodeStatSpec, ...](node_stats)

    @staticmethod
    def _system_chart_sample_value(
        sample: NodeSystemSample,
        metric: _ModWebSystemChartMetric,
    ) -> int | None:
        match metric:
            case _ModWebSystemChartMetric.CPU:
                return sample.cpu_percent
            case _ModWebSystemChartMetric.RAM:
                return sample.ram_percent
            case _ModWebSystemChartMetric.STORAGE:
                return sample.storage_percent

    @staticmethod
    def _append_node_system_history(
        history: NodeSystemHistory,
        summary: NodeSystemSummary,
    ) -> NodeSystemHistory:
        if summary.captured_at_epoch_seconds is None:
            return history
        sample = NodeSystemSample.from_summary(summary)
        samples = list(history.samples)
        if samples and sample.captured_at_epoch_seconds < samples[-1].captured_at_epoch_seconds:
            samples = []
        if samples:
            elapsed = sample.captured_at_epoch_seconds - samples[-1].captured_at_epoch_seconds
            if elapsed < history.sample_interval_seconds:
                samples[-1] = sample
            else:
                samples.append(sample)
        else:
            samples.append(sample)
        cutoff = sample.captured_at_epoch_seconds - history.retention_seconds
        retained = tuple(item for item in samples if item.captured_at_epoch_seconds >= cutoff)
        return replace(history, samples=retained)

    @classmethod
    def _node_system_history_svg(
        cls,
        history: NodeSystemHistory,
        *,
        animate: bool = False,
    ) -> str:
        plot_left = 48.0
        plot_top = 20.0
        plot_width = 832.0
        plot_height = 196.0
        samples = history.samples
        if not samples:
            return (
                '<div class="mod-system-chart-empty">'
                "Collecting telemetry. The first historical sample will appear shortly."
                "</div>"
            )
        end_time = samples[-1].captured_at_epoch_seconds
        start_time = end_time - history.retention_seconds
        time_span = max(1, end_time - start_time)

        def _point(sample: NodeSystemSample, value: int) -> tuple[float, float]:
            x = plot_left + (
                (sample.captured_at_epoch_seconds - start_time) / time_span
            ) * plot_width
            y = plot_top + ((100 - min(100, max(0, value))) / 100) * plot_height
            return x, y

        paths: list[str] = []
        legend: list[str] = []
        for index, series in enumerate(_SYSTEM_CHART_SERIES):
            commands: list[str] = []
            segment_open = False
            for sample in samples:
                value = cls._system_chart_sample_value(sample, series.metric)
                if value is None:
                    segment_open = False
                    continue
                x, y = _point(sample, value)
                commands.append(f"{'L' if segment_open else 'M'} {x:.1f} {y:.1f}")
                segment_open = True
            if commands:
                line_classes = "mod-system-chart-line"
                if animate:
                    line_classes = f"{line_classes} mod-system-chart-line-enter"
                paths.append(
                    f'<path class="{line_classes}" pathLength="1" '
                    f'stroke="{series.color_hex}" d="{" ".join(commands)}"/>'
                )
            legend_x = plot_left + index * 122
            legend.append(
                f'<g transform="translate({legend_x:.0f},244)">'
                f'<circle r="4" fill="{series.color_hex}"/>'
                f'<text x="10" y="4">{series.label}</text>'
                "</g>"
            )

        grid_lines: list[str] = []
        for percent in (0, 25, 50, 75, 100):
            y = plot_top + ((100 - percent) / 100) * plot_height
            grid_lines.append(
                f'<line x1="{plot_left:.0f}" y1="{y:.1f}" x2="{plot_left + plot_width:.0f}" y2="{y:.1f}"/>'
                f'<text x="{plot_left - 9:.0f}" y="{y + 4:.1f}" text-anchor="end">{percent}%</text>'
            )
        return (
            '<svg class="mod-system-chart" viewBox="0 0 900 260" role="img" '
            'aria-label="CPU, RAM, and storage usage over the last hour">'
            f'<g class="mod-system-chart-grid">{"".join(grid_lines)}</g>'
            f'<g>{"".join(paths)}</g>'
            '<g class="mod-system-chart-axis-labels">'
            f'<text x="{plot_left:.0f}" y="232">60m ago</text>'
            f'<text x="{plot_left + plot_width:.0f}" y="232" text-anchor="end">Now</text>'
            "</g>"
            f'<g class="mod-system-chart-legend">{"".join(legend)}</g>'
            "</svg>"
        )

    @staticmethod
    def _node_system_scope_badges(app_entries: tuple[NodeAppEntry, ...]) -> tuple[_ModWebBadgeSpec, ...]:
        scope_running_state: dict[str, bool] = {}
        for entry in app_entries:
            resolved_scope = entry.scope or app_scope_from_name(entry.name)
            if resolved_scope is not None and resolved_scope.strip():
                scope_value = resolved_scope.strip().casefold()
                scope_running_state[scope_value] = scope_running_state.get(scope_value, False) or entry.running
        if not scope_running_state:
            return (_ModWebBadgeSpec(text="No app scopes", tone="grey"),)

        known_scopes = tuple(scope for scope in config.AppScopes if scope.value in scope_running_state)
        known_scope_values = {scope.value for scope in known_scopes}
        badges: list[_ModWebBadgeSpec] = [
            _ModWebBadgeSpec(
                text=scope.display_name,
                tone="purple" if scope_running_state[scope.value] else "grey",
            )
            for scope in known_scopes
        ]
        badges.extend(
            _ModWebBadgeSpec(
                text=scope_value.replace("_", " ").title(),
                tone="purple" if scope_running_state[scope_value] else "grey",
            )
            for scope_value in sorted(scope_running_state.keys() - known_scope_values)
        )
        return tuple(badges)

    @staticmethod
    def _node_system_uptime_badges(system_summary: NodeSystemSummary) -> tuple[_ModWebBadgeSpec, ...]:
        badges: list[_ModWebBadgeSpec] = []
        if system_summary.uptime_seconds is not None:
            badges.append(
                _ModWebBadgeSpec(
                    text=_format_uptime_seconds(system_summary.uptime_seconds),
                    tone="black",
                    icon="dns",
                    tooltip_text="System uptime",
                )
            )
        if system_summary.bot_uptime_seconds is not None:
            badges.append(
                _ModWebBadgeSpec(
                    text=_format_uptime_seconds(system_summary.bot_uptime_seconds),
                    tone="black",
                    icon="smart_toy",
                    tooltip_text="Yukibot uptime",
                )
            )
        return tuple(badges)

    @classmethod
    def _node_system_operational_badges(
        cls,
        system_summary: NodeSystemSummary,
    ) -> tuple[_ModWebBadgeSpec, ...]:
        system_uptime = _ModWebBadgeSpec(
            text=(
                "Unavailable"
                if system_summary.uptime_seconds is None
                else _format_uptime_seconds(system_summary.uptime_seconds)
            ),
            tone="black" if system_summary.uptime_seconds is not None else "grey",
            icon="dns",
            tooltip_text="System uptime",
        )
        bot_uptime = _ModWebBadgeSpec(
            text=(
                "Unavailable"
                if system_summary.bot_uptime_seconds is None
                else _format_uptime_seconds(system_summary.bot_uptime_seconds)
            ),
            tone="black" if system_summary.bot_uptime_seconds is not None else "grey",
            icon="smart_toy",
            tooltip_text="Yukibot uptime",
        )
        cpu_points = cls._node_resource_point_badge(
            available_points=system_summary.cpu_points_available,
            capacity_points=system_summary.cpu_points_capacity,
            icon="speed",
            tooltip_text="CPU",
        ) or _ModWebBadgeSpec(text="Unavailable", tone="grey", icon="speed", tooltip_text="CPU")
        ram_points = cls._node_resource_point_badge(
            available_points=system_summary.ram_points_available,
            capacity_points=system_summary.ram_points_capacity,
            icon="memory",
            tooltip_text="RAM",
        ) or _ModWebBadgeSpec(text="Unavailable", tone="grey", icon="memory", tooltip_text="RAM")
        return (system_uptime, bot_uptime, cpu_points, ram_points)

    def _render_live_home_node_stats_renderer(
        self,
        *,
        ui: ModWebUi,
        initial_summaries: tuple[ModWebHomeNodeSummary, ...],
        system_page_enabled: bool = False,
    ) -> Callable[[tuple[ModWebHomeNodeSummary, ...]], None]:
        current_stats: tuple[_ModWebHomeNodeStatSpec, ...] = self._build_home_node_stat_specs(initial_summaries)
        rendered_bindings: list[_ModWebHomeNodeStatBindings] = []

        def _card_classes(tone: BadgeTone) -> str:
            card_classes = f"mod-home-node-card mod-home-node-card-{tone}"
            if system_page_enabled:
                card_classes = f"{card_classes} mod-home-node-card-actionable"
            return card_classes

        def _running_tooltip_html(tooltip_text: str | None) -> str:
            if tooltip_text is None:
                return ""
            return self._tooltip_lines_html((tooltip_text,)) or ""

        @ui.refreshable
        def _render_stats(stats: tuple[_ModWebHomeNodeStatSpec, ...]) -> None:
            rendered_bindings.clear()
            with ui.row().classes("mod-home-node-grid w-full gap-3"):
                for stat in stats:
                    card: Card = ui.card().classes(_card_classes(stat.card_tone))
                    if system_page_enabled:
                        target_url: str = mod_web_node_system_path(stat.node_name)
                        card.props("role=link tabindex=0")
                        card.on("click", lambda _=None, url=target_url: ui.navigate.to(url))
                        card.on(
                            "keydown.enter",
                            lambda _=None, url=target_url: ui.navigate.to(url),
                            js_handler="(event) => { event.preventDefault(); emit(); }",
                        )
                        card.on(
                            "keydown.space",
                            lambda _=None, url=target_url: ui.navigate.to(url),
                            js_handler="(event) => { event.preventDefault(); emit(); }",
                        )
                    metric_bindings: list[_ModWebHomeMetricBinding] = []
                    with card:
                        with ui.column().classes("w-full gap-3 p-3"):
                            with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                                node_text_style: str | None = self._node_text_style(node_name=stat.node_name)
                                with ui.column().classes("gap-0 min-w-0"):
                                    node_title: Label = ui.label(stat.node_label).classes("mod-home-node-title")
                                    if node_text_style is not None:
                                        node_title.style(node_text_style)
                                    node_subtitle_label: Label | None = None
                                    if stat.node_subtitle is not None:
                                        node_subtitle_label = ui.label(stat.node_subtitle).classes(
                                            "mod-home-node-subtitle"
                                        )
                                        if node_text_style is not None:
                                            node_subtitle_label.style(node_text_style)
                                status_badge = self._badge(
                                    ui=ui,
                                    text=stat.status_text or "",
                                    tone=stat.status_tone,
                                )
                                self._set_element_visibility(status_badge, visible=stat.status_text is not None)
                            with ui.element("div").classes("mod-home-node-metrics"):
                                for metric in stat.metrics:
                                    metric_row: Element = ui.row().classes("mod-home-node-metric")
                                    with metric_row:
                                        metric_icon = ui.icon(metric.icon).classes(
                                            f"mod-home-node-metric-icon mod-tone-{metric.tone}"
                                        )
                                        metric_value_label = ui.label(metric.value).classes("mod-home-node-metric-value")
                                    metric_bindings.append(
                                        _ModWebHomeMetricBinding(
                                            icon_element=metric_icon,
                                            value_label=metric_value_label,
                                        )
                                    )
                                    self._attach_text_tooltip(ui=ui, target=metric_row, text=metric.label)
                            running_row: Element = ui.row().classes("mod-home-node-running")
                            with running_row:
                                running_icon = ui.icon(_HOME_APPS_ICON).classes(
                                    f"mod-home-node-running-icon mod-tone-{stat.running_tone}"
                                )
                                running_value_label = ui.label(stat.running_text).classes("mod-home-node-running-value")
                            running_tooltip, running_tooltip_content = self._attach_html_tooltip(
                                ui=ui,
                                target=running_row,
                                html=_running_tooltip_html(stat.running_tooltip),
                            )
                    rendered_bindings.append(
                        _ModWebHomeNodeStatBindings(
                            card=card,
                            title_label=node_title,
                            subtitle_label=node_subtitle_label,
                            status_badge=status_badge,
                            metric_bindings=tuple(metric_bindings),
                            running_icon=running_icon,
                            running_value_label=running_value_label,
                            running_tooltip=_ModWebHomeTooltipBinding(
                                tooltip=running_tooltip,
                                tooltip_content=running_tooltip_content,
                            ),
                        )
                    )

        def _structure(
            stats: tuple[_ModWebHomeNodeStatSpec, ...],
        ) -> tuple[tuple[str, bool], ...]:
            return tuple((stat.node_name, stat.node_subtitle is not None) for stat in stats)

        def _apply_summaries(node_summaries: tuple[ModWebHomeNodeSummary, ...]) -> None:
            nonlocal current_stats
            stats = self._build_home_node_stat_specs(node_summaries)
            if stats == current_stats:
                return
            if _structure(stats) != _structure(current_stats):
                current_stats = stats
                _render_stats.refresh(stats)
                return
            for binding, previous_stat, next_stat in zip(rendered_bindings, current_stats, stats, strict=True):
                if previous_stat.card_tone != next_stat.card_tone:
                    binding.card.classes(replace=_card_classes(next_stat.card_tone))
                if previous_stat.node_label != next_stat.node_label:
                    binding.title_label.set_text(next_stat.node_label)
                if binding.subtitle_label is not None and previous_stat.node_subtitle != next_stat.node_subtitle:
                    if next_stat.node_subtitle is None:
                        raise RuntimeError("Home node subtitle unexpectedly disappeared without a structure change.")
                    binding.subtitle_label.set_text(next_stat.node_subtitle)
                if previous_stat.status_tone != next_stat.status_tone:
                    binding.status_badge.classes(replace=self._badge_class_name(tone=next_stat.status_tone))
                if previous_stat.status_text != next_stat.status_text:
                    binding.status_badge.set_text(next_stat.status_text or "")
                    self._set_element_visibility(binding.status_badge, visible=next_stat.status_text is not None)
                for metric_binding, previous_metric, next_metric in zip(
                    binding.metric_bindings,
                    previous_stat.metrics,
                    next_stat.metrics,
                    strict=True,
                ):
                    if previous_metric.tone != next_metric.tone:
                        metric_binding.icon_element.classes(
                            replace=f"mod-home-node-metric-icon mod-tone-{next_metric.tone}"
                        )
                    if previous_metric.value != next_metric.value:
                        metric_binding.value_label.set_text(next_metric.value)
                        self._pulse_live_value(metric_binding.value_label)
                if previous_stat.running_tone != next_stat.running_tone:
                    binding.running_icon.classes(replace=f"mod-home-node-running-icon mod-tone-{next_stat.running_tone}")
                if previous_stat.running_text != next_stat.running_text:
                    binding.running_value_label.set_text(next_stat.running_text)
                    self._pulse_live_value(binding.running_value_label)
                if previous_stat.running_tooltip != next_stat.running_tooltip:
                    self._set_html_tooltip_state(
                        binding.running_tooltip.tooltip,
                        binding.running_tooltip.tooltip_content,
                        _running_tooltip_html(next_stat.running_tooltip),
                    )
            current_stats = stats

        _render_stats(current_stats)
        return _apply_summaries

    def _render_live_home_node_stats(
        self,
        *,
        ui: ModWebUi,
        initial_summaries: tuple[ModWebHomeNodeSummary, ...],
        refresh_async_summaries: Callable[[], Awaitable[tuple[ModWebHomeNodeSummary, ...]]] | None = None,
        system_page_enabled: bool = False,
    ) -> Callable[[tuple[ModWebHomeNodeSummary, ...]], None]:
        apply_summaries: Callable[[tuple[ModWebHomeNodeSummary, ...]], None] = (
            self._render_live_home_node_stats_renderer(
                ui=ui,
                initial_summaries=initial_summaries,
                system_page_enabled=system_page_enabled,
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
                    with ui.row().classes(
                        "mod-home-section-header w-full items-center justify-between gap-1 flex-wrap"
                    ):
                        with ui.column().classes("mod-home-section-identity gap-1 min-w-0"):
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
                        with ui.row().classes("mod-home-section-resource-badges gap-2 flex-wrap"):
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

    def _render_node_system_dashboard(
        self,
        *,
        ui: ModWebUi,
        node: ModWebNodeLink,
        user: ModWebUser,
        initial_system_summary: NodeSystemSummary,
        initial_system_history: NodeSystemHistory,
        initial_app_entries: tuple[NodeAppEntry, ...],
        initial_restart_schedules: NodeRestartScheduleState | None,
        initial_restart_state: NodeRestartState | None,
        initial_portal_restart_state: NodeRestartState | None,
        initial_node_capacity: config.NodeCapacityProfile | None,
        initial_node_font_sources: config.NodeFontSourceSettings | None,
        initial_node_disk_settings: NodeDiskManagementState | None,
        current_url: str,
        simulated_down_node_names: tuple[str, ...],
        subscribe_node_state_updates: Callable[
            [Callable[[NodeStateStreamEvent], None]],
            Callable[[], None],
        ],
    ) -> None:
        self._apply_theme(ui=ui)
        current_app_entries = initial_app_entries
        current_system_summary = initial_system_summary
        with ui.column().classes("mod-page w-full gap-6 px-4 py-8 md:px-8"):
            self._render_user_header(ui=ui, user=user)
            with ui.card().classes(self._hero_card_classes()):
                operational_badge_specs = self._node_system_operational_badges(initial_system_summary)
                operational_badge_bindings: list[tuple[Element, Label]] = []
                with ui.element("div").classes("mod-app-node-badge-wrap mod-system-edge-badge-wrap"):
                    with ui.row().classes("mod-app-node-badge-row mod-system-edge-badge-row"):
                        for badge in operational_badge_specs:
                            if badge.icon is None:
                                raise RuntimeError("System operational badges require icons.")
                            operational_badge_bindings.append(
                                ModWebUiHelpersMixin._badge_icon_parts(
                                    ui=ui,
                                    text=badge.text,
                                    tone=badge.tone,
                                    icon=badge.icon,
                                    extra_classes="mod-app-corner-badge mod-system-corner-badge",
                                    tooltip_text=badge.tooltip_text,
                                )
                            )
                with ui.column().classes(f"{self._hero_shell_classes()} mod-system-hero-shell"):
                    with ui.element("div").classes("mod-system-hero-header"):
                        with ui.row().classes(
                            "mod-system-hero-identity items-center gap-3 flex-nowrap"
                        ):
                            ui.html(
                                self._node_bot_avatar_markup(
                                    node_name=node.node_name,
                                    display_name=node.label,
                                    extra_class="mod-system-hero-avatar",
                                )
                            ).classes("shrink-0")
                            with ui.column().classes("gap-1 min-w-0"):
                                node_text_style: str | None = self._node_text_style(node_name=node.node_name)
                                node_title: Label = ui.label(node.label).classes(self._hero_title_classes())
                                if node_text_style is not None:
                                    node_title.style(node_text_style)
                                ui.label(f"{node.node_name} system monitoring").classes(self._hero_support_classes())
                        with ui.column().classes("mod-system-scope-slot"):

                            @ui.refreshable
                            def _render_system_scope_badges(badges: tuple[_ModWebBadgeSpec, ...]) -> None:
                                with ui.row().classes("mod-system-scope-badges"):
                                    for badge in badges:
                                        self._badge_spec(ui=ui, badge=badge)

                            current_scope_badges = self._node_system_scope_badges(current_app_entries)
                            _render_system_scope_badges(current_scope_badges)

                    apply_system_stats: Callable[[tuple[ModWebTitleStat, ...]], None] = (
                        self._render_live_title_stats(
                            ui=ui,
                            initial_stats=self._build_node_system_stats(initial_system_summary),
                        )
                    )

            current_history = self._append_node_system_history(
                initial_system_history,
                initial_system_summary,
            )
            with ui.card().classes("mod-card w-full"):
                with ui.column().classes("w-full gap-3 p-4"):
                    with ui.row().classes("w-full items-center justify-between gap-2 flex-wrap"):
                        with ui.column().classes("gap-0"):
                            ui.label("Utilisation history").classes("text-lg font-black mod-title-small")
                            ui.label("One-hour rolling window · streamed live").classes("mod-subtitle text-xs")
                        self._badge(
                            ui=ui,
                            text=f"{initial_system_history.sample_interval_seconds}s samples",
                            tone="grey",
                        )
                    chart: Html = ui.html(self._node_system_history_svg(current_history, animate=True))
                    chart.classes("mod-system-chart-shell w-full")

            can_manage_node_configuration = self._user_has_level(user, Power_Level.root)
            self._render_node_system_properties(
                ui=ui,
                node=node,
                user=user,
                can_manage_node_configuration=can_manage_node_configuration,
                initial_capacity=initial_node_capacity,
                initial_font_sources=initial_node_font_sources,
                initial_disk_settings=initial_node_disk_settings,
                current_url=current_url,
                simulated_down_node_names=simulated_down_node_names,
            )
            self._render_node_system_actions(
                ui=ui,
                node=node,
                user=user,
                initial_restart_schedules=initial_restart_schedules,
                initial_restart_state=initial_restart_state,
                initial_portal_restart_state=initial_portal_restart_state,
            )

            page_closed = False
            loop: AbstractEventLoop = asyncio.get_running_loop()

            def _apply_update(event: NodeStateStreamEvent) -> None:
                nonlocal current_app_entries, current_history, current_scope_badges
                nonlocal current_system_summary, operational_badge_specs
                if page_closed:
                    return
                if event.app_entries is not None:
                    current_app_entries = event.app_entries
                    next_scope_badges = self._node_system_scope_badges(current_app_entries)
                    if next_scope_badges != current_scope_badges:
                        current_scope_badges = next_scope_badges
                        _render_system_scope_badges.refresh(current_scope_badges)
                if event.system_summary is None:
                    return
                current_system_summary = event.system_summary
                next_operational_badges = self._node_system_operational_badges(current_system_summary)
                for index, (previous_badge, next_badge) in enumerate(
                    zip(operational_badge_specs, next_operational_badges, strict=True)
                ):
                    badge_element, value_label = operational_badge_bindings[index]
                    if previous_badge.text != next_badge.text:
                        value_label.set_text(next_badge.text)
                    if previous_badge.tone != next_badge.tone:
                        badge_element.classes(
                            replace=self._badge_class_name(
                                tone=next_badge.tone,
                                extra_classes=(
                                    "mod-badge-icon-label mod-app-corner-badge mod-system-corner-badge"
                                ),
                            )
                        )
                operational_badge_specs = next_operational_badges
                apply_system_stats(self._build_node_system_stats(current_system_summary))
                next_history = self._append_node_system_history(current_history, current_system_summary)
                if next_history != current_history:
                    current_history = next_history
                    chart.set_content(self._node_system_history_svg(current_history))

            def _handle_update(event: NodeStateStreamEvent) -> None:
                loop.call_soon_threadsafe(lambda: _apply_update(event))

            unsubscribe: Callable[[], None] = subscribe_node_state_updates(_handle_update)

            def _cleanup_live_updates() -> None:
                nonlocal page_closed
                page_closed = True
                unsubscribe()

            self._register_client_cleanup(ui=ui, cleanup=_cleanup_live_updates)

    @staticmethod
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

    @staticmethod
    def _node_settings_field_spec(
        key: _ModWebNodeSettingsFieldKey,
    ) -> _ModWebNodeSettingsNumberFieldSpec:
        for row_specs in _NODE_SETTINGS_CAPACITY_FIELD_ROWS:
            for field_spec in row_specs:
                if field_spec.key is key:
                    return field_spec
        raise RuntimeError(f"Missing node settings field spec for key: {key!r}")

    @staticmethod
    def _build_node_disk_preferences(
        *,
        initial_settings: NodeDiskManagementState,
        selected_activity_mounts: tuple[str, ...],
        primary_choice: str,
        secondary_choice: str,
        label_values: dict[str, str],
    ) -> config.PersistedDiskPreferences:
        discovered_mounts = tuple(disk.mountpoint for disk in initial_settings.disks)
        discovered_mount_set = set(discovered_mounts)
        selected_activity_set = set(selected_activity_mounts)
        if not selected_activity_set <= discovered_mount_set:
            raise ValueError("Activity disks contain an unknown mountpoint.")

        primary_mount = (
            None
            if primary_choice == _ModWebNodeDiskChoice.DEFAULT_PRIMARY.value
            else primary_choice
        )
        allowed_primary_mounts = discovered_mount_set | {
            initial_settings.preferences.primary_mount
        }
        if primary_mount is not None and primary_mount not in allowed_primary_mounts:
            raise ValueError("Primary disk is not available.")
        secondary_mount = (
            None
            if secondary_choice == _ModWebNodeDiskChoice.NO_SECONDARY.value
            else secondary_choice
        )
        allowed_secondary_mounts = discovered_mount_set | {
            initial_settings.preferences.secondary_mount
        }
        if secondary_mount is not None and secondary_mount not in allowed_secondary_mounts:
            raise ValueError("Secondary disk is not available.")

        initial_activity_set = {
            disk.mountpoint for disk in initial_settings.disks if disk.is_activity
        }
        activity_mounts = (
            initial_settings.preferences.activity_mounts
            if selected_activity_set == initial_activity_set
            else [
                mountpoint
                for mountpoint in discovered_mounts
                if mountpoint in selected_activity_set
            ]
        )
        labels = {
            mountpoint: label
            for mountpoint, label in initial_settings.preferences.labels.items()
            if mountpoint not in discovered_mount_set
        }
        for mountpoint in discovered_mounts:
            label = label_values.get(mountpoint)
            if label is None:
                raise RuntimeError(f"Missing disk label control for {mountpoint!r}.")
            if stripped_label := label.strip():
                labels[mountpoint] = stripped_label

        return config.PersistedDiskPreferences(
            activity_mounts=(list(activity_mounts) if activity_mounts is not None else None),
            labels=labels,
            primary_mount=primary_mount,
            secondary_mount=secondary_mount,
        )

    def _render_node_system_properties(
        self,
        *,
        ui: ModWebUi,
        node: ModWebNodeLink,
        user: ModWebUser,
        can_manage_node_configuration: bool,
        initial_capacity: config.NodeCapacityProfile | None,
        initial_font_sources: config.NodeFontSourceSettings | None,
        initial_disk_settings: NodeDiskManagementState | None,
        current_url: str,
        simulated_down_node_names: tuple[str, ...],
    ) -> None:
        field_inputs: dict[_ModWebNodeSettingsFieldKey, Input] = {}
        disk_activity_checkboxes: dict[str, Checkbox] = {}
        disk_label_inputs: dict[str, Input] = {}
        primary_disk_select: Select | None = None
        secondary_disk_select: Select | None = None

        async def _handle_toggle_simulated_down(_: object | None = None) -> None:
            target_url = self._toggle_simulated_down_node_url(
                current_url=current_url,
                node_name=node.node_name,
                simulated_down_node_names=simulated_down_node_names,
            )
            ui.navigate.to(target_url)

        with ui.card().classes("mod-card w-full"):
            with ui.column().classes("w-full gap-4 p-4"):
                with ui.column().classes("gap-0"):
                    ui.label("Properties").classes("text-lg font-black mod-title-small")
                    ui.label(f"Node-specific settings for {node.label}.").classes("mod-subtitle text-xs")

                if initial_font_sources is None:
                    ui.label("Properties are unavailable. Reload the page to try again.").classes(
                        "mod-subtitle text-sm"
                    )
                else:
                    capacity_values: dict[_ModWebNodeSettingsFieldKey, int] | None = (
                        None
                        if initial_capacity is None
                        else {
                            _ModWebNodeSettingsFieldKey.CPU_TOTAL: initial_capacity.cpu_points_total,
                            _ModWebNodeSettingsFieldKey.RAM_TOTAL: initial_capacity.ram_points_total,
                            _ModWebNodeSettingsFieldKey.CPU_RESERVED: initial_capacity.cpu_points_reserved,
                            _ModWebNodeSettingsFieldKey.RAM_RESERVED: initial_capacity.ram_points_reserved,
                        }
                    )
                    with ui.column().classes("mod-app-details-section"):
                        capacity_section = ui.column().classes("mod-app-details-subsection")
                        capacity_section.set_visibility(can_manage_node_configuration)
                        with capacity_section:
                            ui.label("Capacity").classes("mod-stat-label")
                            ui.label("Adjust total capacity and reserved headroom for this node.").classes(
                                "mod-subtitle text-xs"
                            )
                            if capacity_values is None:
                                ui.label("Capacity settings are unavailable.").classes("mod-subtitle text-sm")
                            else:
                                for row_specs in _NODE_SETTINGS_CAPACITY_FIELD_ROWS:
                                    with ui.row().classes("w-full gap-2 flex-wrap"):
                                        for field_spec in row_specs:
                                            field_inputs[field_spec.key] = (
                                                ui.input(field_spec.label, value=str(capacity_values[field_spec.key]))
                                                .props(
                                                    "filled square dense hide-bottom-space color=accent "
                                                    "type=number inputmode=numeric step=1 min=0"
                                                )
                                                .classes("mod-app-details-field mod-app-details-point-field")
                                            )

                        with ui.column().classes("mod-app-details-subsection"):
                            ui.label("Title Fonts").classes("mod-stat-label")
                            ui.label(
                                "Add Google Fonts specimen or CSS URLs for app title fonts."
                            ).classes("mod-subtitle text-xs")
                            font_sources_dialog = ui.dialog()
                            with font_sources_dialog:
                                with ui.card().classes("mod-card mod-dialog-card"):
                                    with ui.column().classes("w-full gap-4 p-5"):
                                        google_font_urls_input = (
                                            ui.textarea(
                                                label="Google Font URLs",
                                                value="\n".join(initial_font_sources.google_font_urls),
                                            )
                                            .props("filled square autogrow hide-bottom-space color=accent")
                                            .classes("mod-app-details-field mod-app-details-notes")
                                        )
                                        with ui.row().classes("w-full justify-end"):
                                            ui.button("Done", on_click=font_sources_dialog.close).classes(
                                                "mod-list-button"
                                            )
                            ui.button("Edit Google Fonts", on_click=font_sources_dialog.open).classes(
                                "mod-list-button secondary"
                            )

                        disk_section = ui.column().classes("mod-app-details-subsection")
                        disk_section.set_visibility(can_manage_node_configuration)
                        with disk_section:
                            ui.label("Disks").classes("mod-stat-label")
                            ui.label(
                                "Choose activity, primary, and secondary disks, and optionally override labels."
                            ).classes("mod-subtitle text-xs")
                            if initial_disk_settings is None:
                                ui.label("Disk settings are unavailable. Reload the page to try again.").classes(
                                    "mod-subtitle text-sm"
                                )
                            elif not initial_disk_settings.disks:
                                ui.label("No manageable disks were discovered.").classes("mod-subtitle text-sm")
                            else:
                                disk_options = {
                                    disk.mountpoint: f"{disk.display_name} · {disk.mountpoint}"
                                    for disk in initial_disk_settings.disks
                                }
                                bot_disk = next(
                                    (disk for disk in initial_disk_settings.disks if disk.is_bot_disk),
                                    None,
                                )
                                primary_options = {
                                    _ModWebNodeDiskChoice.DEFAULT_PRIMARY.value: (
                                        "Bot Disk (default)"
                                        if bot_disk is None
                                        else f"Bot Disk (default) · {bot_disk.mountpoint}"
                                    ),
                                    **disk_options,
                                }
                                configured_primary_mount = initial_disk_settings.preferences.primary_mount
                                if (
                                    configured_primary_mount is not None
                                    and configured_primary_mount not in disk_options
                                ):
                                    primary_options[configured_primary_mount] = (
                                        f"Unavailable · {configured_primary_mount}"
                                    )
                                secondary_options = {
                                    _ModWebNodeDiskChoice.NO_SECONDARY.value: "Not configured",
                                    **disk_options,
                                }
                                configured_secondary_mount = initial_disk_settings.preferences.secondary_mount
                                if (
                                    configured_secondary_mount is not None
                                    and configured_secondary_mount not in disk_options
                                ):
                                    secondary_options[configured_secondary_mount] = (
                                        f"Unavailable · {configured_secondary_mount}"
                                    )
                                with ui.row().classes("w-full gap-2 flex-wrap"):
                                    primary_disk_select = (
                                        ui.select(
                                            options=primary_options,
                                            value=(
                                                initial_disk_settings.preferences.primary_mount
                                                or _ModWebNodeDiskChoice.DEFAULT_PRIMARY.value
                                            ),
                                            label="Primary Disk",
                                        )
                                        .props(
                                            "filled square dense hide-bottom-space color=accent options-dark "
                                            "popup-content-class=mod-setting-menu"
                                        )
                                        .classes("mod-app-details-field mod-system-disk-select")
                                    )
                                    secondary_disk_select = (
                                        ui.select(
                                            options=secondary_options,
                                            value=(
                                                initial_disk_settings.preferences.secondary_mount
                                                or _ModWebNodeDiskChoice.NO_SECONDARY.value
                                            ),
                                            label="Secondary Disk",
                                        )
                                        .props(
                                            "filled square dense hide-bottom-space color=accent options-dark "
                                            "popup-content-class=mod-setting-menu"
                                        )
                                        .classes("mod-app-details-field mod-system-disk-select")
                                    )
                                with ui.column().classes("w-full gap-2"):
                                    for disk in initial_disk_settings.disks:
                                        with ui.element("div").classes("mod-system-disk-property-row"):
                                            with ui.column().classes("gap-0 min-w-0 mod-system-disk-property-identity"):
                                                ui.label(disk.display_name).classes("mod-stat-label")
                                                ui.label(disk.mountpoint).classes(
                                                    "mod-subtitle text-xs mod-system-disk-mountpoint"
                                                )
                                            disk_activity_checkboxes[disk.mountpoint] = (
                                                ui.checkbox("Activity", value=disk.is_activity)
                                                .props("dense color=accent")
                                                .classes("mod-app-details-toggle")
                                            )
                                            disk_label_inputs[disk.mountpoint] = (
                                                ui.input(
                                                    "Label Override",
                                                    value=initial_disk_settings.preferences.labels.get(
                                                        disk.mountpoint,
                                                        "",
                                                    ),
                                                )
                                                .props("filled square dense hide-bottom-space color=accent")
                                                .classes("mod-app-details-field mod-system-disk-label-field")
                                            )

                    async def _save_properties(_: object | None = None) -> None:
                        try:
                            def _capacity_value(key: _ModWebNodeSettingsFieldKey) -> int:
                                return self._parse_required_non_negative_int(
                                    raw_value=_value_as_text(field_inputs[key]),
                                    field_label=self._node_settings_field_spec(key).field_label,
                                )

                            capacity: config.NodeCapacityProfile | None = None
                            if can_manage_node_configuration and field_inputs:
                                capacity = config.NodeCapacityProfile(
                                    cpu_points_total=_capacity_value(_ModWebNodeSettingsFieldKey.CPU_TOTAL),
                                    ram_points_total=_capacity_value(_ModWebNodeSettingsFieldKey.RAM_TOTAL),
                                    cpu_points_reserved=_capacity_value(_ModWebNodeSettingsFieldKey.CPU_RESERVED),
                                    ram_points_reserved=_capacity_value(_ModWebNodeSettingsFieldKey.RAM_RESERVED),
                                )
                            font_sources = config.NodeFontSourceSettings(
                                google_font_urls=config.normalise_google_font_source_urls(
                                    _value_as_text(google_font_urls_input)
                                )
                            )
                            disk_preferences: config.PersistedDiskPreferences | None = None
                            if (
                                can_manage_node_configuration
                                and initial_disk_settings is not None
                                and initial_disk_settings.disks
                            ):
                                if primary_disk_select is None or secondary_disk_select is None:
                                    raise RuntimeError("Node disk selection controls are unavailable.")
                                disk_preferences = self._build_node_disk_preferences(
                                    initial_settings=initial_disk_settings,
                                    selected_activity_mounts=tuple(
                                        disk.mountpoint
                                        for disk in initial_disk_settings.disks
                                        if _value_as_bool(disk_activity_checkboxes[disk.mountpoint])
                                    ),
                                    primary_choice=_value_as_text(primary_disk_select),
                                    secondary_choice=_value_as_text(secondary_disk_select),
                                    label_values={
                                        mountpoint: _value_as_text(label_input)
                                        for mountpoint, label_input in disk_label_inputs.items()
                                    },
                                )
                        except (TypeError, ValueError) as xcp:
                            ui.notify(str(xcp), type="negative")
                            return
                        save_button.disable()
                        try:
                            font_source_result = await self._update_node_font_sources(
                                node_name=node.node_name,
                                user=user,
                                settings=font_sources,
                            )
                            result_messages = [font_source_result.message]
                            if capacity is not None:
                                capacity_result = await self._update_node_capacity(
                                    node_name=node.node_name,
                                    user=user,
                                    capacity=capacity,
                                )
                                result_messages.append(capacity_result.message)
                            if disk_preferences is not None:
                                disk_result = await self._update_node_disk_settings(
                                    node_name=node.node_name,
                                    user=user,
                                    preferences=disk_preferences,
                                )
                                result_messages.append(disk_result.message)
                        except Exception as xcp:
                            log.warning("Node properties update failed: node=%s error=%s", node.node_name, xcp)
                            ui.notify(f"Node properties update failed: {xcp}", type="negative")
                            save_button.enable()
                            return
                        ui.notify(" ".join(result_messages), type="positive")
                        self._guarded_reload(ui=ui)

                    with ui.row().classes("w-full justify-end"):
                        save_button = ui.button("Save", on_click=_save_properties).classes("mod-list-button")

                if config.INDEV and can_manage_node_configuration:
                    with ui.column().classes("mod-app-details-section"):
                        ui.label("Dev").classes("mod-stat-label")
                        simulate_button_text = (
                            "Restore Availability"
                            if node.node_name.casefold()
                            in {configured_name.casefold() for configured_name in simulated_down_node_names}
                            else "Simulate Down"
                        )
                        ui.button(simulate_button_text, on_click=_handle_toggle_simulated_down).classes(
                            "mod-list-button secondary"
                        )

    def _render_node_system_actions(
        self,
        *,
        ui: ModWebUi,
        node: ModWebNodeLink,
        user: ModWebUser,
        initial_restart_schedules: NodeRestartScheduleState | None,
        initial_restart_state: NodeRestartState | None,
        initial_portal_restart_state: NodeRestartState | None,
    ) -> None:
        action_buttons: list[Button] = []
        from nicegui.context import context as nicegui_context

        action_client = nicegui_context.client
        actions_closed = False

        def _close_actions() -> None:
            nonlocal actions_closed
            actions_closed = True

        self._register_client_cleanup(ui=ui, cleanup=_close_actions)

        def _notify_error(message: str, *, multi_line: bool = False) -> None:
            if actions_closed:
                return
            with action_client:
                ui.notify(message, type="negative", multi_line=multi_line)

        def _notify_success(message: str) -> None:
            if actions_closed:
                return
            with action_client:
                ui.notify(message, type="positive")

        def _notify_ongoing(message: str) -> None:
            if actions_closed:
                return
            with action_client:
                ui.notify(message, type="ongoing", close_button=True, timeout=15_000)

        def _create_confirm_handler(
            *,
            spec: _ModWebSystemActionSpec,
            dialog: Dialog,
            auto_restart_running_apps_checkbox: Checkbox,
            silent_checkbox: Checkbox,
        ) -> Callable[[], Awaitable[None]]:
            async def _confirm() -> None:
                for action_button in action_buttons:
                    action_button.disable()
                try:
                    result = await self._remote_node_system_action_async(
                        node,
                        spec.action,
                        _value_as_bool(auto_restart_running_apps_checkbox),
                        _value_as_bool(silent_checkbox),
                        user,
                    )
                except Exception as xcp:
                    if actions_closed:
                        return
                    for action_button in action_buttons:
                        action_button.enable()
                    _notify_error(f"Unable to schedule {spec.title.lower()}: {xcp}", multi_line=True)
                    return
                if actions_closed:
                    return
                dialog.close()
                _notify_ongoing(result.message)

            return _confirm

        with ui.card().classes("mod-card mod-system-danger-card w-full"):
            with ui.column().classes("w-full gap-4 p-4"):
                ui.label("Root actions").classes("text-lg font-black mod-title-small")
                with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                    auto_restart_running_apps_checkbox = ui.checkbox(
                        "Auto-restart running apps",
                        value=True,
                    ).props("dense color=accent").classes(
                        "mod-app-details-toggle mod-system-auto-restart-toggle"
                    )
                    silent_checkbox = ui.checkbox(
                        "Silent",
                        value=False,
                    ).props("dense color=accent").classes(
                        "mod-app-details-toggle mod-system-silent-toggle"
                    )
                    for spec in _SYSTEM_ACTION_SPECS:
                        if spec.required_target is not None and (
                            initial_restart_schedules is None
                            or all(
                                entry.target is not spec.required_target
                                for entry in initial_restart_schedules.schedules
                            )
                        ):
                            continue
                        dialog = ui.dialog()
                        with dialog:
                            with ui.card().classes("mod-card mod-dialog-card"):
                                with ui.column().classes("w-full gap-4 p-5"):
                                    ui.label(f"{spec.title}?").classes("text-xl font-black mod-title-small")
                                    ui.label(node.label).classes("mod-subtitle text-sm")
                                    with ui.row().classes("w-full justify-end gap-2"):
                                        ui.button("Cancel", on_click=dialog.close).classes("mod-list-button secondary")
                                        confirm_button = ui.button(spec.button_label).classes("mod-list-button danger")
                                        action_buttons.append(confirm_button)
                                        confirm_button.on(
                                            "click",
                                            _create_confirm_handler(
                                                spec=spec,
                                                dialog=dialog,
                                                auto_restart_running_apps_checkbox=(
                                                    auto_restart_running_apps_checkbox
                                                ),
                                                silent_checkbox=silent_checkbox,
                                            ),
                                        )
                        open_button = ui.button(spec.button_label, on_click=dialog.open).classes(
                            "mod-list-button danger"
                        )
                        action_buttons.append(open_button)

                ui.label("Restart schedules").classes("text-base font-black mod-title-small")
                if initial_restart_schedules is None:
                    ui.label("Unavailable").classes("mod-subtitle text-sm")
                    return
                if initial_restart_state is not None:
                    ui.label(
                        _format_restart_state_line(
                            "Bot",
                            initial_restart_state.process.timestamp,
                            initial_restart_state.process.kind.value,
                            "Australia/Melbourne",
                        )
                    ).classes(_RESTART_STATE_LINE_CLASSES)
                    if initial_restart_state.voice is not None:
                        ui.label(
                            _format_restart_state_line(
                                "Voice",
                                initial_restart_state.voice.timestamp,
                                initial_restart_state.voice.kind.value,
                                "Australia/Melbourne",
                            )
                        ).classes(_RESTART_STATE_LINE_CLASSES)
                if initial_portal_restart_state is not None:
                    ui.label(
                        _format_restart_state_line(
                            "Portal",
                            initial_portal_restart_state.process.timestamp,
                            initial_portal_restart_state.process.kind.value,
                            "Australia/Melbourne",
                        )
                    ).classes(_RESTART_STATE_LINE_CLASSES)

                def _schedule_status(entry: NodeRestartScheduleEntry) -> str:
                    if not entry.enabled:
                        return "Off"
                    interval = MaintenanceService.format_interval_minutes(entry.interval_minutes)
                    return f"Every {interval}"

                async def _set_client_restart_time(label: Label, timestamp: int | None) -> None:
                    if actions_closed:
                        return
                    if timestamp is None:
                        label.set_text("—")
                        return
                    script = (
                        f"const date = new Date({timestamp * 1000});"
                        "const dateTime = new Intl.DateTimeFormat(undefined, {"
                        "weekday: 'short', day: '2-digit', month: 'short', year: 'numeric', "
                        "hour: '2-digit', minute: '2-digit', hourCycle: 'h23'"
                        "}).format(date);"
                        "const timezonePart = new Intl.DateTimeFormat(undefined, {timeZoneName: 'short'})"
                        ".formatToParts(date).find(part => part.type === 'timeZoneName');"
                        "return `${dateTime} [${timezonePart?.value ?? 'local'}]`;"
                    )
                    try:
                        formatted = await cast(Awaitable[object], ui.run_javascript(script))
                    except Exception:
                        if actions_closed:
                            return
                        label.set_text("unavailable")
                        return
                    if actions_closed:
                        return
                    label.set_text(str(formatted))

                def _set_schedule_display(
                    entry: NodeRestartScheduleEntry,
                    *,
                    status_label: Label,
                    timezone_labels: dict[str, Label],
                    client_label: Label,
                ) -> None:
                    status_label.set_text(_schedule_status(entry))
                    for timezone_label, timezone_name in _RESTART_DISPLAY_TIMEZONES:
                        value = (
                            "—"
                            if entry.next_restart_timestamp is None
                            else _format_restart_timestamp(entry.next_restart_timestamp, timezone_name)
                        )
                        timezone_labels[timezone_name].set_text(f"{timezone_label} · {value}")
                    ui.timer(
                        0.01,
                        lambda: _set_client_restart_time(client_label, entry.next_restart_timestamp),
                        once=True,
                    )

                schedules_by_target: dict[RestartTarget, NodeRestartScheduleEntry] = {
                    entry.target: entry for entry in initial_restart_schedules.schedules
                }

                def _set_schedule_buttons_busy(
                    *,
                    schedule_target: RestartTarget,
                    save: Button,
                    skip: Button,
                    disable: Button,
                    busy: bool,
                ) -> None:
                    if busy:
                        save.disable()
                        skip.disable()
                        disable.disable()
                        return
                    save.enable()
                    disable.enable()
                    if schedules_by_target[schedule_target].enabled:
                        skip.enable()
                    else:
                        skip.disable()

                for target, initial_entry in schedules_by_target.items():
                    initial_days, initial_hours, initial_minutes = _restart_interval_parts(
                        initial_entry.interval_minutes
                    )
                    initial_hours_input = _format_restart_hours_input(initial_hours, initial_minutes)
                    initial_anchor_timestamp = initial_entry.anchor_timestamp or initial_entry.next_restart_timestamp
                    if initial_anchor_timestamp is None:
                        initial_anchor_timestamp = int(datetime.now().timestamp()) + initial_entry.interval_minutes * 60
                    initial_anchor_at = datetime.fromtimestamp(initial_anchor_timestamp, ZoneInfo("UTC"))
                    initial_anchor_weekday = _RESTART_WEEKDAYS[initial_anchor_at.weekday()].value
                    initial_anchor_time = initial_anchor_at.strftime("%H:%M")
                    with ui.column().classes("mod-system-schedule-row w-full gap-3"):
                        with ui.row().classes("w-full items-start justify-between gap-4"):
                            with ui.column().classes("gap-1"):
                                ui.label(target.value.title()).classes("font-black mod-title-small")
                                status_label = ui.label(_schedule_status(initial_entry)).classes("mod-subtitle text-xs")
                            with ui.row().classes("gap-2"):
                                save_button = ui.button("Save").classes("mod-list-button")
                                skip_button = ui.button("Skip").classes("mod-list-button secondary")
                                disable_button = ui.button("Disable").classes("mod-list-button secondary")
                                if not initial_entry.enabled:
                                    skip_button.disable()

                        with ui.row().classes("mod-system-schedule-controls w-full gap-2"):
                            days_input = ui.input("Days", value=str(initial_days)).props(
                                f"{_RESTART_SCHEDULE_FIELD_PROPS} type=number min=0 max=7 step=1"
                            ).classes("w-full mod-system-schedule-field")
                            hours_input = ui.input(
                                "Hours",
                                value=initial_hours_input,
                                placeholder="H or H:MM",
                            ).props(
                                f"{_RESTART_SCHEDULE_FIELD_PROPS} maxlength=5 inputmode=text"
                            ).classes("w-full mod-system-schedule-field")
                            weekday_select = ui.select(
                                _RESTART_WEEKDAY_OPTIONS,
                                value=initial_anchor_weekday,
                                label="Anchor day",
                            ).props(
                                f"{_RESTART_SCHEDULE_FIELD_PROPS} "
                                "options-dark popup-content-class=mod-setting-menu"
                            ).classes("w-full mod-system-schedule-field mod-system-schedule-weekday")
                            anchor_time_input = ui.input("Anchor time", value=initial_anchor_time).props(
                                f"{_RESTART_SCHEDULE_FIELD_PROPS} "
                                "mask=##:## maxlength=5 inputmode=numeric placeholder=HH:MM"
                            ).classes("w-full mod-system-schedule-field mod-system-schedule-time")
                            timezone_select = ui.select(
                                _RESTART_TIMEZONE_OPTIONS,
                                value="UTC",
                                label="Anchor timezone",
                            ).props(
                                f"{_RESTART_SCHEDULE_FIELD_PROPS} "
                                "options-dark popup-content-class=mod-setting-menu"
                            ).classes("w-full mod-system-schedule-field mod-system-schedule-timezone")

                        with ui.row().classes("w-full items-baseline gap-1 flex-wrap"):
                            ui.label("Next restart:").classes("text-xs font-black mod-title-small")
                            client_time_label = ui.label("loading…").classes("mod-subtitle text-xs")
                        timezone_labels: dict[str, Label] = {}
                        with ui.grid(columns=2).classes("w-full gap-x-6 gap-y-1"):
                            for timezone_label, timezone_name in _RESTART_DISPLAY_TIMEZONES:
                                value = (
                                    "—"
                                    if initial_entry.next_restart_timestamp is None
                                    else _format_restart_timestamp(
                                        initial_entry.next_restart_timestamp,
                                        timezone_name,
                                    )
                                )
                                timezone_labels[timezone_name] = ui.label(
                                    f"{timezone_label} · {value}"
                                ).classes("mod-subtitle text-xs")

                        _set_schedule_display(
                            initial_entry,
                            status_label=status_label,
                            timezone_labels=timezone_labels,
                            client_label=client_time_label,
                        )

                        async def _save_schedule(
                            *,
                            schedule_target: RestartTarget = target,
                            schedule_days_input: Input = days_input,
                            schedule_hours_input: Input = hours_input,
                            schedule_weekday_select: Select = weekday_select,
                            schedule_anchor_time_input: Input = anchor_time_input,
                            schedule_timezone_select: Select = timezone_select,
                            schedule_status_label: Label = status_label,
                            schedule_timezone_labels: dict[str, Label] = timezone_labels,
                            schedule_client_label: Label = client_time_label,
                            schedule_save_button: Button = save_button,
                            schedule_skip_button: Button = skip_button,
                            schedule_disable_button: Button = disable_button,
                        ) -> None:
                            try:
                                interval_days = int(_value_as_text(schedule_days_input))
                            except ValueError:
                                _notify_error("Use a whole number of days.")
                                return
                            try:
                                interval_hours, interval_remainder_minutes = _parse_restart_hours_input(
                                    _value_as_text(schedule_hours_input)
                                )
                            except ValueError as xcp:
                                _notify_error(str(xcp))
                                return
                            try:
                                interval_minutes = _restart_interval_from_parts(
                                    days=interval_days,
                                    hours=interval_hours,
                                    minutes=interval_remainder_minutes,
                                )
                            except ValueError as xcp:
                                _notify_error(str(xcp))
                                return
                            _set_schedule_buttons_busy(
                                schedule_target=schedule_target,
                                save=schedule_save_button,
                                skip=schedule_skip_button,
                                disable=schedule_disable_button,
                                busy=True,
                            )

                            timezone_name = _value_as_text(schedule_timezone_select)
                            if timezone_name == _CLIENT_TIMEZONE_VALUE:
                                try:
                                    raw_timezone = await cast(
                                        Awaitable[object],
                                        ui.run_javascript("return Intl.DateTimeFormat().resolvedOptions().timeZone;"),
                                    )
                                except Exception as xcp:
                                    _set_schedule_buttons_busy(
                                        schedule_target=schedule_target,
                                        save=schedule_save_button,
                                        skip=schedule_skip_button,
                                        disable=schedule_disable_button,
                                        busy=False,
                                    )
                                    _notify_error(f"Unable to detect the client timezone: {xcp}")
                                    return
                                if actions_closed:
                                    return
                                if not isinstance(raw_timezone, str) or not raw_timezone:
                                    _set_schedule_buttons_busy(
                                        schedule_target=schedule_target,
                                        save=schedule_save_button,
                                        skip=schedule_skip_button,
                                        disable=schedule_disable_button,
                                        busy=False,
                                    )
                                    _notify_error("The browser did not provide a timezone.")
                                    return
                                timezone_name = raw_timezone
                            try:
                                anchor_weekday = _RestartWeekday(_value_as_text(schedule_weekday_select))
                            except ValueError:
                                _set_schedule_buttons_busy(
                                    schedule_target=schedule_target,
                                    save=schedule_save_button,
                                    skip=schedule_skip_button,
                                    disable=schedule_disable_button,
                                    busy=False,
                                )
                                _notify_error("Choose an anchor day.")
                                return
                            try:
                                anchor_timestamp = _restart_anchor_timestamp(
                                    anchor_weekday,
                                    _value_as_text(schedule_anchor_time_input),
                                    timezone_name,
                                )
                            except ValueError as xcp:
                                _set_schedule_buttons_busy(
                                    schedule_target=schedule_target,
                                    save=schedule_save_button,
                                    skip=schedule_skip_button,
                                    disable=schedule_disable_button,
                                    busy=False,
                                )
                                _notify_error(str(xcp))
                                return
                            try:
                                state = await self._remote_update_restart_schedule_async(
                                    node,
                                    schedule_target,
                                    interval_minutes,
                                    anchor_timestamp,
                                    user,
                                )
                            except Exception as xcp:
                                if actions_closed:
                                    return
                                _set_schedule_buttons_busy(
                                    schedule_target=schedule_target,
                                    save=schedule_save_button,
                                    skip=schedule_skip_button,
                                    disable=schedule_disable_button,
                                    busy=False,
                                )
                                _notify_error(f"Unable to save schedule: {xcp}", multi_line=True)
                                return
                            if actions_closed:
                                return
                            entry = next(item for item in state.schedules if item.target is schedule_target)
                            schedules_by_target[schedule_target] = entry
                            next_days, next_hours, next_minutes = _restart_interval_parts(entry.interval_minutes)
                            schedule_days_input.set_value(str(next_days))
                            schedule_hours_input.set_value(
                                _format_restart_hours_input(next_hours, next_minutes)
                            )
                            _set_schedule_buttons_busy(
                                schedule_target=schedule_target,
                                save=schedule_save_button,
                                skip=schedule_skip_button,
                                disable=schedule_disable_button,
                                busy=False,
                            )
                            _set_schedule_display(
                                entry,
                                status_label=schedule_status_label,
                                timezone_labels=schedule_timezone_labels,
                                client_label=schedule_client_label,
                            )
                            _notify_success(
                                "Schedule saved; near-term restart skipped."
                                if entry.skipped_through_timestamp is not None
                                else "Schedule saved."
                            )

                        async def _disable_schedule(
                            *,
                            schedule_target: RestartTarget = target,
                            schedule_status_label: Label = status_label,
                            schedule_timezone_labels: dict[str, Label] = timezone_labels,
                            schedule_client_label: Label = client_time_label,
                            schedule_save_button: Button = save_button,
                            schedule_skip_button: Button = skip_button,
                            schedule_disable_button: Button = disable_button,
                        ) -> None:
                            _set_schedule_buttons_busy(
                                schedule_target=schedule_target,
                                save=schedule_save_button,
                                skip=schedule_skip_button,
                                disable=schedule_disable_button,
                                busy=True,
                            )
                            try:
                                state = await self._remote_update_restart_schedule_async(
                                    node,
                                    schedule_target,
                                    None,
                                    None,
                                    user,
                                )
                            except Exception as xcp:
                                if actions_closed:
                                    return
                                _set_schedule_buttons_busy(
                                    schedule_target=schedule_target,
                                    save=schedule_save_button,
                                    skip=schedule_skip_button,
                                    disable=schedule_disable_button,
                                    busy=False,
                                )
                                _notify_error(f"Unable to disable schedule: {xcp}", multi_line=True)
                                return
                            if actions_closed:
                                return
                            entry = next(item for item in state.schedules if item.target is schedule_target)
                            schedules_by_target[schedule_target] = entry
                            _set_schedule_buttons_busy(
                                schedule_target=schedule_target,
                                save=schedule_save_button,
                                skip=schedule_skip_button,
                                disable=schedule_disable_button,
                                busy=False,
                            )
                            _set_schedule_display(
                                entry,
                                status_label=schedule_status_label,
                                timezone_labels=schedule_timezone_labels,
                                client_label=schedule_client_label,
                            )
                            _notify_success("Schedule disabled.")

                        async def _skip_schedule(
                            *,
                            schedule_target: RestartTarget = target,
                            schedule_status_label: Label = status_label,
                            schedule_timezone_labels: dict[str, Label] = timezone_labels,
                            schedule_client_label: Label = client_time_label,
                            schedule_save_button: Button = save_button,
                            schedule_skip_button: Button = skip_button,
                            schedule_disable_button: Button = disable_button,
                        ) -> None:
                            _set_schedule_buttons_busy(
                                schedule_target=schedule_target,
                                save=schedule_save_button,
                                skip=schedule_skip_button,
                                disable=schedule_disable_button,
                                busy=True,
                            )
                            try:
                                state = await self._remote_skip_restart_schedule_async(
                                    node,
                                    schedule_target,
                                    user,
                                )
                            except Exception as xcp:
                                if actions_closed:
                                    return
                                _set_schedule_buttons_busy(
                                    schedule_target=schedule_target,
                                    save=schedule_save_button,
                                    skip=schedule_skip_button,
                                    disable=schedule_disable_button,
                                    busy=False,
                                )
                                _notify_error(f"Unable to skip restart: {xcp}", multi_line=True)
                                return
                            if actions_closed:
                                return
                            entry = next(item for item in state.schedules if item.target is schedule_target)
                            schedules_by_target[schedule_target] = entry
                            _set_schedule_display(
                                entry,
                                status_label=schedule_status_label,
                                timezone_labels=schedule_timezone_labels,
                                client_label=schedule_client_label,
                            )
                            _set_schedule_buttons_busy(
                                schedule_target=schedule_target,
                                save=schedule_save_button,
                                skip=schedule_skip_button,
                                disable=schedule_disable_button,
                                busy=False,
                            )
                            _notify_success("Next restart skipped.")

                        save_button.on("click", _save_schedule)
                        skip_button.on("click", _skip_schedule)
                        disable_button.on("click", _disable_schedule)

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

    def _node_bot_avatar_markup(self, *, node_name: str, display_name: str, extra_class: str) -> str:
        avatar_alt = escape(f"{display_name} bot avatar", quote=True)
        avatar_uri = self._node_bot_avatar_uri(node_name=node_name)
        return (
            "<img"
            f' class="mod-user-avatar {escape(extra_class, quote=True)}" src="{escape(avatar_uri, quote=True)}"'
            f' alt="{avatar_alt}" loading="lazy" referrerpolicy="no-referrer">'
        )

    def _home_section_avatar_markup(self, *, node_name: str, display_name: str) -> str:
        return self._node_bot_avatar_markup(
            node_name=node_name,
            display_name=display_name,
            extra_class="mod-home-section-avatar",
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
        return cls._system_resource_point_badges(node_summary.system_summary)

    @classmethod
    def _system_resource_point_badges(
        cls,
        system_summary: NodeSystemSummary,
    ) -> tuple[_ModWebBadgeSpec, ...]:
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
            pending_class_name=ModWebUiHelpersMixin._badge_class_name(
                tone=cls._node_status_badge_tone(section),
                extra_classes=extra_classes,
            ),
            healthy_class_name=ModWebUiHelpersMixin._badge_class_name(tone="black", extra_classes=extra_classes),
            unhealthy_class_name=ModWebUiHelpersMixin._badge_class_name(tone="red", extra_classes=extra_classes),
            show_latency=section.error is None,
        )

    @classmethod
    def _run_home_node_latency_badges_javascript(
        cls,
        *,
        ui: ModWebUi,
        badge_specs: tuple[_ModWebNodePresenceBadgeSpec, ...],
    ) -> None:
        ModWebUiHelpersMixin._run_node_presence_badges_javascript(
            ui=ui,
            badge_specs=badge_specs,
            controller_key="modWebHomeNodeLatency",
        )

    @classmethod
    def _home_node_latency_badges_javascript(
        cls,
        badge_specs: tuple[_ModWebNodePresenceBadgeSpec, ...],
    ) -> str:
        return ModWebUiHelpersMixin._node_presence_badges_javascript(
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
                        connected_player_names=app.connected_player_names,
                        fallback_text=(
                            runtime_badge.text
                            if runtime_badge.text
                            == self._player_count_snapshot_text(
                                player_count=app.player_count,
                                player_capacity=app.player_capacity,
                            )
                            else None
                        ),
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
        player_capacity_text: str | None = _format_player_capacity(player_capacity)
        if player_capacity_text is None:
            return None
        return f"{player_count} / {player_capacity_text}"

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

    async def _refresh_runtime_model(self, *, model: ModWebBasePageModel, user: ModWebUser) -> ModWebBasePageModel:
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
        color_hex = self._resolved_app_color_hex(
            app_name=entry.name,
            scope=entry.scope,
            color_hex=entry.color_hex,
        )
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
                color_hex=color_hex,
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
                app_scope=entry.scope,
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
