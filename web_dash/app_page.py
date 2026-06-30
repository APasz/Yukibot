from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from apps._config import (
    APP_FRIENDLY_NAME_MAX_LENGTH,
    app_title_font_default_label,
    app_title_font_options,
    normalise_app_title_font,
    resolve_app_title_font,
)
from font_assets import font_assets

from .assets import extract_html_tag_contents
from .app_page_minecraft import (
    ModWebAppPageMinecraftMixin,
    _MinecraftRecipeBrowserEntry,
    _MinecraftRecipeDragPayload,
    _MinecraftRecipeEditorArea,
    _MinecraftRecipeEditorIngredientKind,
    _MinecraftRecipeEditorIngredientState,
    _MinecraftRecipeEditorOperation,
    _MinecraftRecipeEditorSelection,
    _MinecraftRecipeEditorState,
)
from .app_page_sevendays import ModWebAppPageSevenDaysMixin
from .app_page_updates import ModWebAppPageUpdateMixin
from .constants import (
    _APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS,
    _APP_RUNTIME_REFRESH_INTERVAL_SECONDS,
    _APP_SECTION_QUERY_PARAM,
    _DOWNLOAD_FEEDBACK_DELAY_SECONDS,
    _SEARCH_INPUT_DEBOUNCE_MILLISECONDS,
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
    Awaitable,
    BadgeTone,
    Button,
    Callable,
    Card,
    Checkbox,
    ClientPackPolicy,
    Html,
    Input,
    Label,
    LiteralString,
    ModWebUser,
    NodeAppActivityProviderEntry,
    NodeAppMutationAction,
    NodeAppMutationResult,
    NodeAppRuntimeSummary,
    NodeAppStateStreamEvent,
    NodeAppTransitionState,
    NodeConsoleActionList,
    NodeModEntry,
    NodeModMutationAction,
    NodeModSummary,
    NodeModUploadBatchResult,
    NodeSaveList,
    NodeSettingList,
    NodeSystemSummary,
    Power_Level,
    Select,
    Timer,
    Upload,
    assert_never,
    asyncio,
    cast,
    escape,
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
    ModWebModSortOrder,
    ModWebOverviewPageModel,
    ModWebPageLoadWarning,
    ModWebPageModel,
    _ModWebAppHeroCornerBindings,
    _ModWebAppHeroRuntimeDetails,
    _ModWebBadgeSpec,
    _ModWebChatSurfaceConfig,
    _ModWebKillControlState,
    _ModWebModToolbarBindings,
    _ModWebNodePresenceBadgeSpec,
    _ModWebRuntimeToolbarBindings,
    _ModWebStartStopControlState,
    _ModWebTabActionSpec,
)
from .ui_helpers import ModWebUiHelpersMixin

if TYPE_CHECKING:
    from nicegui.element import Element
    from nicegui.elements.dialog import Dialog
    from nicegui.elements.tabs import Tab
    from nicegui.elements.tooltip import Tooltip
    from nicegui.events import MultiUploadEventArguments


_LEAFLET_VENDOR_DIRECTORY: Path = Path(__file__).resolve().parent.parent / "resources" / "vendor" / "leaflet"

__all__ = (
    "ModWebAppPageMixin",
    "_MinecraftRecipeBrowserEntry",
    "_MinecraftRecipeDragPayload",
    "_MinecraftRecipeEditorArea",
    "_MinecraftRecipeEditorIngredientKind",
    "_MinecraftRecipeEditorIngredientState",
    "_MinecraftRecipeEditorOperation",
    "_MinecraftRecipeEditorSelection",
    "_MinecraftRecipeEditorState",
)


@lru_cache(maxsize=1)
def _leaflet_vendor_asset(file_name: str) -> str:
    return (_LEAFLET_VENDOR_DIRECTORY / file_name).read_text(encoding="utf-8")


class ModWebAppPageMixin(
    ModWebAppPageMinecraftMixin,
    ModWebAppPageSevenDaysMixin,
    ModWebAppPageUpdateMixin,
    ModWebServiceSupport,
):
    def _render_page_load_warnings(
        self,
        *,
        ui: ModWebUi,
        load_warnings: tuple[ModWebPageLoadWarning, ...],
    ) -> None:
        if not load_warnings:
            return
        with ui.column().classes("w-full gap-3"):
            for warning in load_warnings:
                with ui.card().classes("w-full border border-amber-500/40 bg-[#23160a] text-amber-100"):
                    ui.label(warning.title).classes("text-sm font-semibold uppercase tracking-[0.22em] text-amber-300")
                    ui.label(warning.detail).classes("text-sm leading-6 text-amber-50/90")

    def _apply_live_app_state_update(
        self,
        *,
        model: ModWebBasePageModel,
        event: NodeAppStateStreamEvent,
        last_system_summary: NodeSystemSummary | None,
    ) -> tuple[ModWebBasePageModel, NodeSystemSummary | None]:
        next_app_stats: NodeAppRuntimeSummary | None = self._merged_runtime_summary(
            previous=model.app_stats, updated=event.app_stats
        )
        next_system_summary: NodeSystemSummary | None = (
            event.system_summary if event.system_summary is not None else last_system_summary
        )
        next_update_info = event.update_info if (event.is_initial or event.update_changed) else model.update_info
        next_update_status = event.update_status if (event.is_initial or event.update_changed) else model.update_status
        app_start_blocked: bool = self._app_start_blocked_remote(
            app_name=model.app_name,
            app_stats=next_app_stats,
            start_blocked_app_ids=() if next_system_summary is None else next_system_summary.start_blocked_app_ids,
        )
        return (
            self._model_with_runtime_state(
                model,
                app_stats=next_app_stats,
                app_start_blocked=app_start_blocked,
                update_info=next_update_info,
                update_status=next_update_status,
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

    @classmethod
    def _ensure_mod_list_dropzone_style(cls, *, ui: ModWebUi) -> None:
        ui.add_head_html(
            """
            <style>
            .mod-mod-list-dropzone {
              position: relative;
              width: 100%;
              max-height: none !important;
              border: none !important;
              background: transparent !important;
              box-shadow: none !important;
            }
            .mod-mod-list-dropzone.q-uploader {
              width: 100% !important;
              max-height: none !important;
            }
            .mod-mod-list-dropzone .q-uploader__header {
              display: none !important;
            }
            .mod-mod-list-dropzone .q-uploader__list {
              padding: 0 !important;
              min-height: 0 !important;
              max-height: none !important;
              overflow: visible !important;
              flex: 0 0 auto !important;
            }
            .mod-mod-list-dropzone .mod-mod-list-drop-shell {
              position: relative;
              min-height: 0;
            }
            .mod-mod-list-dropzone .mod-mod-list-drop-overlay {
              position: absolute;
              inset: 0;
              display: flex;
              align-items: center;
              justify-content: center;
              border-radius: 1.25rem;
              background: rgba(15, 23, 42, 0.78);
              border: 2px dashed rgba(255, 255, 255, 0.32);
              color: #ffffff;
              font-weight: 700;
              letter-spacing: 0.08em;
              text-transform: uppercase;
              opacity: 0;
              pointer-events: none;
              transition: opacity 120ms ease;
              z-index: 10;
            }
            .mod-mod-list-dropzone.q-uploader--dnd .mod-mod-list-drop-overlay {
              opacity: 1;
            }
            </style>
            """
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
        initial_system_summary: NodeSystemSummary | None = None,
        chat_surface: _ModWebChatSurfaceConfig | None = None,
    ) -> None:
        if chat_surface is not None and not model.supports_chat:
            raise ValueError("App page received chat configuration for an app without chat support.")
        self._apply_theme(ui=ui)
        current_model: ModWebBasePageModel = model
        last_system_summary: NodeSystemSummary | None = initial_system_summary
        with ui.column().classes(self._app_page_classes()):
            self._render_user_header(ui=ui, user=user)
            hero_card: Card = ui.card().classes(self._app_hero_card_classes(model.app_stats)).style(
                self._hero_card_style(model.app_color_hex)
            )
            hero_runtime_refresh = None if subscribe_app_state_updates is not None else refresh_async_app_stats
            with hero_card:
                hero_corner_bindings = self._render_app_hero_corner_badges(
                    ui=ui,
                    node_name=model.node_name,
                    top_badges=self._app_page_hero_badges(model),
                    initial_system_summary=initial_system_summary,
                    initial_app_stats=model.app_stats,
                )
                with ui.column().classes(self._app_page_hero_shell_classes()):
                    apply_app_hero_runtime: Callable[[NodeAppRuntimeSummary | None], None] = (
                        self._render_live_app_hero_runtime(
                            ui=ui,
                            hero_card=hero_card,
                            app_name=model.app_name,
                            title=model.app_friendly,
                            title_font_preset=model.app_title_font_preset,
                            join_address=model.join_address,
                            join_direct_ip_address=model.join_direct_ip_address,
                            activity_providers=self._enabled_app_activity_providers(model),
                            initial_app_stats=model.app_stats,
                            refresh_async_app_stats=hero_runtime_refresh,
                        )
                    )
                    toolbar_bindings: _ModWebRuntimeToolbarBindings = self._render_global_app_toolbar(
                        ui=ui,
                        model=model,
                        user=user,
                        refresh_async_runtime_model=refresh_async_runtime_model,
                        poll_runtime_model=subscribe_app_state_updates is None,
                    )
            self._render_page_load_warnings(
                ui=ui,
                load_warnings=getattr(model, "load_warnings", ()),
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
                    refresh_async_runtime_model=refresh_async_runtime_model,
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
                        last_system_summary=last_system_summary,
                    )
                    hero_corner_bindings.apply_node_summary(last_system_summary)
                    hero_corner_bindings.apply_app_stats(current_model.app_stats)
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
        initial_system_summary: NodeSystemSummary | None = None,
        chat_surface: _ModWebChatSurfaceConfig | None = None,
    ) -> None:
        if chat_surface is not None and not model.supports_chat:
            raise ValueError("Overview page received chat configuration for an app without chat support.")
        self._apply_theme(ui=ui)
        current_model: ModWebBasePageModel = model
        last_system_summary: NodeSystemSummary | None = initial_system_summary
        with ui.column().classes(self._app_page_classes()):
            self._render_user_header(ui=ui, user=user)
            hero_card: Card = ui.card().classes(self._app_hero_card_classes(model.app_stats)).style(
                self._hero_card_style(model.app_color_hex)
            )
            hero_runtime_refresh = None if subscribe_app_state_updates is not None else refresh_async_app_stats
            with hero_card:
                hero_corner_bindings = self._render_app_hero_corner_badges(
                    ui=ui,
                    node_name=model.node_name,
                    top_badges=self._app_page_hero_badges(model),
                    initial_system_summary=initial_system_summary,
                    initial_app_stats=model.app_stats,
                )
                with ui.column().classes(self._app_page_hero_shell_classes()):
                    apply_app_hero_runtime: Callable[[NodeAppRuntimeSummary | None], None] = (
                        self._render_live_app_hero_runtime(
                            ui=ui,
                            hero_card=hero_card,
                            app_name=model.app_name,
                            title=model.app_friendly,
                            title_font_preset=model.app_title_font_preset,
                            join_address=model.join_address,
                            join_direct_ip_address=model.join_direct_ip_address,
                            activity_providers=self._enabled_app_activity_providers(model),
                            initial_app_stats=model.app_stats,
                            refresh_async_app_stats=hero_runtime_refresh,
                        )
                    )
                    toolbar_bindings: _ModWebRuntimeToolbarBindings = self._render_global_app_toolbar(
                        ui=ui,
                        model=model,
                        user=user,
                        refresh_async_runtime_model=refresh_async_runtime_model,
                        poll_runtime_model=subscribe_app_state_updates is None,
                    )
            self._render_page_load_warnings(
                ui=ui,
                load_warnings=getattr(model, "load_warnings", ()),
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
                        refresh_async_runtime_model=refresh_async_runtime_model,
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
                        last_system_summary=last_system_summary,
                    )
                    hero_corner_bindings.apply_node_summary(last_system_summary)
                    hero_corner_bindings.apply_app_stats(current_model.app_stats)
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
            return _ModWebAppHeroRuntimeDetails(
                status_text="Unknown",
                status_tone="grey",
                relay_badge=relay_badge,
                version_badge=version_badge,
            )

        if app_stats.transition_state is NodeAppTransitionState.STOPPING:
            status_text = "Stopping"
            status_tone = "warn"
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

        relay_badge = _ModWebBadgeSpec(text=f"{app_stats.relay_support.display_value}", tone="grey")
        version_badge = _ModWebBadgeSpec(text=f"{app_stats.version or 'Unknown'}", tone="black")
        player_count_badge: _ModWebBadgeSpec | None = None
        if app_stats.player_count is not None and app_stats.player_capacity is not None:
            player_tone = "purple" if app_stats.player_count > 0 else "grey"
            player_count_badge = _ModWebBadgeSpec(
                text=f"{app_stats.player_count} / {app_stats.player_capacity}",
                tone=player_tone,
            )
        return _ModWebAppHeroRuntimeDetails(
            status_text=status_text,
            status_tone=status_tone,
            relay_badge=relay_badge,
            version_badge=version_badge,
            player_count_badge=player_count_badge,
        )

    def _app_page_hero_badges(self, model: ModWebBasePageModel) -> tuple[_ModWebBadgeSpec, ...]:
        return self._app_resource_point_badges(model)

    @staticmethod
    def _enabled_app_activity_providers(model: ModWebBasePageModel) -> tuple[NodeAppActivityProviderEntry, ...]:
        return tuple(provider for provider in model.activity_providers if provider.enabled)

    @staticmethod
    def _visible_app_activity_providers(
        *,
        app_stats: NodeAppRuntimeSummary | None,
        activity_providers: tuple[NodeAppActivityProviderEntry, ...],
    ) -> tuple[NodeAppActivityProviderEntry, ...]:
        if app_stats is None or not app_stats.running:
            return ()
        runtime_providers_by_id: dict[str, NodeAppActivityProviderEntry] = {
            provider.provider_id.casefold(): provider
            for provider in app_stats.activity_providers
            if provider.enabled and provider.current_value is not None
        }
        visible_providers: list[NodeAppActivityProviderEntry] = []
        for provider in activity_providers:
            runtime_provider = runtime_providers_by_id.get(provider.provider_id.casefold())
            if runtime_provider is None or runtime_provider.current_value is None:
                continue
            visible_providers.append(runtime_provider)
        return tuple(visible_providers)

    @classmethod
    def _visible_app_activity_provider_badges(
        cls,
        *,
        app_stats: NodeAppRuntimeSummary | None,
        activity_providers: tuple[NodeAppActivityProviderEntry, ...],
    ) -> tuple[str, ...]:
        return tuple(
            cls._app_activity_provider_badge_markup(
                provider_id=provider.provider_id,
                label=provider.label,
                current_value=provider.current_value,
            )
            for provider in cls._visible_app_activity_providers(
                app_stats=app_stats,
                activity_providers=activity_providers,
            )
            if provider.current_value is not None
        )

    def _app_activity_provider_tooltip_html(
        self,
        *,
        provider: NodeAppActivityProviderEntry,
        connected_player_names: tuple[str, ...] = (),
    ) -> str:
        current_value: str = provider.current_value or "Unavailable"
        lines: list[str] = [provider.label]
        provider_id: str = provider.provider_id.casefold()
        if provider_id == "day" and current_value.startswith("D") and current_value[1:].isdigit():
            lines.append(f"Current day: {int(current_value[1:])}")
        elif provider_id == "stage":
            tier_text, separator, schematic_name = current_value.partition(":")
            if tier_text.startswith("T") and tier_text[1:].isdigit():
                lines.append(f"Current tier: {int(tier_text[1:])}")
                if separator and schematic_name.strip():
                    lines.append(f"Schematic: {schematic_name.strip()}")
            else:
                lines.append(f"Current value: {current_value}")
        elif provider_id == "time":
            is_blood_moon: bool = current_value.startswith("!")
            raw_value: str = current_value[1:] if is_blood_moon else current_value
            if raw_value.startswith("D") and "/H" in raw_value:
                day_text, hour_text = raw_value[1:].split("/H", 1)
                if day_text.isdigit() and hour_text.isdigit():
                    lines.extend((f"Day: {int(day_text)}", f"Hour: {int(hour_text)}"))
                    if is_blood_moon:
                        lines.append("Blood moon active")
                else:
                    lines.append(f"Current value: {current_value}")
            else:
                lines.append(f"Current value: {current_value}")
        else:
            lines.append(f"Current value: {current_value}")
        if provider_id in {"player", "players"}:
            lines.append("Connected players:" if connected_player_names else "No players connected")
            lines.extend(connected_player_names)
        return self._tooltip_lines_html(tuple(lines)) or provider.label

    @staticmethod
    def _app_activity_provider_badge_markup(*, provider_id: str, label: str, current_value: str) -> str:
        if provider_id.casefold() == "day" and current_value.startswith("D") and current_value[1:].isdigit():
            return escape(f"Day {int(current_value[1:])}")
        if provider_id.casefold() == "stage":
            tier_prefix = current_value
            schematic_name = ""
            if ":" in current_value:
                tier_prefix, schematic_name = (part.strip() for part in current_value.split(":", 1))
            if tier_prefix.startswith("T") and tier_prefix[1:].isdigit():
                tier_text = f"Tier {int(tier_prefix[1:])}"
                if schematic_name:
                    return escape(f"{tier_text}: {schematic_name}")
                return escape(tier_text)
        if provider_id.casefold() == "time":
            is_blood_moon = current_value.startswith("!")
            raw_value = current_value[1:] if is_blood_moon else current_value
            if raw_value.startswith("D") and "/H" in raw_value:
                day_text, hour_text = raw_value[1:].split("/H", 1)
                if day_text.isdigit() and hour_text.isdigit():
                    day_markup = (
                        f'<span class="mod-app-activity-alert">{escape(str(int(day_text)))}</span>'
                        if is_blood_moon
                        else escape(str(int(day_text)))
                    )
                    return f"Day {day_markup} Hour {escape(str(int(hour_text)))}"
        return escape(f"{label}: {current_value}")

    @classmethod
    def _app_resource_point_badges(cls, model: ModWebBasePageModel) -> tuple[_ModWebBadgeSpec, ...]:
        resource_points = model.resource_points
        if resource_points is None:
            return ()
        return (
            _ModWebBadgeSpec(
                text=cls._app_resource_point_badge_text(
                    running_points=resource_points.cpu_points_running,
                    startup_points=resource_points.cpu_points_startup,
                ),
                tone="black",
                icon="speed",
                tooltip_text=cls._app_resource_point_badge_tooltip(
                    resource_name="CPU",
                    running_points=resource_points.cpu_points_running,
                    startup_points=resource_points.cpu_points_startup,
                ),
            ),
            _ModWebBadgeSpec(
                text=cls._app_resource_point_badge_text(
                    running_points=resource_points.ram_points_running,
                    startup_points=resource_points.ram_points_startup,
                ),
                tone="black",
                icon="memory",
                tooltip_text=cls._app_resource_point_badge_tooltip(
                    resource_name="RAM",
                    running_points=resource_points.ram_points_running,
                    startup_points=resource_points.ram_points_startup,
                ),
            ),
        )

    @staticmethod
    def _app_resource_point_badge_tooltip(
        *,
        resource_name: str,
        running_points: int,
        startup_points: int,
    ) -> str:
        if startup_points == running_points:
            return f"{resource_name} resource points: {running_points}"
        return f"{resource_name} resource points — running: {running_points}; startup: {startup_points}"

    @staticmethod
    def _app_resource_point_badge_text(*, running_points: int, startup_points: int) -> str:
        if startup_points == running_points:
            return str(running_points)
        return f"{running_points} ({startup_points})"

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

    def _render_app_hero_corner_badges(
        self,
        *,
        ui: ModWebUi,
        node_name: str,
        top_badges: tuple[_ModWebBadgeSpec, ...],
        initial_system_summary: NodeSystemSummary | None,
        initial_app_stats: NodeAppRuntimeSummary | None,
    ) -> _ModWebAppHeroCornerBindings:
        initial_runtime_details = self._app_hero_runtime_details(initial_app_stats)
        presence_stream_url = self._app_page_node_presence_stream_url(node_name=node_name)
        initial_node_badge_tone: BadgeTone = (
            "black"
            if presence_stream_url is not None
            else self._app_page_node_badge_tone(
                node_name=node_name,
                system_summary=initial_system_summary,
            )
        )
        with ui.element("div").classes("mod-app-node-badge-wrap"):
            with ui.row().classes("mod-app-node-badge-row"):
                node_badge = self._badge(
                    ui=ui,
                    text=node_name,
                    tone=initial_node_badge_tone,
                    extra_classes="mod-app-corner-badge mod-app-node-badge",
                    tooltip_text=f"Node: {node_name}",
                )
                if color_hex := self._node_role_color_hex(node_name=node_name):
                    node_badge.style(self._node_badge_style(color_hex))
                relay_badge = self._badge(
                    ui=ui,
                    text=initial_runtime_details.relay_badge.text,
                    tone=initial_runtime_details.relay_badge.tone,
                    extra_classes="mod-app-corner-badge",
                    tooltip_text=f"Chat relay support: {initial_runtime_details.relay_badge.text}",
                )
                version_badge = self._badge(
                    ui=ui,
                    text=initial_runtime_details.version_badge.text,
                    tone=initial_runtime_details.version_badge.tone,
                    extra_classes="mod-app-corner-badge",
                    tooltip_text=f"Application version: {initial_runtime_details.version_badge.text}",
                )
                for badge in top_badges:
                    self._badge_spec(ui=ui, badge=badge, extra_classes="mod-app-corner-badge")
        node_badge_spec = self._app_page_node_presence_badge_spec(
            node_name=node_name,
            badge_element=node_badge,
            presence_stream_url=presence_stream_url,
        )
        if node_badge_spec is not None:
            self._run_node_presence_badges_javascript(
                ui=ui,
                badge_specs=(node_badge_spec,),
                controller_key="modWebAppHeroNodePresence",
            )

        def _apply_node_summary(system_summary: NodeSystemSummary | None) -> None:
            if node_badge_spec is not None:
                return
            self._set_badge_state(
                node_badge,
                node_name,
                self._app_page_node_badge_tone(
                    node_name=node_name,
                    system_summary=system_summary,
                ),
                extra_classes="mod-app-corner-badge mod-app-node-badge",
            )

        def _apply_app_stats(app_stats: NodeAppRuntimeSummary | None) -> None:
            runtime_details = self._app_hero_runtime_details(app_stats)
            self._set_badge_state(
                relay_badge,
                runtime_details.relay_badge.text,
                runtime_details.relay_badge.tone,
                extra_classes="mod-app-corner-badge",
            )
            self._set_badge_state(
                version_badge,
                runtime_details.version_badge.text,
                runtime_details.version_badge.tone,
                extra_classes="mod-app-corner-badge",
            )

        return _ModWebAppHeroCornerBindings(
            apply_node_summary=_apply_node_summary,
            apply_app_stats=_apply_app_stats,
        )

    @staticmethod
    def _app_page_node_badge_tone(
        *,
        node_name: str,
        system_summary: NodeSystemSummary | None,
    ) -> BadgeTone:
        del node_name
        if system_summary is not None:
            return "black"
        return "red"

    def _app_page_node_presence_stream_url(self, *, node_name: str) -> str | None:
        return self._remote_node_link(node_name).presence_stream_url

    @classmethod
    def _app_page_node_presence_badge_spec(
        cls,
        *,
        node_name: str,
        badge_element: Label,
        presence_stream_url: str | None,
    ) -> _ModWebNodePresenceBadgeSpec | None:
        if presence_stream_url is None:
            return None
        badge_element_id = getattr(badge_element, "id", None)
        if not isinstance(badge_element_id, int):
            return None
        extra_classes = "mod-app-corner-badge mod-app-node-badge"
        healthy_class_name = ModWebUiHelpersMixin._badge_class_name(tone="black", extra_classes=extra_classes)
        return _ModWebNodePresenceBadgeSpec(
            node_name=node_name,
            badge_element_id=badge_element_id,
            text_element_id=None,
            node_label=node_name,
            pending_text=node_name,
            alive_text=node_name,
            down_text=node_name,
            presence_stream_url=presence_stream_url,
            pending_class_name=healthy_class_name,
            healthy_class_name=healthy_class_name,
            unhealthy_class_name=ModWebUiHelpersMixin._badge_class_name(tone="red", extra_classes=extra_classes),
            show_latency=False,
        )

    @staticmethod
    def _app_scope_from_name(app_name: str) -> str | None:
        scope, separator, _instance_key = app_name.partition("_")
        if not separator or not scope.strip():
            return None
        return scope

    @classmethod
    def _app_title_font_style(
        cls,
        *,
        app_name: str,
        title_font_preset: str,
    ) -> str | None:
        resolved_font = resolve_app_title_font(
            value=title_font_preset,
            scope=cls._app_scope_from_name(app_name),
        )
        if resolved_font.css_font_family is None:
            return None
        if resolved_font.is_builtin:
            return f"font-family: {resolved_font.css_font_family} !important;"
        return (
            f"font-family: {resolved_font.css_font_family} !important;"
            "font-weight: 400 !important;"
            "font-style: normal !important;"
        )

    @classmethod
    def _app_title_font_options(cls, *, app_name: str, selected_value: str | None = None) -> dict[str, str]:
        return app_title_font_options(
            custom_font_families=font_assets.available_font_families(scope=cls._app_scope_from_name(app_name)),
            selected_value=selected_value,
        )

    @classmethod
    def _app_title_font_default_label(cls, *, app_name: str) -> str:
        return app_title_font_default_label(scope=cls._app_scope_from_name(app_name))

    def _render_live_app_hero_runtime(
        self,
        *,
        ui: ModWebUi,
        hero_card: Card,
        app_name: str,
        title: str,
        title_font_preset: str,
        activity_providers: tuple[NodeAppActivityProviderEntry, ...],
        initial_app_stats: NodeAppRuntimeSummary | None,
        join_address: str | None = None,
        join_direct_ip_address: str | None = None,
        refresh_async_app_stats: Callable[[], Awaitable[NodeAppRuntimeSummary | None]] | None = None,
    ) -> Callable[[NodeAppRuntimeSummary | None], None]:
        initial_runtime_details = self._app_hero_runtime_details(initial_app_stats)
        with ui.column().classes("w-full gap-3"):
            with ui.row().classes("w-full items-start justify-between gap-3 flex-wrap"):
                with ui.column().classes(self._hero_header_main_classes()):
                    title_label = ui.label(title).classes(self._hero_title_classes())
                    title_style = self._app_title_font_style(
                        app_name=app_name,
                        title_font_preset=title_font_preset,
                    )
                    if title_style is not None:
                        title_label.style(title_style)
                with ui.column().classes("mod-app-hero-status gap-1"):
                    status_value_label = ui.label(initial_runtime_details.status_text).classes(
                        f"mod-app-hero-status-value mod-app-hero-status-value-{initial_runtime_details.status_tone}"
                    )
                    if join_address is not None:
                        with ui.column().classes("mod-app-hero-join-addresses gap-0"):
                            ui.label(join_address).classes("mod-app-hero-join-address")
                            if join_direct_ip_address is not None:
                                ui.label(join_direct_ip_address).classes("mod-app-hero-join-address-direct")
            runtime_badge_row = ui.row().classes(f"{self._hero_badge_row_classes(fill=True)} w-full")
            with runtime_badge_row:
                player_badge = ui.label(
                    initial_runtime_details.player_count_badge.text
                    if initial_runtime_details.player_count_badge is not None
                    else ""
                ).classes(
                    self._badge_class_name(
                        tone=initial_runtime_details.player_count_badge.tone
                        if initial_runtime_details.player_count_badge is not None
                        else "grey"
                    )
                )
                player_badge_tooltip, player_badge_tooltip_content = self._attach_html_tooltip(
                    ui=ui,
                    target=player_badge,
                    html=(
                        self._player_count_tooltip_html(
                            connected_player_names=initial_app_stats.connected_player_names
                            if initial_app_stats is not None
                            else (),
                            fallback_text=(
                                initial_runtime_details.player_count_badge.text
                                if initial_runtime_details.player_count_badge is not None
                                else None
                            ),
                        )
                        or ""
                    ),
                )
                initial_activity_badges = self._visible_app_activity_provider_badges(
                    app_stats=initial_app_stats,
                    activity_providers=activity_providers,
                )
                initial_visible_activity_providers = self._visible_app_activity_providers(
                    app_stats=initial_app_stats,
                    activity_providers=activity_providers,
                )
                activity_badge_labels: tuple[Html, ...] = tuple(
                    cast(
                        Html,
                        ui.html(initial_activity_badges[index] if index < len(initial_activity_badges) else "").classes(
                            self._badge_class_name(tone="black")
                        ),
                    )
                    for index, _provider in enumerate(activity_providers)
                )
                activity_badge_tooltips = tuple(
                    self._attach_html_tooltip(
                        ui=ui,
                        target=activity_badge_label,
                        html=(
                            self._app_activity_provider_tooltip_html(
                                provider=initial_visible_activity_providers[index],
                                connected_player_names=(
                                    initial_app_stats.connected_player_names
                                    if initial_app_stats is not None
                                    else ()
                                ),
                            )
                            if index < len(initial_visible_activity_providers)
                            else ""
                        ),
                    )
                    for index, activity_badge_label in enumerate(activity_badge_labels)
                )

        def _apply_runtime(app_stats: NodeAppRuntimeSummary | None) -> None:
            hero_card.classes(replace=self._app_hero_card_classes(app_stats))
            runtime_details = self._app_hero_runtime_details(app_stats)
            status_value_label.set_text(runtime_details.status_text)
            status_value_label.classes(
                replace=f"mod-app-hero-status-value mod-app-hero-status-value-{runtime_details.status_tone}"
            )
            self._set_optional_badge_state(player_badge, runtime_details.player_count_badge)
            self._set_html_tooltip_state(
                player_badge_tooltip,
                player_badge_tooltip_content,
                self._player_count_tooltip_html(
                    connected_player_names=app_stats.connected_player_names if app_stats is not None else (),
                    fallback_text=(
                        runtime_details.player_count_badge.text
                        if runtime_details.player_count_badge is not None
                        else None
                    ),
                )
                or "",
            )
            visible_activity_badges = self._visible_app_activity_provider_badges(
                app_stats=app_stats,
                activity_providers=activity_providers,
            )
            visible_activity_providers = self._visible_app_activity_providers(
                app_stats=app_stats,
                activity_providers=activity_providers,
            )
            for index, (activity_badge_label, activity_badge, activity_provider) in enumerate(
                zip(
                    activity_badge_labels,
                    visible_activity_badges,
                    visible_activity_providers,
                    strict=False,
                )
            ):
                activity_badge_label.set_content(activity_badge)
                activity_badge_label.update()
                activity_tooltip, activity_tooltip_content = activity_badge_tooltips[index]
                self._set_html_tooltip_state(
                    activity_tooltip,
                    activity_tooltip_content,
                    self._app_activity_provider_tooltip_html(
                        provider=activity_provider,
                        connected_player_names=app_stats.connected_player_names if app_stats is not None else (),
                    ),
                )
                self._set_element_visibility(activity_badge_label, visible=True)
            for index, activity_badge_label in enumerate(
                activity_badge_labels[len(visible_activity_badges) :],
                start=len(visible_activity_badges),
            ):
                activity_tooltip, activity_tooltip_content = activity_badge_tooltips[index]
                self._set_html_tooltip_state(activity_tooltip, activity_tooltip_content, "")
                self._set_element_visibility(activity_badge_label, visible=False)
            self._set_element_visibility(
                runtime_badge_row,
                visible=runtime_details.player_count_badge is not None or bool(visible_activity_badges),
            )

        _apply_runtime(initial_app_stats)
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

    @staticmethod
    def _set_element_visibility(element: Label | Element, *, visible: bool) -> None:
        if visible:
            element.style(remove="display: none;")
            return
        element.style(add="display: none;")

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
            case ModWebAppSectionKind.UPDATE:
                return self._update_section_badges(model=model)
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
        del tab
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
        del tab
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
        canvas_frame_id = f"{container_id}-canvas-frame"
        world_id = f"{container_id}-world"
        color_id = f"{container_id}-color"
        snap_id = f"{container_id}-snap"
        status_id = f"{container_id}-status"
        notice_id = f"{container_id}-notice"
        mode_id = f"{container_id}-mode"
        prompt_id = f"{container_id}-prompt"
        prompt_title_id = f"{container_id}-prompt-title"
        prompt_form_id = f"{container_id}-prompt-form"
        prompt_input_id = f"{container_id}-prompt-input"
        prompt_submit_id = f"{container_id}-prompt-submit"
        refresh_id = f"{container_id}-refresh"
        marker_id = f"{container_id}-marker"
        line_id = f"{container_id}-line"
        read_only_note = (
            ""
            if model.can_write_map_annotations
            else (
                '<div class="mod-card mod-card-plain mod-map-note">'
                '<div class="mod-map-readonly mod-subtitle">Sign in with a `User` level account to add shared annotations.</div>'
                "</div>"
            )
        )
        write_toolbar_controls = (
            ""
            if not model.can_write_map_annotations
            else (
                f'<div class="mod-map-toolbar-group mod-map-toolbar-group-dimension">'
                f'<select id="{world_id}" class="mod-map-select" aria-label="Dimension"></select>'
                f'<div class="mod-map-toolbar-pair mod-map-toolbar-pair-dimension">'
                f'<input id="{color_id}" class="mod-map-color" type="color" value="#22C55E" aria-label="Annotation color">'
                f'<label class="mod-map-toggle" for="{snap_id}"><input id="{snap_id}" type="checkbox" checked>45°</label>'
                f"</div>"
                f"</div>"
                f'<div class="mod-map-toolbar-group mod-map-toolbar-group-tools">'
                f'<div class="mod-map-toolbar-pair mod-map-toolbar-pair-tools">'
                f'<button id="{marker_id}" type="button" class="mod-list-button secondary mod-toolbar-button mod-map-button">Point</button>'
                f'<button id="{line_id}" type="button" class="mod-list-button secondary mod-toolbar-button mod-map-button">Line</button>'
                f"</div>"
                f'<button id="{refresh_id}" type="button" class="mod-list-button secondary mod-toolbar-button mod-map-button">Refresh</button>'
                f"</div>"
            )
        )
        map_html = ui.html(
            f"""
            <div id="{container_id}" class="mod-map-shell">
              <div class="mod-card mod-card-plain mod-tab-toolbar mod-tab-toolbar-surface mod-map-toolbar">
                <div class="mod-map-toolbar-main">
                  <div id="{mode_id}" class="mod-map-mode mod-subtitle">Loading map…</div>
                  <div id="{status_id}" class="mod-map-status mod-subtitle">Loading map data…</div>
                </div>
                <div class="mod-tab-toolbar-actions mod-map-toolbar-actions">
                  {write_toolbar_controls}
                  {"" if model.can_write_map_annotations else f'<select id="{world_id}" class="mod-map-select" aria-label="Dimension"></select><button id="{refresh_id}" type="button" class="mod-list-button secondary mod-toolbar-button mod-map-button">Refresh</button>'}
                </div>
              </div>
              {read_only_note}
              <div class="mod-card mod-card-plain mod-map-status-panel">
                <div id="{notice_id}" class="mod-map-notice mod-subtitle">Connecting to Squaremap…</div>
              </div>
              <div class="mod-card mod-card-plain mod-map-stage">
                <div id="{canvas_frame_id}" class="mod-map-canvas-frame">
                  <div id="{canvas_id}" class="mod-map-canvas"></div>
                  <div id="{prompt_id}" class="mod-map-label-prompt" hidden>
                    <div id="{prompt_title_id}" class="mod-map-label-prompt-title">Label annotation</div>
                    <form id="{prompt_form_id}" class="mod-map-label-prompt-form">
                      <input
                        id="{prompt_input_id}"
                        class="mod-map-label-input"
                        type="text"
                        maxlength="80"
                        autocomplete="off"
                      spellcheck="false"
                      aria-label="Annotation label">
                      <div class="mod-map-label-prompt-actions">
                        <button id="{prompt_submit_id}" type="submit" class="mod-list-button mod-toolbar-button mod-map-button">Save</button>
                      </div>
                    </form>
                  </div>
                </div>
              </div>
            </div>
            """
        )
        map_html.classes("w-full")
        ui.run_javascript(
            self._map_client_bootstrap_script(
                config_payload={
                    "containerId": container_id,
                    "canvasId": canvas_id,
                    "canvasFrameId": canvas_frame_id,
                    "worldSelectId": world_id,
                    "colorInputId": color_id,
                    "snapToggleId": snap_id,
                    "statusId": status_id,
                    "noticeId": notice_id,
                    "modeId": mode_id,
                    "promptId": prompt_id,
                    "promptTitleId": prompt_title_id,
                    "promptFormId": prompt_form_id,
                    "promptInputId": prompt_input_id,
                    "promptSubmitId": prompt_submit_id,
                    "refreshButtonId": refresh_id,
                    "markerButtonId": marker_id,
                    "lineButtonId": line_id,
                    "appName": model.app_name,
                    "nodeName": model.node_name,
                    "mapApiUrl": model.map_api_url,
                    "clientErrorUrl": "/mod-web/client-errors/map",
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
        version = ModWebAppPageMixin._map_client_asset_version()
        ui.add_head_html(
            f'<link rel="stylesheet" href="/mod-web/assets/map.css?v={version}">'
            f'<script src="/mod-web/assets/map.js?v={version}"></script>'
        )

    @staticmethod
    @lru_cache(maxsize=1)
    def _map_client_stylesheet() -> str:
        return extract_html_tag_contents(ModWebAppPageMixin._map_client_assets_html(), tag_name="style")

    @staticmethod
    @lru_cache(maxsize=1)
    def _map_client_script() -> str:
        return extract_html_tag_contents(ModWebAppPageMixin._map_client_assets_html(), tag_name="script")

    @staticmethod
    @lru_cache(maxsize=1)
    def _map_client_asset_version() -> str:
        content = (
            ModWebAppPageMixin._map_client_stylesheet()
            + ModWebAppPageMixin._map_client_script()
        ).encode("utf-8")
        return hashlib.sha256(content).hexdigest()[:12]

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
                width: 100%;
                gap: 0.55rem;
              }
              .mod-map-status-panel {
                display: flex;
                flex-wrap: wrap;
                align-items: center;
                justify-content: space-between;
                gap: 0.75rem;
              }
              .mod-map-status-panel,
              .mod-map-note,
              .mod-map-stage {
                width: 100%;
                box-sizing: border-box;
                padding: 1rem 1.1rem;
              }
              .mod-map-toolbar {
                display: flex;
                flex-direction: column;
                align-items: stretch;
                width: 100%;
                box-sizing: border-box;
                gap: 0.65rem;
              }
              .mod-map-toolbar-main,
              .mod-map-toolbar-group,
              .mod-map-toolbar-pair,
              .mod-map-toolbar-actions {
                display: flex;
                align-items: center;
                gap: 0.55rem;
              }
              .mod-map-toolbar-main {
                width: 100%;
                min-width: 0;
                flex-wrap: nowrap;
                justify-content: space-between;
              }
              .mod-map-toolbar-actions {
                width: 100%;
                flex-wrap: wrap;
                align-items: center;
                gap: 0.65rem;
              }
              .mod-map-toolbar-group {
                min-width: 0;
              }
              .mod-map-toolbar-group-dimension {
                flex: 1 1 18rem;
                flex-wrap: nowrap;
              }
              .mod-map-toolbar-group-tools {
                flex: 1 1 16rem;
                flex-wrap: nowrap;
                justify-content: flex-end;
              }
              .mod-map-toolbar-pair-dimension {
                flex: 0 0 auto;
                flex-wrap: nowrap;
              }
              .mod-map-toolbar-pair-tools {
                flex: 1 1 auto;
                min-width: 0;
                flex-wrap: nowrap;
              }
              .mod-map-status-panel {
                align-items: flex-start;
              }
              .mod-map-label-prompt[hidden] {
                display: none;
              }
              .mod-map-canvas-frame {
                position: relative;
                width: 100%;
                max-width: min(100%, 72rem);
              }
              .mod-map-label-prompt {
                position: absolute;
                z-index: 700;
                display: flex;
                flex-direction: column;
                gap: 0.55rem;
                width: min(19rem, calc(100% - 1rem));
                padding: 0.8rem 0.85rem;
                border: 1px solid rgba(139, 92, 246, 0.42);
                border-radius: 0;
                background: rgba(0, 0, 0, 0.96);
                box-shadow:
                  0 18px 42px rgba(3, 7, 18, 0.34),
                  inset 0 1px 0 rgba(255, 255, 255, 0.05),
                  inset 0 -1px 0 rgba(139, 92, 246, 0.16);
                transform: translate(-50%, calc(-100% - 0.85rem));
              }
              .mod-map-label-prompt-title {
                color: #ddd6fe;
                font-size: 0.85rem;
                font-weight: 700;
                letter-spacing: 0.02em;
                text-transform: uppercase;
              }
              .mod-map-label-prompt-form {
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 0.55rem;
              }
              .mod-map-label-input {
                width: 100%;
                min-height: 2.5rem;
                padding: 0 0.9rem;
                border: 1px solid rgba(139, 92, 246, 0.42);
                border-radius: 0;
                background:
                  linear-gradient(180deg, rgba(196, 181, 253, 0.08), rgba(196, 181, 253, 0)),
                  rgba(36, 17, 58, 0.72);
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.04),
                  inset 0 -1px 0 rgba(139, 92, 246, 0.24);
                color: #f5f3ff;
              }
              .mod-map-label-input::placeholder {
                color: rgba(237, 233, 254, 0.62);
              }
              .mod-map-label-input:focus {
                outline: none;
                border-color: rgba(196, 181, 253, 0.9);
                box-shadow:
                  0 0 0 1px rgba(196, 181, 253, 0.35),
                  inset 0 1px 0 rgba(255, 255, 255, 0.04),
                  inset 0 -1px 0 rgba(139, 92, 246, 0.24);
              }
              .mod-map-label-prompt-actions {
                display: flex;
                width: 100%;
                align-items: center;
                gap: 0.55rem;
              }
              .mod-map-label-prompt-actions .mod-map-button {
                flex: 1 1 100%;
                width: 100%;
              }
              .mod-map-stage {
                display: flex;
                justify-content: center;
                padding: 0.7rem;
                border: 1px solid rgba(139, 92, 246, 0.24);
                background:
                  linear-gradient(180deg, rgba(76, 29, 149, 0.14), rgba(24, 24, 27, 0)),
                  rgba(12, 10, 18, 0.62);
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.03),
                  inset 0 -1px 0 rgba(139, 92, 246, 0.1);
              }
              .mod-map-select,
              .mod-map-color,
              .mod-map-toggle {
                border: 1px solid rgba(139, 92, 246, 0.42);
                border-radius: 0;
                background:
                  linear-gradient(180deg, rgba(196, 181, 253, 0.08), rgba(196, 181, 253, 0)),
                  rgba(36, 17, 58, 0.72);
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.04),
                  inset 0 -1px 0 rgba(139, 92, 246, 0.24);
                color: #f5f3ff;
                min-height: 2.5rem;
              }
              .mod-map-select {
                flex: 1 1 12rem;
                min-width: 10rem;
                padding: 0 0.75rem;
                appearance: none;
              }
              .mod-map-select option {
                background: #1f1630;
                color: #f5f3ff;
              }
              .mod-map-color {
                width: 2.5rem;
                min-width: 2.5rem;
                padding: 0.2rem;
              }
              .mod-map-color::-webkit-color-swatch-wrapper {
                padding: 0;
              }
              .mod-map-color::-webkit-color-swatch,
              .mod-map-color::-moz-color-swatch {
                border: none;
                border-radius: 0;
              }
              .mod-map-button {
                min-height: 2.5rem;
                padding: 0 0.85rem;
              }
              .mod-map-toolbar-group-tools .mod-map-button {
                flex: 1 1 0;
                min-width: 0;
              }
              .mod-map-toolbar .mod-list-button.mod-toolbar-button.mod-map-button.mod-map-button-active {
                border-color: rgba(168, 85, 247, 0.9) !important;
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.06),
                  inset 0 -1px 0 rgba(216, 180, 254, 0.28),
                  0 0 0 1px rgba(168, 85, 247, 0.18) !important;
                background:
                  linear-gradient(180deg, rgba(168, 85, 247, 0.28), rgba(126, 34, 206, 0.18)),
                  rgba(67, 26, 95, 0.92) !important;
                color: #faf5ff !important;
              }
              .mod-map-toggle {
                display: inline-flex;
                align-items: center;
                gap: 0.4rem;
                font-size: 0.92rem;
                min-width: 2.5rem;
                padding: 0 0.75rem;
                color: #ede9fe;
              }
              .mod-map-toggle[data-disabled="true"] {
                border-color: rgba(115, 115, 125, 0.36);
                background:
                  linear-gradient(180deg, rgba(82, 82, 91, 0.08), rgba(82, 82, 91, 0)),
                  rgba(24, 24, 27, 0.72);
                box-shadow:
                  inset 0 1px 0 rgba(255, 255, 255, 0.02),
                  inset 0 -1px 0 rgba(82, 82, 91, 0.18);
                color: #a1a1aa;
                opacity: 0.8;
              }
              .mod-map-toggle input {
                accent-color: #a855f7;
              }
              .mod-map-toggle input:disabled {
                cursor: not-allowed;
              }
              .mod-map-readonly,
              .mod-map-notice,
              .mod-map-mode,
              .mod-map-help {
                color: var(--mod-subtitle, #4b5563);
                font-size: 0.92rem;
              }
              .mod-map-mode {
                min-width: 0;
                white-space: nowrap;
              }
              .mod-map-status {
                color: var(--mod-subtitle, #4b5563);
                font-size: 0.92rem;
                font-weight: 600;
                margin-left: auto;
                text-align: right;
                white-space: nowrap;
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
                aspect-ratio: 1 / 1;
                min-height: 0;
                height: auto;
                border-radius: 0;
                border: 1px solid rgba(196, 181, 253, 0.2);
                overflow: hidden;
                background:
                  radial-gradient(circle at top, rgba(56, 189, 248, 0.22), transparent 34%),
                  linear-gradient(180deg, rgba(226, 232, 240, 0.94), rgba(248, 250, 252, 0.98));
                box-shadow:
                  0 20px 48px rgba(3, 7, 18, 0.26),
                  0 0 0 1px rgba(139, 92, 246, 0.08);
              }
              .mod-map-canvas .leaflet-container {
                width: 100%;
                height: 100%;
                background: transparent;
                font: inherit;
              }
              .mod-map-canvas .leaflet-control-zoom {
                border: 1px solid rgba(139, 92, 246, 0.28);
                border-radius: 0;
                overflow: hidden;
                box-shadow: 0 12px 28px rgba(15, 23, 42, 0.16);
              }
              .mod-map-canvas .leaflet-control-zoom a {
                border-radius: 0;
                background:
                  linear-gradient(180deg, rgba(248, 250, 252, 0.98), rgba(226, 232, 240, 0.96));
                color: #111827;
              }
              .mod-map-canvas .leaflet-control-zoom a:hover {
                background:
                  linear-gradient(180deg, rgba(237, 233, 254, 0.96), rgba(221, 214, 254, 0.92));
                color: #4c1d95;
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
              .mod-map-annotation-meta {
                margin-top: 0.3rem;
                color: #6b7280;
                font-size: 0.8rem;
              }
              @media (max-width: 768px) {
                .mod-map-toolbar-main,
                .mod-map-toolbar-actions,
                .mod-map-toolbar-group {
                  width: 100%;
                }
                .mod-map-toolbar-group-dimension {
                  flex-wrap: wrap;
                }
                .mod-map-toolbar-group-tools {
                  flex-wrap: wrap;
                  justify-content: stretch;
                }
                .mod-map-toolbar-pair-tools {
                  flex: 1 1 100%;
                }
                .mod-map-select {
                  min-width: 10rem;
                  flex: 1 1 11rem;
                }
                .mod-map-status {
                  max-width: 55%;
                }
                .mod-map-label-prompt {
                  width: calc(100% - 0.6rem);
                  transform: translate(-50%, calc(-100% - 0.6rem));
                }
                .mod-map-canvas {
                  max-width: 100%;
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
                    const createdBy = annotation.created_by_name
                      ? `<div class="mod-map-annotation-meta">By ${escapeHtml(annotation.created_by_name)}</div>`
                      : "";
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
                  const isLabelPromptOpen = (state) => state.labelPrompt && state.labelPrompt.hidden === false;
                  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
                  const placeLabelPrompt = (state, anchorPoint = null) => {
                    if (!state.labelPrompt || !state.canvasFrame) {
                      return;
                    }
                    const fallbackPoint = {
                      x: state.canvasFrame.clientWidth / 2,
                      y: state.canvasFrame.clientHeight / 2,
                    };
                    const requestedPoint = anchorPoint || state.lastPromptAnchorPoint || fallbackPoint;
                    const promptWidth = state.labelPrompt.offsetWidth || 304;
                    const promptHeight = state.labelPrompt.offsetHeight || 148;
                    const frameWidth = state.canvasFrame.clientWidth;
                    const frameHeight = state.canvasFrame.clientHeight;
                    const nextLeft = clamp(requestedPoint.x, promptWidth / 2 + 8, frameWidth - promptWidth / 2 - 8);
                    const nextTop = clamp(requestedPoint.y, promptHeight + 16, frameHeight - 12);
                    state.lastPromptAnchorPoint = { x: nextLeft, y: nextTop };
                    state.labelPrompt.style.left = `${nextLeft}px`;
                    state.labelPrompt.style.top = `${nextTop}px`;
                  };
                  const resolveLabelPrompt = (state, value, { focusMap = true } = {}) => {
                    const pendingLabelPrompt = state.pendingLabelPrompt;
                    state.pendingLabelPrompt = null;
                    state.pendingLabelKind = null;
                    closeLabelPrompt(state, { focusMap });
                    pendingLabelPrompt?.(value);
                  };
                  const closeLabelPrompt = (state, { focusMap = true } = {}) => {
                    if (state.labelPrompt) {
                      state.labelPrompt.hidden = true;
                    }
                    if (state.labelPromptInput) {
                      state.labelPromptInput.value = "";
                    }
                    state.lastPromptAnchorPoint = null;
                    if (focusMap) {
                      state.map?.getContainer?.().focus?.();
                    }
                  };
                  const cancelPendingAnnotation = (state, { focusMap = true } = {}) => {
                    const pendingKind = state.pendingLabelKind;
                    resolveLabelPrompt(state, null, { focusMap });
                    if (pendingKind === "line" || state.pendingLine.length > 0) {
                      cancelLineDraft(state);
                      setStatus(state, "Cancelled line draft.");
                      return;
                    }
                    if (pendingKind === "point" && state.config.canWrite) {
                      setToolState(state, "pan");
                      setStatus(state, "Cancelled point placement.");
                    }
                  };
                  const setSnapToggleState = (state, enabled) => {
                    const toggle = state.snapToggle;
                    const toggleLabel = toggle?.closest(".mod-map-toggle");
                    if (!toggle) {
                      return;
                    }
                    toggle.disabled = !enabled;
                    if (toggleLabel) {
                      toggleLabel.dataset.disabled = enabled ? "false" : "true";
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
                    setSnapToggleState(state, tool === "line");
                    if (tool === "line") {
                      const pointCount = state.pendingLine.length;
                      setModeText(state, pointCount > 0 ? `Line tool: ${pointCount} point${pointCount === 1 ? "" : "s"}` : "Line tool");
                      return;
                    }
                    setModeText(state, tool === "marker" ? "Point tool" : "Pan mode");
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
                    if (state.documentPointerDownListener) {
                      document.removeEventListener("pointerdown", state.documentPointerDownListener, true);
                    }
                    if (state.documentContextMenuListener) {
                      document.removeEventListener("contextmenu", state.documentContextMenuListener, true);
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
                  const isSquaremapOfflineError = (detail) =>
                    detail.startsWith("Squaremap request timed out:")
                    || detail.startsWith("Squaremap request failed:");
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
                  const apiSubpathUrl = (state, baseSuffix, relativePath) => {
                    const baseUrl = new URL(apiUrl(state, baseSuffix), window.location.origin);
                    const joinedPath =
                      `${baseUrl.pathname.replace(/\\/$/, "")}/${String(relativePath || "").replace(/^\\/+/, "")}`;
                    return `${joinedPath}${baseUrl.search}`;
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
                  const bringLayerToFront = (layer) => {
                    if (!layer) {
                      return;
                    }
                    if (typeof layer.bringToFront === "function") {
                      layer.bringToFront();
                      return;
                    }
                    if (typeof layer.eachLayer === "function") {
                      layer.eachLayer((childLayer) => bringLayerToFront(childLayer));
                    }
                  };
                  const reportClientError = (state, error, context) => {
                    if (!state?.config?.clientErrorUrl) {
                      return;
                    }
                    const message =
                      error instanceof Error
                        ? normalizeMapError(error.message, "Map data is unavailable.")
                        : normalizeMapError(String(error), "Map data is unavailable.");
                    const stack = error instanceof Error && typeof error.stack === "string" ? error.stack : null;
                    const signature = JSON.stringify([context, message, stack]);
                    const now = Date.now();
                    if (
                      state.lastClientErrorSignature === signature
                      && state.lastClientErrorReportedAt !== null
                      && now - state.lastClientErrorReportedAt < 30000
                    ) {
                      return;
                    }
                    state.lastClientErrorSignature = signature;
                    state.lastClientErrorReportedAt = now;
                    const payload = {
                      context,
                      message,
                      stack,
                      page_path: window.location.pathname,
                      app_name: state.config.appName || null,
                      node_name: state.config.nodeName || null,
                      map_api_url: state.config.mapApiUrl || null,
                      public_map_url: state.config.publicMapUrl || null,
                    };
                    console.error("[mod-map]", context, error);
                    void fetch(state.config.clientErrorUrl, {
                      method: "POST",
                      headers: {
                        "Content-Type": "application/json",
                        Accept: "application/json",
                      },
                      body: JSON.stringify(payload),
                      keepalive: true,
                    }).catch(() => {});
                  };
                  const handleClientActionFailure = (state, error, context) => {
                    const message =
                      error instanceof Error
                        ? normalizeMapError(error.message, "Map action failed.")
                        : "Map action failed.";
                    reportClientError(state, error, context);
                    setStatus(state, message, "error");
                  };
                  const syncOverlayOrder = (state) => {
                    bringLayerToFront(state.squaremapLayer);
                    bringLayerToFront(state.playerLayer);
                    bringLayerToFront(state.annotationLayer);
                    bringLayerToFront(state.previewLayerGroup);
                  };
                  const replaceLayerGroup = (state, layerKey, nextLayer) => {
                    const currentLayer = state[layerKey];
                    if (currentLayer === nextLayer) {
                      syncOverlayOrder(state);
                      return;
                    }
                    nextLayer.addTo(state.map);
                    state[layerKey] = nextLayer;
                    currentLayer?.remove();
                    syncOverlayOrder(state);
                  };
                  const resetTileLayers = (state) => {
                    state.pendingTileLayer?.remove();
                    state.pendingTileLayer = null;
                    state.tileLayer?.remove();
                    state.tileLayer = null;
                  };
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
                  const loadAnnotationLayer = async (state) => {
                    const payload = await fetchJson(apiUrl(state, "/annotations"));
                    const nextLayer = window.L.layerGroup();
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
                        }).addTo(nextLayer);
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
                        ).addTo(nextLayer);
                        bindAnnotationLayer(state, layer, annotation);
                      }
                    }
                    return nextLayer;
                  };
                  const refreshAnnotations = async (state) => {
                    replaceLayerGroup(state, "annotationLayer", await loadAnnotationLayer(state));
                  };
                  const loadPlayerLayer = async (state) => {
                    const nextLayer = window.L.layerGroup();
                    if (!state.currentWorld) {
                      return { result: { source: MAP_SOURCE_LIVE, cacheUpdatedAtUnixMs: null, skipped: true }, layer: nextLayer };
                    }
                    if (!state.currentWorld.player_tracker?.enabled) {
                      return { result: { source: MAP_SOURCE_LIVE, cacheUpdatedAtUnixMs: null, skipped: true }, layer: nextLayer };
                    }
                    const result = await fetchJsonDetailed(apiUrl(state, "/players"));
                    const payload = result.data;
                    const players = Array.isArray(payload?.players)
                      ? payload.players
                      : Array.isArray(payload)
                        ? payload
                        : [];
                    if (players.length === 0) {
                      return { result, layer: nextLayer };
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
                      }).addTo(nextLayer);
                      marker.bindTooltip(player.display_name || player.name, {
                        direction: "top",
                        className: "mod-map-player-label",
                      });
                    }
                    return { result, layer: nextLayer };
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
                          iconUrl: apiSubpathUrl(
                            state,
                            "/assets",
                            `icon/registered/${encodeURIComponent(markerData.icon)}.png`,
                          ),
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
                  const loadSquaremapMarkerLayer = async (state) => {
                    const nextLayer = window.L.layerGroup();
                    if (!state.currentWorld) {
                      return { result: { source: MAP_SOURCE_LIVE, cacheUpdatedAtUnixMs: null, skipped: true }, layer: nextLayer };
                    }
                    const result = await fetchJsonDetailed(
                      apiUrl(state, `/worlds/${encodeURIComponent(state.currentWorld.name)}/markers`),
                    );
                    const payload = result.data;
                    if (!Array.isArray(payload)) {
                      return { result, layer: nextLayer };
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
                        layer.addTo(nextLayer);
                      }
                    }
                    return { result, layer: nextLayer };
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
                  const refreshTiles = async (state, { force = false } = {}) => {
                    if (!state.currentWorld) {
                      return;
                    }
                    const now = Date.now();
                    const intervalMs = Math.max(1, state.currentWorld.tiles_update_interval || 15) * 1000;
                    if (!force && state.lastTileRefreshAt !== null && now - state.lastTileRefreshAt < intervalMs) {
                      return;
                    }
                    state.tileRevision += 1;
                    state.lastTileRefreshAt = now;
                    state.pendingTileLayer?.remove();
                    state.pendingTileLayer = null;
                    const nextTileLayer = createTileLayer(state, state.currentWorld);
                    nextTileLayer.setOpacity(state.tileLayer ? 0 : 1);
                    nextTileLayer.addTo(state.map);
                    if (!state.tileLayer) {
                      state.tileLayer = nextTileLayer;
                      syncOverlayOrder(state);
                      return;
                    }
                    state.pendingTileLayer = nextTileLayer;
                    syncOverlayOrder(state);
                    await new Promise((resolve) => {
                      let settled = false;
                      const finish = () => {
                        if (settled) {
                          return;
                        }
                        settled = true;
                        timeoutId && window.clearTimeout(timeoutId);
                        if (state.pendingTileLayer !== nextTileLayer) {
                          resolve(null);
                          return;
                        }
                        const previousTileLayer = state.tileLayer;
                        state.pendingTileLayer = null;
                        state.tileLayer = nextTileLayer;
                        nextTileLayer.setOpacity(1);
                        if (previousTileLayer && previousTileLayer !== nextTileLayer) {
                          previousTileLayer.remove();
                        }
                        syncOverlayOrder(state);
                        resolve(null);
                      };
                      const timeoutId = window.setTimeout(finish, 1500);
                      nextTileLayer.once("load", finish);
                    });
                  };
                  const currentDraftColor = (state) => {
                    if (!state.colorInput || !state.colorInput.value) {
                      return "#22C55E";
                    }
                    return state.colorInput.value.toUpperCase();
                  };
                  const requestAnnotationLabel = async (state, labelKind, anchorPoint = null) => {
                    if (!state.labelPrompt || !state.labelPromptTitle || !state.labelPromptInput) {
                      throw new Error("Map label prompt is unavailable.");
                    }
                    if (state.pendingLabelPrompt) {
                      return null;
                    }
                    state.pendingLabelKind = labelKind;
                    state.labelPrompt.hidden = false;
                    state.labelPromptTitle.textContent = `Label ${labelKind}`;
                    state.labelPromptInput.placeholder = `Enter a label for this ${labelKind}`;
                    state.labelPromptInput.value = "";
                    window.setTimeout(() => {
                      placeLabelPrompt(state, anchorPoint);
                      state.labelPromptInput?.focus();
                      state.labelPromptInput?.select();
                    }, 0);
                    const label = await new Promise((resolve) => {
                      state.pendingLabelPrompt = resolve;
                    });
                    if (typeof label !== "string") {
                      return null;
                    }
                    const trimmedLabel = label.trim();
                    if (!trimmedLabel) {
                      setStatus(state, "A label is required.", "error");
                      return null;
                    }
                    if (trimmedLabel.length > 80) {
                      setStatus(state, "Labels must be 80 characters or fewer.", "error");
                      return null;
                    }
                    return trimmedLabel;
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
                  const createMarkerAnnotation = async (state, point, anchorPoint = null) => {
                    const label = await requestAnnotationLabel(state, "point", anchorPoint);
                    if (!label) {
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
                  const dedupeTrailingLinePoint = (state) => {
                    if (state.pendingLine.length < 2) {
                      return;
                    }
                    const lastPoint = state.pendingLine[state.pendingLine.length - 1];
                    const previousPoint = state.pendingLine[state.pendingLine.length - 2];
                    if (lastPoint.x !== previousPoint.x || lastPoint.z !== previousPoint.z) {
                      return;
                    }
                    state.pendingLine.pop();
                    updatePreview(state);
                  };
                  const finishLineAnnotation = async (state, anchorPoint = null) => {
                    dedupeTrailingLinePoint(state);
                    if (state.pendingLine.length < 2) {
                      setStatus(state, "A line needs at least two points.", "error");
                      return;
                    }
                    const label = await requestAnnotationLabel(state, "line", anchorPoint);
                    if (!label) {
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
                      loadSquaremapMarkerLayer(state),
                      loadPlayerLayer(state),
                      loadAnnotationLayer(state),
                    ]);
                    if (annotationResult.status !== "fulfilled") {
                      throw annotationResult.reason;
                    }
                    replaceLayerGroup(state, "annotationLayer", annotationResult.value);
                    if (markerResult.status === "fulfilled") {
                      replaceLayerGroup(state, "squaremapLayer", markerResult.value.layer);
                    }
                    if (playerResult.status === "fulfilled") {
                      replaceLayerGroup(state, "playerLayer", playerResult.value.layer);
                    }
                    const markersLoaded = markerResult.status === "fulfilled" ? markerResult.value.result : null;
                    const playersLoaded = playerResult.status === "fulfilled" ? playerResult.value.result : null;
                    const playersFailed = playerResult.status === "rejected";
                    if (markersLoaded && markersLoaded.source === MAP_SOURCE_LIVE) {
                      if (forceTiles) {
                        await refreshTiles(state, { force: true });
                      }
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
                      if (forceTiles) {
                        await refreshTiles(state, { force: true });
                      }
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
                    resetTileLayers(state);
                    state.tileRevision = 0;
                    state.lastTileRefreshAt = null;
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
                    const defaultWorldName = state.worldByName.has("minecraft_overworld")
                      ? "minecraft_overworld"
                      : state.manifest.initial_world_name;
                    const requestedWorldName =
                      state.currentWorld && state.worldByName.has(state.currentWorld.name)
                        ? state.currentWorld.name
                        : defaultWorldName;
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
                    reportClientError(state, error, "map-bootstrap");
                    setModeText(state, "Map unavailable");
                    const isOfflineError = isSquaremapOfflineError(detail);
                    if (isOfflineError) {
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
                      return;
                    }
                    setNotice(state, detail, "error");
                    setStatus(state, "Map data is unavailable.", "error");
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
                    state.toolButtons.marker?.addEventListener("click", () => {
                      if (state.tool === "marker") {
                        setToolState(state, "pan");
                        return;
                      }
                      cancelLineDraft(state);
                      setToolState(state, "marker");
                    });
                    state.toolButtons.line?.addEventListener("click", () => {
                      if (state.tool === "line") {
                        cancelLineDraft(state);
                        setStatus(state, "Cancelled line draft.");
                        return;
                      }
                      cancelLineDraft(state);
                      setToolState(state, "line");
                      updatePreview(state);
                      setStatus(state, "Line tool active. Double-click to save, right-click to cancel.");
                    });
                    state.labelPromptForm?.addEventListener("submit", (event) => {
                      event.preventDefault();
                      const value = state.labelPromptInput?.value ?? "";
                      resolveLabelPrompt(state, value);
                    });
                    state.labelPromptInput?.addEventListener("keydown", (event) => {
                      if (event.key !== "Escape") {
                        return;
                      }
                      event.preventDefault();
                      cancelPendingAnnotation(state);
                    });
                  };
                  const deleteAnnotation = async (containerId, annotationId) => {
                    const state = instances.get(containerId);
                    if (!state) {
                      return;
                    }
                    try {
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
                    } catch (error) {
                      handleClientActionFailure(state, error, "delete-annotation");
                    }
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
                      canvasFrame: get(config.canvasFrameId),
                      map: window.L.map(canvas, {
                        crs: window.L.CRS.Simple,
                        center: [0, 0],
                        zoom: 0,
                        minZoom: 0,
                        maxZoom: 8,
                        attributionControl: false,
                        doubleClickZoom: false,
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
                      pendingTileLayer: null,
                      tool: "pan",
                      worldSelect: get(config.worldSelectId),
                      colorInput: get(config.colorInputId),
                      snapToggle: get(config.snapToggleId),
                      statusEl: get(config.statusId),
                      noticeEl: get(config.noticeId),
                      modeEl: get(config.modeId),
                      refreshButton: get(config.refreshButtonId),
                      labelPrompt: get(config.promptId),
                      labelPromptTitle: get(config.promptTitleId),
                      labelPromptForm: get(config.promptFormId),
                      labelPromptInput: get(config.promptInputId),
                      labelPromptSubmit: get(config.promptSubmitId),
                      toolButtons: {
                        marker: get(config.markerButtonId),
                        line: get(config.lineButtonId),
                      },
                      pollTimer: null,
                      syncPromise: null,
                      visibilityTimer: null,
                      iconBaseUrl: "",
                      squaremapSource: MAP_SOURCE_LIVE,
                      lastSquaremapCacheUpdatedAtUnixMs: null,
                      lastClientErrorSignature: null,
                      lastClientErrorReportedAt: null,
                      pendingLabelPrompt: null,
                      pendingLabelKind: null,
                      lastPromptAnchorPoint: null,
                      documentPointerDownListener: null,
                      documentContextMenuListener: null,
                    };
                    state.documentPointerDownListener = (event) => {
                      if (!isLabelPromptOpen(state)) {
                        return;
                      }
                      if (event.button !== 0) {
                        return;
                      }
                      if (state.labelPrompt && event.target instanceof Node && state.labelPrompt.contains(event.target)) {
                        return;
                      }
                      event.preventDefault();
                      event.stopPropagation();
                      cancelPendingAnnotation(state, { focusMap: false });
                    };
                    state.documentContextMenuListener = (event) => {
                      if (!isLabelPromptOpen(state)) {
                        return;
                      }
                      event.preventDefault();
                      event.stopPropagation();
                      cancelPendingAnnotation(state, { focusMap: false });
                    };
                    document.addEventListener("pointerdown", state.documentPointerDownListener, true);
                    document.addEventListener("contextmenu", state.documentContextMenuListener, true);
                    state.squaremapLayer.addTo(state.map);
                    state.playerLayer.addTo(state.map);
                    state.annotationLayer.addTo(state.map);
                    state.previewLayerGroup.addTo(state.map);
                    instances.set(config.containerId, state);
                    bindControls(state);
                    setToolState(state, "pan");
                    state.map.on("click", (event) => {
                      if (!state.config.canWrite || !state.currentWorld) {
                        return;
                      }
                      if (isLabelPromptOpen(state)) {
                        return;
                      }
                      if (event.originalEvent?.detail && event.originalEvent.detail > 1) {
                        return;
                      }
                      const rawPoint = toPoint(state.currentWorld, event.latlng);
                      if (state.tool === "marker") {
                        void createMarkerAnnotation(state, rawPoint, event.containerPoint).catch((error) =>
                          handleClientActionFailure(state, error, "create-marker-annotation")
                        );
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
                    state.map.on("dblclick", (event) => {
                      if (!state.config.canWrite || !state.currentWorld || isLabelPromptOpen(state) || state.tool !== "line") {
                        return;
                      }
                      event.originalEvent?.preventDefault?.();
                      void finishLineAnnotation(state, event.containerPoint).catch((error) =>
                        handleClientActionFailure(state, error, "finish-line-annotation")
                      );
                    });
                    state.map.on("contextmenu", (event) => {
                      if (!state.config.canWrite) {
                        return;
                      }
                      if (isLabelPromptOpen(state)) {
                        cancelPendingAnnotation(state, { focusMap: false });
                        return;
                      }
                      if (state.tool !== "line") {
                        return;
                      }
                      event.originalEvent?.preventDefault?.();
                      cancelLineDraft(state);
                      setStatus(state, "Cancelled line draft.");
                    });
                    state.visibilityTimer = window.setInterval(() => {
                      if (state.container.offsetParent !== null) {
                        state.map.invalidateSize(false);
                      }
                    }, 1000);
                    state.pollTimer = window.setInterval(() => {
                      if (document.visibilityState !== "visible" || state.container.offsetParent === null) {
                        return;
                      }
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
        refresh_async_runtime_model: Callable[[], Awaitable[ModWebBasePageModel]] | None = None,
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
            ui.navigate.to(current_section_url)

        with ui.column().classes("w-full gap-3"):
            with ui.row().classes("mod-section-strip w-full items-start justify-between gap-3 flex-wrap"):
                with ui.element("div").classes("mod-section-tabs-shell"):
                    with ui.tabs(value=initial_tab_id, on_change=sync_section_url).classes("mod-section-tabs") as section_tabs:
                        for tab in tabs:
                            tab_by_id[tab.tab_id] = ui.tab(tab.tab_id, label=tab.label)
                with ui.row().classes("mod-section-chrome items-start justify-end gap-3 flex-wrap"):
                    for tab in tabs:
                        if tab.tab_id != initial_tab_id:
                            continue
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
                        if tab.tab_id != initial_tab_id:
                            continue
                        section_runtime_model = self._render_page_section(
                            ui=ui,
                            model=model,
                            user=user,
                            tab=tab,
                            chat_surface=chat_surface,
                            refresh_async_runtime_model=refresh_async_runtime_model,
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
        refresh_async_runtime_model: Callable[[], Awaitable[ModWebBasePageModel]] | None = None,
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
            case ModWebAppSectionKind.UPDATE:
                return self._render_update_section(
                    ui=ui,
                    model=model,
                    user=user,
                    refresh_async_runtime_model=refresh_async_runtime_model,
                )
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

    @staticmethod
    def _resolve_client_pack_mod_names(
        *,
        mods: tuple[NodeModEntry, ...],
        optional_names: frozenset[str],
        choice_names: dict[str, str],
    ) -> tuple[str, ...]:
        optional_entries: dict[str, NodeModEntry] = {
            entry.name: entry
            for entry in mods
            if entry.downloadable and entry.client_pack.policy is ClientPackPolicy.OPTIONAL
        }
        unknown_optional_names: frozenset[str] = optional_names.difference(optional_entries)
        if unknown_optional_names:
            raise ValueError(f"Unknown optional client-pack mods: {', '.join(sorted(unknown_optional_names))}")

        choice_groups: dict[str, frozenset[str]] = {}
        for entry in mods:
            if not entry.downloadable or entry.client_pack.policy is not ClientPackPolicy.ALTERNATIVE:
                continue
            group_name: str | None = entry.client_pack.choice_group
            if group_name is None:
                raise ValueError(f"Alternative client-pack mod {entry.name!r} has no choice group.")
            choice_groups[group_name] = choice_groups.get(group_name, frozenset()).union({entry.name})
        if choice_names.keys() != choice_groups.keys():
            raise ValueError("Every client-pack choice group requires one selection.")
        for group_name, selected_name in choice_names.items():
            if selected_name not in choice_groups[group_name]:
                raise ValueError(f"Invalid selection {selected_name!r} for client-pack group {group_name!r}.")

        return tuple(
            entry.name
            for entry in mods
            if entry.downloadable
            and (
                entry.client_pack.policy is ClientPackPolicy.REQUIRED
                or (entry.client_pack.policy is ClientPackPolicy.OPTIONAL and entry.name in optional_names)
                or (
                    entry.client_pack.policy is ClientPackPolicy.ALTERNATIVE
                    and entry.client_pack.choice_group is not None
                    and choice_names[entry.client_pack.choice_group] == entry.name
                )
            )
        )

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
        show_sort: bool = len(mod_options) > 1
        current_search_query: str = ""
        current_sort_order: ModWebModSortOrder = ModWebModSortOrder.NEWEST
        downloadable_names: tuple[str, ...] = tuple[str, ...](
            entry.name for entry in model.mods.mods if entry.downloadable
        )
        optional_client_entries: tuple[NodeModEntry, ...] = tuple(
            entry
            for entry in model.mods.mods
            if entry.downloadable and entry.client_pack.policy is ClientPackPolicy.OPTIONAL
        )
        client_choice_groups: dict[str, tuple[NodeModEntry, ...]] = {}
        for entry in model.mods.mods:
            client_pack = entry.client_pack
            if not entry.downloadable or client_pack.policy is not ClientPackPolicy.ALTERNATIVE:
                continue
            if client_pack.choice_group is None:
                raise ValueError(f"Alternative client-pack mod {entry.name!r} has no choice group.")
            client_choice_groups[client_pack.choice_group] = (
                *client_choice_groups.get(client_pack.choice_group, ()),
                entry,
            )
        has_client_pack_choices: bool = bool(optional_client_entries or client_choice_groups)
        client_choice_defaults: dict[str, str] = {}
        for group_name, choices in client_choice_groups.items():
            defaults: tuple[NodeModEntry, ...] = tuple(entry for entry in choices if entry.client_pack.default_choice)
            if len(choices) < 2 or len(defaults) != 1:
                raise ValueError(
                    f"Client-pack choice group {group_name!r} requires at least two mods and exactly one default."
                )
            client_choice_defaults[group_name] = defaults[0].name
        can_delete_mods: bool = self._user_has_level(user, Power_Level.sudo)
        deletable_names: tuple[str, ...] = tuple[str, ...](
            entry.name for entry in model.mods.mods if can_delete_mods and not self._is_builtin_mod(entry)
        )
        downloadable_name_set: frozenset[str] = frozenset(downloadable_names)
        deletable_name_set: frozenset[str] = frozenset(deletable_names)
        selectable_names: tuple[str, ...] = tuple[str, ...](
            entry.name
            for entry in model.mods.mods
            if entry.name in downloadable_name_set or entry.name in deletable_name_set
        )
        selectable_name_set: frozenset[str] = frozenset(selectable_names)
        downloadable_count: int = model.mods.summary.downloadable_count
        can_upload_mod: bool = self._user_has_level(user, Power_Level.user)
        selection_button = None
        download_button = None
        delete_button = None
        result_count_label: Label | None = None

        def update_result_count(visible_count: int) -> None:
            if result_count_label is None:
                return
            result_count_label.set_text(
                self._mod_result_count_label(
                    visible_count=visible_count,
                    total_count=len(model.mods.mods),
                )
            )

        def selected_downloadable_mod_names_in_page_order() -> tuple[str, ...]:
            return tuple[str, ...](
                entry.name
                for entry in model.mods.mods
                if entry.name in selected_mod_names and entry.name in downloadable_name_set
            )

        def selected_deletable_mod_names_in_page_order() -> tuple[str, ...]:
            return tuple[str, ...](
                entry.name
                for entry in model.mods.mods
                if entry.name in selected_mod_names and entry.name in deletable_name_set
            )

        def update_count() -> None:
            if selection_button is None or download_button is None:
                return
            selected_count: int = len(selected_mod_names)
            selected_downloadable_count: int = len(selected_downloadable_mod_names_in_page_order())
            selected_deletable_count: int = len(selected_deletable_mod_names_in_page_order())
            selection_button.set_text(self._selection_toggle_label(selected_count=selected_count))
            download_button.set_text(
                self._download_selection_label(
                    selected_count=selected_downloadable_count,
                    downloadable_count=downloadable_count,
                )
            )
            can_download: bool = downloadable_count > 0 and (not selected_mod_names or selected_downloadable_count > 0)
            selection_button.set_enabled(bool(selectable_names))
            download_button.set_enabled(can_download)
            if delete_button is not None:
                delete_button.set_text(self._delete_selection_label(selected_count=selected_deletable_count))
                delete_button.set_enabled(selected_deletable_count > 0)

        def set_selected(mod_name: str, selected: bool) -> None:
            if selected:
                selected_mod_names.add(mod_name)
            else:
                selected_mod_names.discard(mod_name)
            update_count()

        def select_all() -> None:
            selected_mod_names.update(selectable_names)
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
            mod_names: tuple[str, ...] = selected_downloadable_mod_names_in_page_order()
            if mod_names:
                query: str = urlencode({"selected_only": "true", "mod_name": list(mod_names)}, doseq=True)
                await self._start_download(
                    ui=ui,
                    user=user,
                    model=model,
                    url=f"{self._download_base_url(model)}?{query}",
                    message=self._download_feedback_message(
                        kind=ModDownloadKind.SELECTED,
                        app_friendly=model.app_friendly,
                        selected_count=len(mod_names),
                    ),
                    filenames=(f"{model.app_name}-selected-mods.zip",),
                )
                return
            if selected_mod_names:
                ui.notify("No selected mods are downloadable.", type="warning")
                return
            if has_client_pack_choices:
                client_pack_dialog.open()
                return
            await self._start_download(
                ui=ui,
                user=user,
                model=model,
                url=model.download_all_url,
                message=self._download_feedback_message(
                    kind=ModDownloadKind.ALL,
                    app_friendly=model.app_friendly,
                ),
                filenames=(f"{model.app_name}-mods.zip",),
            )

        async def delete_selected() -> None:
            mod_names: tuple[str, ...] = selected_deletable_mod_names_in_page_order()
            if not mod_names:
                ui.notify("Select at least one deletable mod first.", type="warning")
                return
            try:
                for mod_name in mod_names:
                    await self._mutate_mod(
                        model=model,
                        mod_name=mod_name,
                        action=NodeModMutationAction.DELETE,
                        user=user,
                    )
            except Exception as xcp:
                ui.notify(f"Mod delete failed: {xcp}", type="negative")
                return
            delete_dialog.close()
            mod_label: str = "mod" if len(mod_names) == 1 else "mods"
            ui.notify(f"Deleted {len(mod_names)} {mod_label}.", type="positive")
            ui.navigate.reload()

        inline_upload_control: Upload | None = None

        async def upload_mods(event: "MultiUploadEventArguments") -> None:
            upload_names: tuple[str, ...] = tuple(upload_file.name for upload_file in event.files)
            if not upload_names:
                ui.notify("Choose at least one mod file to upload.", type="warning")
                return
            upload_label: str = upload_names[0] if len(upload_names) == 1 else f"{len(upload_names)} files"
            if inline_upload_control is not None:
                inline_upload_control.disable()
            ui.notify(f"Uploading {upload_label} to {model.app_friendly}.", type="info")
            try:
                result: NodeModUploadBatchResult = await self._upload_mods(
                    model=model,
                    upload_files=tuple(event.files),
                    user=user,
                )
            except Exception as xcp:
                if inline_upload_control is not None:
                    inline_upload_control.enable()
                ui.notify(f"Mod upload failed: {xcp}", type="negative")
                return
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        optional_client_checkboxes: dict[str, Checkbox] = {}
        client_choice_selects: dict[str, Select] = {}

        async def download_configured_client_pack() -> None:
            optional_names: frozenset[str] = frozenset(
                mod_name
                for mod_name, checkbox in optional_client_checkboxes.items()
                if bool(_value_as_object(checkbox))
            )
            choice_names: dict[str, str] = {}
            for group_name, select in client_choice_selects.items():
                selected_name: str = _value_as_text(select)
                choice_names[group_name] = selected_name
            try:
                mod_names: tuple[str, ...] = self._resolve_client_pack_mod_names(
                    mods=model.mods.mods,
                    optional_names=optional_names,
                    choice_names=choice_names,
                )
            except ValueError as xcp:
                ui.notify(str(xcp), type="warning")
                return
            if not mod_names:
                ui.notify("Select at least one mod for the client pack.", type="warning")
                return
            client_pack_dialog.close()
            query: str = urlencode({"selected_only": "true", "mod_name": list(mod_names)}, doseq=True)
            await self._start_download(
                ui=ui,
                user=user,
                model=model,
                url=f"{self._download_base_url(model)}?{query}",
                message=self._download_feedback_message(
                    kind=ModDownloadKind.SELECTED,
                    app_friendly=model.app_friendly,
                    selected_count=len(mod_names),
                ),
                filenames=(f"{model.app_name}-client-pack.zip",),
            )

        client_pack_dialog = ui.dialog()
        if has_client_pack_choices:
            with client_pack_dialog:
                with ui.card().classes("mod-card mod-dialog-card"):
                    with ui.column().classes("w-full gap-4 p-5"):
                        with ui.column().classes("gap-0"):
                            ui.label("Configure Client Pack").classes("text-xl font-black mod-title-small")
                            ui.label("Required mods are always included. Optional mods start selected.").classes(
                                "mod-subtitle text-sm"
                            )
                        required_entries: tuple[NodeModEntry, ...] = tuple(
                            entry
                            for entry in model.mods.mods
                            if entry.downloadable and entry.client_pack.policy is ClientPackPolicy.REQUIRED
                        )
                        if required_entries:
                            with ui.column().classes("w-full gap-2"):
                                ui.label("Required").classes("mod-stat-label")
                                for entry in required_entries:
                                    required_checkbox: Checkbox = ui.checkbox(
                                        entry.friendly,
                                        value=True,
                                    ).props("dense")
                                    required_checkbox.disable()
                        if optional_client_entries:
                            with ui.column().classes("w-full gap-2"):
                                ui.label("Optional").classes("mod-stat-label")
                                for entry in optional_client_entries:
                                    optional_client_checkboxes[entry.name] = ui.checkbox(
                                        entry.friendly,
                                        value=True,
                                    ).props("dense")
                        if client_choice_groups:
                            with ui.column().classes("w-full gap-3"):
                                ui.label("Choose One").classes("mod-stat-label")
                                for group_name, choices in client_choice_groups.items():
                                    group_label: str = group_name.replace("_", " ").replace("-", " ").strip().title()
                                    client_choice_selects[group_name] = (
                                        ui.select(
                                            {entry.name: entry.friendly for entry in choices},
                                            value=client_choice_defaults[group_name],
                                            label=group_label,
                                        )
                                        .props("filled square dense hide-bottom-space color=accent options-dark")
                                        .classes("w-full mod-config-select")
                                    )
                        with ui.row().classes("w-full justify-end gap-2"):
                            ui.button("Cancel", on_click=client_pack_dialog.close).classes("mod-list-button secondary")
                            ui.button("Download", on_click=download_configured_client_pack).classes("mod-list-button")

        with ui.dialog() as delete_dialog:
            with ui.card().classes("mod-card mod-dialog-card"):
                with ui.column().classes("w-full gap-4 p-5"):
                    with ui.column().classes("gap-0"):
                        ui.label("Delete Mods").classes("text-xl font-black mod-title-small")
                        ui.label(f"Remove the selected mods from {model.app_friendly}? This cannot be undone.").classes(
                            "mod-subtitle text-sm"
                        )
                    ui.label("Built-in mods are excluded automatically.").classes("mod-subtitle text-sm")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Cancel", on_click=delete_dialog.close).classes("mod-list-button secondary")
                        ui.button("Delete", on_click=delete_selected).classes("mod-list-button danger")

        with ui.card().classes(self._flat_tab_card_classes()):
            with ui.column().classes(self._tab_section_body_classes()):
                mods_description: str | None = self._mods_card_description(model.mods.summary)
                if mods_description is not None:
                    self._render_flat_tab_header(
                        ui=ui,
                        title="Mods",
                        description=mods_description,
                    )
                upload_picker_action: Callable[[], None] | None = None
                if can_upload_mod:

                    def _open_upload_picker() -> None:
                        return None

                    upload_picker_action = _open_upload_picker

                @ui.refreshable
                def _mod_download_rows(search_query: str) -> None:
                    checkboxes.clear()
                    if not model.mods.mods:
                        update_result_count(0)
                        return

                    filtered_mods = self._filter_mod_entries(
                        mods=model.mods.mods,
                        options=mod_options,
                        search_query=search_query,
                    )
                    filtered_mods = self._sort_mod_entries(filtered_mods, current_sort_order)
                    update_result_count(len(filtered_mods))
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
                                can_select=entry.name in selectable_name_set,
                                app_friendly=model.app_friendly,
                                model=model,
                                user=user,
                            )
                            if checkbox is not None:
                                checkboxes[entry.name] = checkbox
                                checkbox.set_value(entry.name in selected_mod_names)

                def _refresh_mod_rows(event: ModWebEventArgumentsContainer) -> None:
                    nonlocal current_search_query
                    current_search_query = _event_args_as_text(event)
                    _mod_download_rows.refresh(current_search_query)

                def _sort_mod_rows(event: ModWebEventArgumentsContainer) -> None:
                    nonlocal current_sort_order
                    current_sort_order = ModWebModSortOrder(_event_args_as_text(event))
                    _mod_download_rows.refresh(current_search_query)

                toolbar_bindings: _ModWebModToolbarBindings = self._render_mod_toolbar(
                    ui=ui,
                    model=model,
                    user=user,
                    toggle_selection=toggle_selection,
                    download_selected=download_selected,
                    delete_selected=delete_dialog.open,
                    upload_mod=upload_picker_action,
                    show_search=show_search,
                    on_search=_refresh_mod_rows if show_search else None,
                    show_sort=show_sort,
                    on_sort=_sort_mod_rows if show_sort else None,
                )
                selection_button: Button | None = toolbar_bindings.selection_button
                download_button: Button | None = toolbar_bindings.download_button
                delete_button: Button | None = toolbar_bindings.delete_button
                result_count_label = toolbar_bindings.result_count_label
                update_count()

                if can_upload_mod:
                    self._ensure_mod_list_dropzone_style(ui=ui)
                    inline_upload_control = ui.upload(
                        label="",
                        auto_upload=True,
                        multiple=True,
                        on_multi_upload=upload_mods,
                    ).classes("mod-mod-list-dropzone w-full")
                    if inline_upload_control is None:
                        raise RuntimeError("Inline mod upload control is not available.")
                    with inline_upload_control.add_slot("list"):
                        with ui.element("div").classes("mod-mod-list-drop-shell w-full"):
                            _mod_download_rows("")
                            with ui.element("div").classes("mod-mod-list-drop-overlay"):
                                ui.label("Drop mod files to upload").classes("text-sm")
                else:
                    _mod_download_rows("")
        return

    def _render_global_app_toolbar(
        self,
        *,
        ui: ModWebUi,
        model: ModWebBasePageModel,
        user: ModWebUser,
        refresh_async_runtime_model: Callable[[], Awaitable[ModWebBasePageModel]] | None = None,
        poll_runtime_model: bool = True,
    ) -> _ModWebRuntimeToolbarBindings:
        can_control_app_runtime: bool = self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.START)
        )
        can_kill_app_runtime: bool = self._user_has_level(user, required_app_mutation_level(NodeAppMutationAction.KILL))
        can_manage_app_state: bool = self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.ENABLE)
        )
        can_edit_app_details: bool = self._user_has_level(
            user, required_app_mutation_level(NodeAppMutationAction.UPDATE_DETAILS)
        )
        can_open_details_dialog: bool = can_manage_app_state or can_edit_app_details
        if not can_control_app_runtime and not can_kill_app_runtime and not can_open_details_dialog:
            return _ModWebRuntimeToolbarBindings()

        start_stop_button: Button | None = None
        kill_button: Button | None = None
        details_dialog: Dialog | None = None
        details_enable_disable_button: Button | None = None
        friendly_name_input: Input | None = None
        title_font_select: Select | None = None
        notes_input: Input | None = None
        running_cpu_points_input: Input | None = None
        running_ram_points_input: Input | None = None
        startup_cpu_points_input: Input | None = None
        startup_ram_points_input: Input | None = None
        steam_update_enabled_checkbox: Checkbox | None = None
        steam_update_branch_select: Select | None = None
        lifecycle_started_checkbox: Checkbox | None = None
        lifecycle_stopped_checkbox: Checkbox | None = None
        lifecycle_crashed_checkbox: Checkbox | None = None
        relay_notice_player_session_checkbox: Checkbox | None = None
        relay_notice_player_death_checkbox: Checkbox | None = None
        relay_notice_progress_checkbox: Checkbox | None = None
        relay_advancements_checkbox: Checkbox | None = None
        activity_provider_checkboxes: list[tuple[str, Checkbox]] = []
        current_runtime_model: ModWebBasePageModel = model
        start_stop_control_state: _ModWebStartStopControlState | None = None
        kill_control_state: _ModWebKillControlState | None = None
        steam_update_preset = self._details_steam_update_preset(model.app_name)
        steam_update_branch_options = self._details_steam_update_branch_options(
            app_name=model.app_name,
            update_info=model.update_info,
        )
        steam_update_selected_branch = self._details_steam_update_selected_branch(
            app_name=model.app_name,
            update_info=model.update_info,
        )
        steam_update_app_id = self._details_steam_update_app_id(
            app_name=model.app_name,
            update_info=model.update_info,
        )

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
            if can_manage_app_state and details_enable_disable_button is not None:
                details_enable_disable_button.set_text(self._app_enable_disable_label(runtime_model))
                details_enable_disable_button.classes(
                    replace=f"{self._app_enable_disable_button_classes(runtime_model)} mod-app-details-state-button"
                )

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
                ui.notify(
                    pending_message,
                    type="info",
                    timeout=_APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS,
                )
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
                ui.notify(
                    f"App action failed: {xcp}",
                    type="negative",
                    timeout=_APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS,
                )
                return
            completion_message: str | None = self._app_action_completion_message(
                pending_message=pending_message,
                result_message=result.message,
            )
            if completion_message is not None:
                ui.notify(
                    completion_message,
                    type="positive",
                    timeout=_APP_ACTION_NOTIFICATION_TIMEOUT_MILLISECONDS,
                )
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

        async def _handle_details_enable_disable(_: object | None = None) -> None:
            await run_app_action(self._app_enable_disable_action(current_runtime_model))

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

        def _parse_optional_positive_int(*, raw_value: str, field_label: str) -> int | None:
            value = raw_value.strip()
            if not value:
                return None
            try:
                parsed = int(value)
            except ValueError as xcp:
                raise ValueError(f"{field_label} must be a whole number.") from xcp
            if parsed < 0:
                raise ValueError(f"{field_label} must not be negative.")
            if parsed == 0:
                return None
            return parsed

        def _sync_details_steam_update_controls() -> None:
            if steam_update_enabled_checkbox is None or steam_update_branch_select is None:
                return
            if bool(_value_as_object(steam_update_enabled_checkbox)):
                steam_update_branch_select.enable()
                return
            steam_update_branch_select.disable()

        def _details_toggle_checkbox(
            *,
            label: str,
            value: bool,
            on_change: Callable[[object], object] | None = None,
        ) -> Checkbox:
            return ui.checkbox(label, value=value, on_change=on_change).props("dense").classes("mod-app-details-toggle")

        async def _handle_details_submit(_: object | None = None) -> None:
            if (
                friendly_name_input is None
                or title_font_select is None
                or notes_input is None
                or running_cpu_points_input is None
                or running_ram_points_input is None
                or startup_cpu_points_input is None
                or startup_ram_points_input is None
                or lifecycle_started_checkbox is None
                or lifecycle_stopped_checkbox is None
                or lifecycle_crashed_checkbox is None
            ):
                return
            next_friendly_name: str = _value_as_text(friendly_name_input).strip()
            if not next_friendly_name:
                ui.notify("Friendly name must not be empty.", type="negative")
                return
            if len(next_friendly_name) > APP_FRIENDLY_NAME_MAX_LENGTH:
                ui.notify(
                    f"Friendly name must be {APP_FRIENDLY_NAME_MAX_LENGTH} characters or fewer.",
                    type="negative",
                )
                return
            try:
                next_title_font_preset = normalise_app_title_font(_value_as_text(title_font_select))
            except (TypeError, ValueError):
                ui.notify("Title font is invalid.", type="negative")
                return
            next_notes: str = _value_as_text(notes_input)
            try:
                next_running_cpu_points = _parse_required_non_negative_int(
                    raw_value=_value_as_text(running_cpu_points_input),
                    field_label="Running CPU points",
                )
                next_running_ram_points = _parse_required_non_negative_int(
                    raw_value=_value_as_text(running_ram_points_input),
                    field_label="Running RAM points",
                )
                next_startup_cpu_points = _parse_optional_positive_int(
                    raw_value=_value_as_text(startup_cpu_points_input),
                    field_label="Startup CPU points",
                )
                next_startup_ram_points = _parse_optional_positive_int(
                    raw_value=_value_as_text(startup_ram_points_input),
                    field_label="Startup RAM points",
                )
            except ValueError as xcp:
                ui.notify(str(xcp), type="negative")
                return
            next_steam_update_enabled: bool | None = None
            next_steam_update_selected_branch: str | None = None
            disabled_activity_provider_ids = tuple(
                provider_id
                for provider_id, checkbox in activity_provider_checkboxes
                if not bool(_value_as_object(checkbox))
            )
            if steam_update_preset is not None:
                if steam_update_enabled_checkbox is None or steam_update_branch_select is None:
                    ui.notify("Steam update controls are unavailable.", type="negative")
                    return
                next_steam_update_enabled = bool(_value_as_object(steam_update_enabled_checkbox))
                if next_steam_update_enabled:
                    next_steam_update_selected_branch = _value_as_text(steam_update_branch_select).strip()
                    if next_steam_update_selected_branch not in steam_update_branch_options:
                        ui.notify("Steam update branch is invalid.", type="negative")
                        return
            try:
                result = await self._mutate_app(
                    model=current_runtime_model,
                    action=NodeAppMutationAction.UPDATE_DETAILS,
                    user=user,
                    friendly_name=next_friendly_name,
                    title_font_preset=next_title_font_preset,
                    notes=next_notes,
                    lifecycle_notice_started=bool(_value_as_object(lifecycle_started_checkbox)),
                    lifecycle_notice_stopped=bool(_value_as_object(lifecycle_stopped_checkbox)),
                    lifecycle_notice_crashed=bool(_value_as_object(lifecycle_crashed_checkbox)),
                    relay_notice_player_session=(
                        None
                        if relay_notice_player_session_checkbox is None
                        else bool(_value_as_object(relay_notice_player_session_checkbox))
                    ),
                    relay_notice_player_death=(
                        None
                        if relay_notice_player_death_checkbox is None
                        else bool(_value_as_object(relay_notice_player_death_checkbox))
                    ),
                    relay_notice_progress=(
                        None
                        if relay_notice_progress_checkbox is None
                        else bool(_value_as_object(relay_notice_progress_checkbox))
                    ),
                    relay_advancements_enabled=(
                        None
                        if relay_advancements_checkbox is None
                        else bool(_value_as_object(relay_advancements_checkbox))
                    ),
                    disabled_activity_provider_ids=disabled_activity_provider_ids,
                    running_cpu_points=next_running_cpu_points,
                    running_ram_points=next_running_ram_points,
                    startup_cpu_points=next_startup_cpu_points,
                    startup_ram_points=next_startup_ram_points,
                    steam_update_enabled=next_steam_update_enabled,
                    steam_update_selected_branch=next_steam_update_selected_branch,
                )
            except Exception as xcp:
                log.warning(
                    "App details update failed: node=%s app=%s error=%s",
                    current_runtime_model.node_name,
                    current_runtime_model.app_name,
                    xcp,
                )
                ui.notify(f"App details update failed: {xcp}", type="negative")
                return
            ui.notify(result.message, type="positive")
            ui.navigate.reload()

        if can_open_details_dialog:
            with ui.dialog() as details_dialog:
                with ui.card().classes("mod-card mod-dialog-card mod-app-details-dialog-card"):
                    with ui.column().classes("w-full gap-4 mod-app-details-layout"):
                        with ui.column().classes("gap-1"):
                            ui.label("App Details").classes("text-xl font-black mod-title-small")
                            ui.label("Update instance-level details shown across web and relay surfaces.").classes(
                                "mod-subtitle text-sm"
                            )
                        if can_edit_app_details:
                            resource_points = model.resource_points
                            running_cpu_points_value = (
                                "0" if resource_points is None else str(resource_points.cpu_points_running)
                            )
                            running_ram_points_value = (
                                "0" if resource_points is None else str(resource_points.ram_points_running)
                            )
                            startup_cpu_points_value = (
                                ""
                                if (
                                    resource_points is None
                                    or not resource_points.startup_defined
                                    or resource_points.cpu_points_startup == resource_points.cpu_points_running
                                )
                                else str(resource_points.cpu_points_startup)
                            )
                            startup_ram_points_value = (
                                ""
                                if (
                                    resource_points is None
                                    or not resource_points.startup_defined
                                    or resource_points.ram_points_startup == resource_points.ram_points_running
                                )
                                else str(resource_points.ram_points_startup)
                            )
                            with ui.column().classes("mod-app-details-section"):
                                friendly_name_input = (
                                    ui.input("Friendly name", value=model.app_friendly)
                                    .props(
                                        "filled square dense clearable hide-bottom-space "
                                        f"color=accent autofocus maxlength={APP_FRIENDLY_NAME_MAX_LENGTH}"
                                    )
                                    .classes("mod-app-details-field")
                                )
                                title_font_select = (
                                    ui.select(
                                        self._app_title_font_options(
                                            app_name=model.app_name,
                                            selected_value=model.app_title_font_preset,
                                        ),
                                        value=model.app_title_font_preset,
                                        label=f"Title font [{self._app_title_font_default_label(app_name=model.app_name)}]",
                                    )
                                    .props("filled square dense hide-bottom-space color=accent options-dark")
                                    .classes("mod-app-details-field")
                                )
                                notes_input = (
                                    ui.input("Shared instance notes", value=model.app_notes or "")
                                    .props("filled square type=textarea autogrow hide-bottom-space color=accent")
                                    .classes("mod-app-details-field mod-app-details-notes")
                                )
                                if steam_update_preset is not None:
                                    with ui.column().classes("mod-app-details-subsection"):
                                        ui.label("Update Configuration").classes("mod-stat-label")
                                        ui.label(
                                            "Enable or repair the default Steam updater block for this instance."
                                        ).classes("mod-subtitle text-xs")
                                        steam_update_enabled_checkbox = _details_toggle_checkbox(
                                            label="Enable Steam updates",
                                            value=model.update_info is not None,
                                            on_change=lambda _: _sync_details_steam_update_controls(),
                                        )
                                        if steam_update_app_id is not None:
                                            ui.label(f"Steam App ID: {steam_update_app_id}").classes(
                                                "mod-subtitle text-xs"
                                            )
                                        steam_update_branch_select = (
                                            ui.select(
                                                steam_update_branch_options,
                                                value=steam_update_selected_branch,
                                                label="Configured target branch",
                                            )
                                            .props("filled square dense hide-bottom-space color=accent options-dark")
                                            .classes("mod-app-details-field")
                                        )
                                        _sync_details_steam_update_controls()
                                with ui.column().classes("mod-app-details-subsection"):
                                    ui.label("Resource Points").classes("mod-stat-label")
                                    ui.label("Leave a startup field blank, or set it to 0, to use that resource's running points.").classes(
                                        "mod-subtitle text-xs"
                                    )
                                    with ui.row().classes("w-full gap-2 flex-wrap"):
                                        running_cpu_points_input = (
                                            ui.input("Running CPU", value=running_cpu_points_value)
                                            .props(
                                                "filled square dense hide-bottom-space color=accent "
                                                "type=number inputmode=numeric step=1 min=0"
                                            )
                                            .classes("mod-app-details-field mod-app-details-point-field")
                                        )
                                        running_ram_points_input = (
                                            ui.input("Running RAM", value=running_ram_points_value)
                                            .props(
                                                "filled square dense hide-bottom-space color=accent "
                                                "type=number inputmode=numeric step=1 min=0"
                                            )
                                            .classes("mod-app-details-field mod-app-details-point-field")
                                        )
                                    with ui.row().classes("w-full gap-2 flex-wrap"):
                                        startup_cpu_points_input = (
                                            ui.input("Startup CPU", value=startup_cpu_points_value)
                                            .props(
                                                "filled square dense hide-bottom-space color=accent "
                                                "type=number inputmode=numeric step=1 min=0"
                                            )
                                            .classes("mod-app-details-field mod-app-details-point-field")
                                        )
                                        startup_ram_points_input = (
                                            ui.input("Startup RAM", value=startup_ram_points_value)
                                            .props(
                                                "filled square dense hide-bottom-space color=accent "
                                                "type=number inputmode=numeric step=1 min=0"
                                            )
                                            .classes("mod-app-details-field mod-app-details-point-field")
                                        )
                                with ui.column().classes("mod-app-details-subsection"):
                                    ui.label("Relay Notices").classes("mod-stat-label")
                                    lifecycle_started_checkbox = _details_toggle_checkbox(
                                        label="Started",
                                        value=model.lifecycle_notice_started,
                                    )
                                    lifecycle_stopped_checkbox = _details_toggle_checkbox(
                                        label="Stopped",
                                        value=model.lifecycle_notice_stopped,
                                    )
                                    lifecycle_crashed_checkbox = _details_toggle_checkbox(
                                        label="Crash",
                                        value=model.lifecycle_notice_crashed,
                                    )
                                    if model.relay_notice_player_session is not None:
                                        relay_notice_player_session_checkbox = _details_toggle_checkbox(
                                            label="Player Join/Leave",
                                            value=model.relay_notice_player_session,
                                        )
                                    if model.relay_notice_player_death is not None:
                                        relay_notice_player_death_checkbox = _details_toggle_checkbox(
                                            label="Death",
                                            value=model.relay_notice_player_death,
                                        )
                                    if model.relay_notice_progress is not None:
                                        relay_notice_progress_label = model.relay_notice_progress_label or "Progress"
                                        relay_notice_progress_checkbox = _details_toggle_checkbox(
                                            label=f"{relay_notice_progress_label}",
                                            value=model.relay_notice_progress,
                                        )
                                    if model.relay_advancements_enabled is not None:
                                        relay_advancement_term = model.relay_advancement_term or "Advancement"
                                        relay_advancements_checkbox = _details_toggle_checkbox(
                                            label=f"{relay_advancement_term}",
                                            value=model.relay_advancements_enabled,
                                        )
                                if model.activity_providers:
                                    with ui.column().classes("mod-app-details-subsection"):
                                        ui.label("Activity Providers").classes("mod-stat-label")
                                        for provider in model.activity_providers:
                                            checkbox = _details_toggle_checkbox(
                                                label=provider.label,
                                                value=provider.enabled,
                                            )
                                            activity_provider_checkboxes.append((provider.provider_id, checkbox))
                        if can_manage_app_state:
                            with ui.column().classes("mod-app-details-section"):
                                ui.label("Instance State").classes("mod-stat-label")
                                details_enable_disable_button = ui.button(
                                    self._app_enable_disable_label(model),
                                    on_click=_handle_details_enable_disable,
                                ).classes(
                                    f"{self._app_enable_disable_button_classes(model)} mod-app-details-state-button"
                                )
                        with ui.row().classes("w-full justify-end gap-2 mod-app-details-actions"):
                            ui.button("Cancel", on_click=details_dialog.close).classes("mod-list-button secondary")
                            if can_edit_app_details:
                                ui.button("Save", on_click=_handle_details_submit).classes("mod-list-button")

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
                    if can_open_details_dialog and details_dialog is not None:
                        ui.button(
                            "Properties",
                            on_click=details_dialog.open,
                        ).classes("mod-list-button secondary mod-toolbar-button")
        if (
            poll_runtime_model
            and (can_control_app_runtime or can_kill_app_runtime)
            and refresh_async_runtime_model is not None
        ):
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
        delete_selected: Callable[[], None],
        upload_mod: Callable[[], object] | None = None,
        show_search: bool = False,
        on_search: Callable[[ModWebEventArgumentsContainer], None] | None = None,
        show_sort: bool = False,
        on_sort: Callable[[ModWebEventArgumentsContainer], None] | None = None,
    ) -> _ModWebModToolbarBindings:
        can_upload_mod: bool = upload_mod is not None and self._user_has_level(user, Power_Level.user)
        can_delete_mods: bool = self._user_has_level(user, Power_Level.sudo) and any(
            not self._is_builtin_mod(entry) for entry in model.mods.mods
        )
        show_bulk_mod_actions: bool = bool(model.mods.mods)
        if not can_upload_mod and not show_bulk_mod_actions and not show_search and not show_sort:
            return _ModWebModToolbarBindings(
                selection_button=None,
                download_button=None,
                delete_button=None,
                result_count_label=None,
            )

        selection_button: Button | None = None
        download_button: Button | None = None
        delete_button: Button | None = None
        result_count_label: Label | None = None

        with ui.row().classes("mod-tab-toolbar mod-mods-toolbar w-full"):
            with ui.row().classes("mod-mods-toolbar-filters w-full"):
                if show_search:
                    if on_search is None:
                        raise ValueError("Mod search handler is not available.")
                    search_input: Input = (
                        ui.input(placeholder="Search mods")
                        .props(
                            "filled square dense clearable hide-bottom-space color=accent "
                            f"debounce={_SEARCH_INPUT_DEBOUNCE_MILLISECONDS}"
                        )
                        .classes("mod-config-search mod-settings-search mod-mods-toolbar-search")
                    )
                    search_input.on("update:model-value", on_search)
                if show_sort:
                    if on_sort is None:
                        raise ValueError("Mod sort handler is not available.")
                    sort_select: Select = (
                        ui.select(
                            {order.value: order.label for order in ModWebModSortOrder},
                            value=ModWebModSortOrder.NEWEST.value,
                            label="Sort",
                        )
                        .props("filled square dense hide-bottom-space color=accent options-dark")
                        .classes("mod-config-select mod-mods-toolbar-sort")
                    )
                    sort_select.on("update:model-value", on_sort)
                result_count_label = ui.label(
                    self._mod_result_count_label(
                        visible_count=len(model.mods.mods),
                        total_count=len(model.mods.mods),
                    )
                ).classes("mod-mods-toolbar-result-count")
            with ui.row().classes("mod-tab-toolbar-actions mod-mods-toolbar-actions"):
                if can_upload_mod:
                    upload_button = ui.button("Upload", on_click=upload_mod).classes(
                        "mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill"
                    )
                    upload_button.on(
                        "click",
                        js_handler=(
                            "(event) => document.querySelector('.mod-mod-list-dropzone input[type=\"file\"]')?.click()"
                        ),
                    )
                if show_bulk_mod_actions:
                    selection_button = ui.button("", on_click=toggle_selection).classes(
                        "mod-list-button secondary mod-toolbar-button mod-toolbar-button-fill"
                    )
                    download_button = ui.button("", on_click=download_selected).classes(
                        "mod-list-button mod-toolbar-button mod-toolbar-button-fill mod-toolbar-primary"
                    )
                    if can_delete_mods:
                        delete_button = ui.button("", on_click=delete_selected).classes(
                            "mod-list-button danger mod-toolbar-button mod-toolbar-button-fill"
                        )
        return _ModWebModToolbarBindings(
            selection_button=selection_button,
            download_button=download_button,
            delete_button=delete_button,
            result_count_label=result_count_label,
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
    def _mods_card_description(summary: NodeModSummary) -> str | None:
        if summary.total_count == 0:
            return "No mods are currently indexed."
        return None

    @staticmethod
    def _mods_header_badges(summary: NodeModSummary) -> tuple[_ModWebBadgeSpec, ...]:
        mod_label = "mod" if summary.total_count == 1 else "mods"
        coremod_label = "coremod" if summary.coremod_count == 1 else "coremods"
        badges: list[_ModWebBadgeSpec] = [_ModWebBadgeSpec(text=f"{summary.total_count} {mod_label}", tone="black")]
        if summary.non_downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.non_downloadable_count} blocked", tone="warn"))
        if summary.downloadable_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.downloadable_count} downloadable", tone="purple"))
        if summary.coremod_count > 0:
            badges.append(_ModWebBadgeSpec(text=f"{summary.coremod_count} {coremod_label}", tone="red"))
        return tuple[_ModWebBadgeSpec, ...](badges)
